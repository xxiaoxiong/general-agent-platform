"""执行上下文准备：把语义路线转换为可执行资源（文档/附件/续做上下文）。

从 API 层抽出，既供显式模式在创建 Run 前确定性地准备，也供 Run 内决策后
在同一处完成准备，避免两套逻辑漂移。所有资源仍由 documents.repository
按用户归属重新校验。
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Iterable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent import AgentRun
from app.models.document import RunInputRequest

from ..document_turn import document_turn, is_template_reference
from ..task_router import (
    DOCUMENT_GRAPH_TYPES, choose_word_attachment, continuation_attachments,
    recent_word_attachments, resolve_attachment_refs_async, word_candidates,
)


@dataclass
class PreparedExecution:
    execution_context: dict[str, Any]
    attachment_refs: list[dict[str, Any]] = field(default_factory=list)
    declared_refs: list[dict[str, Any]] = field(default_factory=list)
    inherited: list[dict[str, Any]] = field(default_factory=list)


async def _resolve_inherited(
    ref: dict[str, Any], *, user_id: int, agent_id: int, session_id: int | None,
) -> dict[str, Any] | None:
    try:
        return (await resolve_attachment_refs_async(
            [ref], user_id=user_id, agent_id=agent_id, session_id=session_id,
        ))[0]
    except (ValueError, OSError):
        return None


async def prepare_execution(
    db: AsyncSession,
    *,
    message: str,
    history: list[dict[str, Any]],
    route: dict[str, Any],
    user_id: int,
    agent_id: int,
    session_id: int | None,
    is_superuser: bool = False,
    attachment_refs: Iterable[dict[str, Any]] = (),
    document_id: int | None = None,
    document_profile_id: int | None = None,
    task_options: dict[str, Any] | None = None,
    allow_upgrade: bool = True,
    artifacts: Iterable[dict[str, Any]] = (),
) -> PreparedExecution:
    """返回附带文档上下文、附件引用与资源版本的执行上下文。"""
    refs = list(attachment_refs)
    declared = list(refs)
    options = dict(task_options or {})
    current_word = word_candidates(refs)
    previous_word = recent_word_attachments(history) if session_id and not current_word else []

    # 指代前文（"这份报告/基于上面"）且本轮未重传时，沿用会话最近上传的 Word
    for ref in continuation_attachments(message, history, route.get("task_type", ""), refs):
        resolved = await _resolve_inherited(
            ref, user_id=user_id, agent_id=agent_id, session_id=session_id,
        )
        if resolved is not None:
            refs.append(resolved)
            declared.append(resolved)

    inherited: list[dict[str, Any]] = []
    anchor_run: str | None = None
    continued = None
    if (session_id and route.get("task_type") == "general_chat" and not refs
            and not (document_id or document_profile_id or options)):
        from ..completion import resolve_completion_resume

        continued = await resolve_completion_resume(db, user_id, session_id, message, history)
    if continued:
        route = {**route, "completion_resume": continued,
                 "reason": "继续本会话未完成的文件交付"}
    else:
        route, inherited, anchor_run = document_turn(
            message, history, route, allow_upgrade=allow_upgrade,
        )

    context = route.get("document_context")
    if context is not None:
        template_refs = current_word if is_template_reference(message) else []
        known = {ref["path"] for ref in refs}
        for ref in (() if template_refs else inherited):
            if ref["path"] in known:
                continue
            resolved = await _resolve_inherited(
                ref, user_id=user_id, agent_id=agent_id, session_id=session_id,
            )
            if resolved is None:
                continue
            refs.append(resolved)
            template_refs.append(resolved)
            known.add(resolved["path"])
        context["template_paths"] = [ref["path"] for ref in template_refs]
        if anchor_run is None:
            # 自动模式的历史消息可能没有 document_run_id 标记：用产物归属回退
            anchor_run = next(
                (item.get("created_by_run_id") for item in artifacts
                 if isinstance(item, dict) and item.get("created_by_run_id")),
                None,
            )
        if anchor_run:
            responses = (await db.scalars(select(RunInputRequest.response).join(
                AgentRun, AgentRun.id == RunInputRequest.run_id,
            ).where(
                AgentRun.id == anchor_run, AgentRun.user_id == user_id,
                AgentRun.session_id == session_id, RunInputRequest.status == "submitted",
            ).order_by(RunInputRequest.ordinal))).all()
            context["carried_inputs"] = {
                key: value for response in responses if isinstance(response, dict)
                for key, value in response.items()
            }

    route["attachment_refs"] = refs
    target_document_id = document_id if document_id is not None else route.get("document_id")
    if route.get("document_version") and "document_version_no" not in options:
        options["document_version_no"] = route["document_version"]
    if (route.get("task_type") in DOCUMENT_GRAPH_TYPES
            or route.get("task_type") == "document_analyze"
            or document_profile_id or target_document_id):
        from app.services.documents.errors import DocumentError
        from app.services.documents.repository import resolve_task_resources

        editing = route.get("task_type") == "document_edit" and not target_document_id
        candidates = (current_word or previous_word) if editing else []
        chosen = choose_word_attachment(message, candidates)
        prefer_attachment = bool(current_word and editing) or bool(chosen and any(
            term in message for term in ("原文件", "原附件", str(chosen.get("name") or chosen["path"]))
        ))
        resources = await resolve_task_resources(
            db, user_id=user_id, is_superuser=is_superuser, session_id=session_id,
            task_type=route["task_type"], message=context["request"] if context else message,
            profile_id=document_profile_id, document_id=target_document_id,
            task_options=options, prefer_attachment=prefer_attachment,
            has_attachment=bool(candidates),
        )
        if editing and not resources.get("document_id") and candidates:
            if chosen is None:
                raise DocumentError(422, "有多份 Word 可编辑，请在消息中写明一个文件名")
            if not current_word:
                resolved = await _resolve_inherited(
                    chosen, user_id=user_id, agent_id=agent_id, session_id=session_id,
                )
                if resolved is None:
                    raise DocumentError(422, "之前的 Word 附件已不可用，请重新上传")
                chosen = resolved
            resources["source_attachment"] = chosen
        route.update(resources)

    return PreparedExecution(
        execution_context=route,
        attachment_refs=refs,
        declared_refs=declared,
        inherited=inherited,
    )
