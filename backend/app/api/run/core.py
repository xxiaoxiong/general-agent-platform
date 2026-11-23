"""Runs API v2（§7）：Run 创建/状态/事件 SSE/取消/恢复。

产品语义（§8.4 Cancel / Resume / Reconnect）：
- POST /runs 创建 Run 并返回 run_id（X-Run-Id 响应头），执行在 Runtime
  Worker 中推进，事件持久化于 agent_run_events；
- GET /runs/{id}/events 以 v2 协议（§7.1）SSE 订阅/回放，
  ``Last-Event-ID``（或 after_seq）断线续传，不丢失不重复不乱序；
- 停止订阅与取消分离：前端停止只断开 SSE，取消必须显式调用 cancel；
- 审批决策携带稳定 action_id（FR-06），resume 后重新订阅事件流；
- 所有事件以持久化日志为事实来源（FR-07），API 与执行 Worker 可不同进程。
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Literal, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Response
from fastapi.responses import StreamingResponse
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.core.database import get_db
from app.models.agent import Agent, AgentRun, AgentRunEvent, AgentSession
from app.models.user import User

from app.api.common.schemas import RunCancelRequest, RunCreate, RunInputSubmission, RunResumeRequest

from .common import _attachment_note, _drain_legacy_stream, _get_run_or_404, _load_agent_config

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/runs", tags=["runs-v2"])


# ── Run 生命周期 ──────────────────────────────────────────────────────────────

@router.post("")
async def create_run(
    body: RunCreate,
    response: Response,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key"),
):
    """创建 Run（§7 POST /runs）。

    响应即刻返回 run_id（X-Run-Id）；事件流经 GET /runs/{id}/events 订阅。
    幂等（P1-01）：同 Key 同摘要复用原 Run；同 Key 不同摘要 409。
    """
    from app.services.agent_runtime import get_runtime_service
    from app.services.agent_runtime.document_turn import document_turn, is_template_reference
    from app.services.agent_runtime.service import compute_request_hash
    from app.services.agent_runtime.repositories import IdempotencyConflict, SessionBusy
    from app.services.agent_runtime.source_bindings import SourceBindingError, check_source_bindings
    from app.services.agent_runtime.task_router import (
        DOCUMENT_GRAPH_TYPES, choose_word_attachment, recent_word_attachments, resolve_attachment_refs,
        route_task, word_candidates,
    )

    # 1) 会话定位（仅限当前用户）
    session: Optional[AgentSession] = None
    if body.conversation_id:
        res = await db.execute(
            select(AgentSession).where(
                AgentSession.id == body.conversation_id,
                AgentSession.user_id == current_user.id,
            )
        )
        session = res.scalar_one_or_none()
        if session is None:
            raise HTTPException(status_code=404, detail="会话不存在")

    # 2) 助手定位：会话绑定 > 显式指定 > 默认助手（UX-01）
    if session is not None:
        agent_id = session.agent_id
    elif body.assistant_id:
        agent_id = body.assistant_id
    else:
        from app.services.assistant.default_assistant import ensure_default_assistant

        default = await ensure_default_assistant(db, current_user.id, created_by=current_user.id)
        await db.commit()
        agent_id = default.id

    res = await db.execute(
        select(Agent).where(Agent.id == agent_id, Agent.is_active == True)  # noqa: E712
    )
    agent = res.scalar_one_or_none()
    if agent is None or agent.owner_id not in (None, current_user.id):
        raise HTTPException(status_code=404, detail="助手不存在")

    agent_config = await _load_agent_config(db, agent, use_memory=body.use_memory)
    # 会话内手动选模型：仅本次 Run 覆盖助手默认模型（不落版本快照）
    if body.model_config_id:
        agent_config["model_config_id"] = body.model_config_id
    # 权限模块：审批模式随 Run 创建写入配置（并落入 agent_config_snapshot，
    # 崩溃恢复/断点续跑后仍保持原模式）；非法取值一律回落 manual（安全默认）
    agent_config["approval_mode"] = (
        body.approval_mode if body.approval_mode in ("manual", "auto") else "manual"
    )
    # ensure_version 可能写入 agent_versions（flush 持写锁）：先提交释放，
    # 避免与 start_run_with_session 的独立会话写入互相阻塞（SQLite 必需）
    await db.commit()
    if any(
        key not in {"profile_version_no", "document_version_no"}
        or type(value) is not int or value < 1
        for key, value in body.task_options.items()
    ):
        raise HTTPException(status_code=422, detail="任务选项仅支持正整数的规范或文档版本号")
    try:
        attachment_refs = await asyncio.to_thread(
            resolve_attachment_refs, body.attachments, user_id=current_user.id,
            agent_id=agent_id, session_id=body.conversation_id,
        ) if body.attachments else []
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from None
    except OSError:
        raise HTTPException(status_code=404, detail="无法读取会话附件") from None

    task_request = None
    if body.task_mode != "auto" or body.document_profile_id or body.document_id or body.task_options or attachment_refs:
        task_request = {
            "task_mode": body.task_mode,
            "document_profile_id": body.document_profile_id,
            "document_id": body.document_id,
            "attachment_refs": attachment_refs,
            "task_options": body.task_options,
        }
    if body.team_mode:
        task_request = {**(task_request or {}), "team_mode": True}
    request_hash = compute_request_hash(
        endpoint="chat", user_id=current_user.id, agent_id=agent_id,
        session_id=body.conversation_id, user_input=body.message,
        agent_config=agent_config, task_request=task_request,
    )

    # 3) 幂等预查（P1-01）
    if idempotency_key:
        try:
            existing = await get_runtime_service().lookup_idempotent(
                current_user.id,
                idempotency_key,
                request_hash,
            )
        except IdempotencyConflict as e:
            raise HTTPException(status_code=409, detail=str(e))
        if existing is not None:
            response.headers["X-Run-Id"] = existing.id
            return {
                "run_id": existing.id,
                "conversation_id": existing.session_id,
                "status": existing.status,
                "idempotent_reuse": True,
                "events_url": f"/api/v2/runs/{existing.id}/events",
            }

    history = list(session.messages or []) if session is not None else []
    declared_refs = list(attachment_refs)
    current_word = word_candidates(attachment_refs)
    previous_word = recent_word_attachments(history) if session and not current_word else []
    execution_context = route_task(
        body.message, task_mode=body.task_mode,
        document_profile_id=body.document_profile_id, document_id=body.document_id,
        attachment_refs=attachment_refs,
        has_document_context=any(message.get("document_run_id") for message in history if isinstance(message, dict)),
        has_word_attachment=bool(previous_word),
    )
    inherited, anchor_run = [], None
    continued = None
    if (session and not body.team_mode and not attachment_refs
            and not (body.document_id or body.document_profile_id or body.task_options)
            and execution_context["task_type"] == "general_chat"):
        from app.services.agent_runtime.completion import resolve_completion_resume

        continued = await resolve_completion_resume(db, current_user.id, session.id, body.message, history)
    if continued:
        execution_context["completion_resume"] = continued
        execution_context["reason"] = "继续本会话未完成的文件交付"
    else:
        execution_context, inherited, anchor_run = document_turn(body.message, history, execution_context)
    if body.team_mode:
        from app.services.agent_runtime.team_project_intent import project_scope

        execution_context["team_mode"] = True
        scope = project_scope(
            body.message, body.task_mode, has_selected_document=bool(body.document_id or body.document_profile_id),
        )
        if scope and execution_context["task_type"] not in {"document_edit", "profile_extract"}:
            execution_context.pop("document_context", None)
            execution_context.update({"task_type": "software_project", "project_scope": scope,
                                      "reason": "用户要求实际构建可交付的软件项目"})
    context = execution_context.get("document_context")
    if context is not None:
        template_refs = current_word if is_template_reference(body.message) else []
        known = {ref["path"] for ref in attachment_refs}
        for ref in inherited if not template_refs else ():
            if ref["path"] in known:
                continue
            try:
                resolved = (await asyncio.to_thread(
                    resolve_attachment_refs, [ref], user_id=current_user.id,
                    agent_id=agent_id, session_id=body.conversation_id,
                ))[0]
            except (ValueError, OSError):
                continue
            attachment_refs.append(resolved)
            template_refs.append(resolved)
            known.add(resolved["path"])
        context["template_paths"] = [ref["path"] for ref in template_refs]
        if anchor_run:
            from app.models.document import RunInputRequest

            responses = (await db.scalars(select(RunInputRequest.response).join(
                AgentRun, AgentRun.id == RunInputRequest.run_id,
            ).where(AgentRun.id == anchor_run, AgentRun.user_id == current_user.id,
                    AgentRun.session_id == body.conversation_id, RunInputRequest.status == "submitted")
                .order_by(RunInputRequest.ordinal))).all()
            context["carried_inputs"] = {
                key: value for response in responses if isinstance(response, dict)
                for key, value in response.items()
            }
    execution_context["attachment_refs"] = attachment_refs
    if execution_context["task_type"] in DOCUMENT_GRAPH_TYPES or body.document_profile_id or body.document_id:
        from app.services.documents.errors import DocumentError
        from app.services.documents.repository import resolve_task_resources

        try:
            editing = execution_context["task_type"] == "document_edit" and not body.document_id
            candidates = (current_word or previous_word) if editing else []
            chosen = choose_word_attachment(body.message, candidates)
            prefer_attachment = bool(current_word and editing) or bool(chosen and any(
                term in body.message for term in ("原文件", "原附件", str(chosen.get("name") or chosen["path"]))
            ))
            resources = await resolve_task_resources(
                db, user_id=current_user.id, is_superuser=current_user.is_superuser,
                session_id=body.conversation_id, task_type=execution_context["task_type"],
                message=context["request"] if context else body.message,
                profile_id=body.document_profile_id, document_id=body.document_id,
                task_options=body.task_options, prefer_attachment=prefer_attachment,
                has_attachment=bool(candidates),
            )
            if editing and not resources.get("document_id") and candidates:
                if chosen is None:
                    raise DocumentError(422, "有多份 Word 可编辑，请在消息中写明一个文件名")
                if not current_word:
                    try:
                        chosen = (await asyncio.to_thread(
                            resolve_attachment_refs, [chosen], user_id=current_user.id,
                            agent_id=agent_id, session_id=body.conversation_id,
                        ))[0]
                    except (ValueError, OSError):
                        raise DocumentError(422, "之前的 Word 附件已不可用，请重新上传") from None
                resources["source_attachment"] = chosen
        except DocumentError as e:
            raise HTTPException(status_code=e.status_code, detail=e.message) from None
        execution_context.update(resources)
    # 附件注记随普通会话持久化；专用 Graph 从快照中的附件引用读取。
    note = "" if execution_context["task_type"] in DOCUMENT_GRAPH_TYPES else _attachment_note(attachment_refs)
    user_message = {"role": "user", "content": body.message + note}
    if declared_refs:
        user_message["attachments"] = [
            {key: ref[key] for key in ("name", "path", "text_path", "size") if key in ref}
            for ref in declared_refs
        ]
    messages = history + [user_message]
    try:
        db_conn = await check_source_bindings(db, current_user.id, agent_config)
    except SourceBindingError as e:
        raise HTTPException(status_code=404, detail=str(e)) from None
    db_info = {"name": db_conn.name, "db_type": db_conn.db_type} if db_conn is not None else None
    await db.commit()

    # 4) 启动运行（Session 创建 + 用户消息追加 + Run 创建同事务原子提交）
    try:
        run_id, conversation_id, stream = await get_runtime_service().start_run_with_session(
            agent_config=agent_config,
            messages=messages,
            user_id=current_user.id,
            session_id=body.conversation_id,
            idempotency_key=idempotency_key,
            db_info=db_info,
            endpoint="chat",
            user_message=user_message,
            execution_context=execution_context,
            request_hash=request_hash,
        )
    except IdempotencyConflict as e:
        raise HTTPException(status_code=409, detail=str(e))
    except SessionBusy as e:
        raise HTTPException(status_code=409, detail=str(e))

    # 执行由 Worker 独立驱动；旧契约事件流后台排空（v2 事实来源是持久化事件日志）
    asyncio.ensure_future(_drain_legacy_stream(stream))

    response.headers["X-Run-Id"] = run_id
    return {
        "run_id": run_id,
        "conversation_id": conversation_id,
        "status": "running",
        "events_url": f"/api/v2/runs/{run_id}/events",
    }


@router.get("/{run_id}")
async def get_run_status(
    run_id: str,
    current_user: User = Depends(get_current_user),
):
    """Run 状态（断线后重连 / 轮询用；恒读数据库，§8.4 5）。"""
    info = await _get_run_or_404(run_id, current_user.id)
    return info


@router.get("/{run_id}/events")
async def stream_run_events(
    run_id: str,
    after_seq: Optional[int] = Query(None),
    last_event_id: Optional[str] = Header(None),
    current_user: User = Depends(get_current_user),
):
    """v2 事件流订阅/回放（§7.1）：标准 SSE id:/event:/data: 帧。

    - ``Last-Event-ID``（或 after_seq）之前的事件不重复下发（§8.4 4/6）；
    - 事实来源为 agent_run_events，API 与执行 Worker 不同进程也可消费；
    - 到达终止事件（run.completed/failed/cancelled/suspended 或暂停边界）即结束。
    """
    from app.services.agent_runtime.event_protocol import replay_events_v2, sse_format

    await _get_run_or_404(run_id, current_user.id)
    if last_event_id and last_event_id.strip().isdigit():
        after_seq = int(last_event_id.strip())
    if after_seq is not None:
        from app.observability import metrics

        metrics.inc_sse_reconnect()

    async def event_stream():
        async for envelope in replay_events_v2(run_id, after_seq):
            yield sse_format(envelope)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "X-Run-Id": run_id},
    )


@router.post("/{run_id}/cancel")
async def cancel_run(
    run_id: str,
    body: RunCancelRequest = None,
    current_user: User = Depends(get_current_user),
):
    """取消 Run（§8.4 3）：取消意图先记录，Worker 确认后进入终态。

    在途外部工具结果不确定时不伪造取消成功（返回 cancelling 由 Worker 收敛）。
    """
    from app.services.agent_runtime import get_runtime_service

    await _get_run_or_404(run_id, current_user.id)
    result = await get_runtime_service().cancel_run(run_id, current_user.id)
    if not result.get("ok"):
        raise HTTPException(status_code=409, detail=result.get("error", "取消失败"))
    status = result.get("status")
    if status == "cancelling":
        message = "取消请求已记录，Worker 确认后进入终态"
    elif status == "cancelled":
        message = "运行已取消"
    else:
        message = f"运行已处于终态（{status}），未执行取消"
    return {"run_id": run_id, "status": status, "message": message}


@router.post("/{run_id}/resume")
async def resume_run(
    run_id: str,
    body: RunResumeRequest,
    current_user: User = Depends(get_current_user),
):
    """HITL 审批后恢复（§8.4 8）：决策携带稳定 action_id。

    恢复执行由 Worker 推进；事件流经 GET /runs/{id}/events 继续订阅。
    重复一致决策幂等；冲突 409；非法 400；越权 403（FR-04/F-11）。
    """
    from app.services.agent_runtime import get_runtime_service
    from app.services.agent_runtime.service import (
        ApprovalConflict,
        ApprovalForbidden,
        ApprovalInvalid,
    )

    info = await _get_run_or_404(run_id, current_user.id)
    if info.get("status") == "waiting_input":
        raise HTTPException(status_code=409, detail="运行正在等待业务输入")
    decisions = [
        {"type": d.type, "message": d.message, "action_id": d.action_id}
        for d in body.decisions
    ] or [{"type": "approve", "message": "", "action_id": None}]

    service = get_runtime_service()
    try:
        verdict = await service.check_decisions(run_id, decisions, current_user.id)
    except ApprovalForbidden as e:
        raise HTTPException(status_code=403, detail=str(e))
    except ApprovalInvalid as e:
        raise HTTPException(status_code=400, detail=str(e))
    except ApprovalConflict as e:
        raise HTTPException(status_code=409, detail=str(e))

    if info.get("status") != "waiting_approval":
        if verdict == "idempotent":
            return {
                "run_id": run_id,
                "status": info.get("status"),
                "idempotent": True,
                "message": "该审批决策已提交过，未重复执行",
            }
        raise HTTPException(status_code=409, detail="运行不在待审批状态")

    # 恢复驱动（决策校验/持久化/Claim 在 resume 流内完成；事件持久化后经 /events 消费）
    asyncio.ensure_future(_drain_legacy_stream(service.resume_run(run_id, decisions, current_user.id)))
    return {
        "run_id": run_id,
        "status": "running",
        "message": "已恢复执行",
        "events_url": f"/api/v2/runs/{run_id}/events",
    }


@router.post("/{run_id}/input")
async def submit_input(
    run_id: str,
    body: RunInputSubmission,
    current_user: User = Depends(get_current_user),
):
    """在同一 Run 中提交所需业务信息，并由 Worker 继续执行。"""
    from app.services.agent_runtime.run_input import RunInputError, submit_run_input

    try:
        result = await submit_run_input(run_id, current_user.id, body.request_id, body.values)
    except RunInputError as e:
        raise HTTPException(status_code=e.status_code, detail=str(e)) from None
    return {**result, "events_url": f"/api/v2/runs/{run_id}/events"}


@router.post("/{run_id}/retry")
async def retry_run(
    run_id: str, current_user: User = Depends(get_current_user),
    failed_event_seq: Optional[int] = Header(None, alias="X-Document-Failure-Seq"),
):
    from app.services.agent_runtime import get_runtime_service
    from app.services.agent_runtime.document_retry import retry_document_run
    from app.services.documents.errors import DocumentError

    try:
        result = await retry_document_run(
            get_runtime_service(), run_id, current_user.id, expected_sequence=failed_event_seq,
        )
    except DocumentError as exc:
        raise HTTPException(exc.status_code, exc.message) from None
    return {**result, "events_url": f"/api/v2/runs/{run_id}/events"}


