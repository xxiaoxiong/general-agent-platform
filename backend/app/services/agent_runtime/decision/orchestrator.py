"""Run 内决策编排：上下文构建 → 模型决策 → 校验 → 执行准备 → 快照持久化。

调用位置在 Run 已创建并领取租约之后（service/stream.py），因此决策具备
Run ID、事件日志、Trace 与恢复能力；显式模式/资源操作不会进入这里。
"""
from __future__ import annotations

import logging
from typing import Any, Iterable

from app.core.config import settings
from app.observability.langfuse_tracing import platform_span

from .apply import apply_decision
from .context import DecisionContext, build_decision_context
from .persistence import (
    load_input_payload, mark_message_document_run, persist_execution_context,
)
from .preparer import prepare_execution
from .service import decide
from .validator import ValidatedDecision, validate_decision

logger = logging.getLogger(__name__)


def should_defer_semantic(
    mode: str,
    *,
    task_mode: str,
    document_id: int | None = None,
    document_profile_id: int | None = None,
    task_options: dict[str, Any] | None = None,
    team_mode: bool = False,
) -> bool:
    """自动模式一律把语义决策延迟到 Run 内执行（含新会话第一条消息）。

    显式任务模式、显式文档资源、显式团队模式是用户的确定性指令，直接执行；
    其余都交给 Run 内决策器，避免关键词规则把「查查最新进展」这类时效任务
    误判为普通对话（决策因此具备 Run ID、事件、Trace 与恢复能力）。
    """
    return bool(
        mode in {"shadow", "llm"}
        and task_mode == "auto"
        and not document_id and not document_profile_id
        and not task_options and not team_mode
    )


def _turns(messages: Iterable[dict[str, Any]] | None) -> list[dict[str, Any]]:
    return [dict(item) for item in (messages or []) if isinstance(item, dict)]


def _current_and_history(messages: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
    index = next(
        (position for position in range(len(messages) - 1, -1, -1)
         if messages[position].get("role") == "user"),
        None,
    )
    if index is None:
        return "", messages
    content = str(messages[index].get("content") or "").split("\n\n[附件]", 1)[0].strip()
    return content, messages[:index]


def _tool_names(agent_config: dict[str, Any]) -> list[str]:
    names: list[str] = []
    for item in agent_config.get("tools") or []:
        name = item.get("name") if isinstance(item, dict) else item
        if name:
            names.append(str(name))
    return names


def _effective_route(task_type: str | None) -> str:
    if task_type in {"document_generate", "document_edit", "profile_extract"}:
        return "document_graph"
    if task_type == "software_project":
        return "team_project_graph"
    return "deep_agent"


def _rules_snapshot(
    execution_context: dict[str, Any], *, mode: str, latency_ms: int = 0,
    input_tokens: int = 0, output_tokens: int = 0,
    error: str = "", notes: list[str] | None = None,
) -> dict[str, Any]:
    task_type = execution_context.get("task_type")
    return {
        "schema_version": "v1",
        "decision_source": "rules",
        "mode": mode,
        "applied": True,
        "intent": task_type or "unknown",
        "route": _effective_route(task_type),
        "effective_task_type": execution_context.get("task_type"),
        "continuity": "new_task",
        "requires_action": False,
        "decision_status": "clear",
        "context_refs": [],
        "capability_suggestions": [],
        "goal": "",
        "constraints": [],
        "deliverables": [],
        "acceptance_criteria": [],
        "latency_ms": latency_ms,
        "model_id": "",
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "fallback": True,
        "degraded_reason": error,
        "notes": list(notes or []),
    }


def _decision_snapshot(
    validated: ValidatedDecision,
    *,
    mode: str,
    applied: bool,
    effective: dict[str, Any],
    latency_ms: int,
    model_id: str,
    input_tokens: int,
    output_tokens: int,
    repair_attempted: bool,
    notes: list[str] | None = None,
) -> dict[str, Any]:
    decision = validated.decision
    return {
        "schema_version": decision.schema_version,
        "decision_source": "llm",
        "mode": mode,
        "applied": applied,
        "intent": validated.intent,
        "route": validated.route,
        "effective_task_type": effective.get("task_type"),
        "effective_route": _effective_route(effective.get("task_type")),
        "continuity": decision.continuity,
        "decision_status": decision.decision_status,
        "context_refs": list(validated.valid_refs),
        "capability_suggestions": list(validated.allowed_capabilities),
        "goal": decision.goal,
        "constraints": list(decision.constraints),
        "deliverables": list(decision.deliverables),
        "acceptance_criteria": list(decision.acceptance_criteria),
        "project_scope": decision.project_scope,
        "requires_action": bool(decision.requires_action),
        "clarification_question": validated.clarification_question,
        "rationale": decision.rationale,
        "deliverable_format": decision.deliverable_format,
        "edit_scope": decision.edit_scope,
        "target": decision.target,
        "latency_ms": latency_ms,
        "model_id": model_id,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "repair_attempted": repair_attempted,
        "fallback": False,
        "notes": list(notes or []),
    }


def _degrade_to_chat(route: dict[str, Any], reason: str) -> dict[str, Any]:
    degraded = {
        **route,
        "task_type": "general_chat",
        "document_id": None,
        "document_version": None,
        "style_edit": False,
        "reason": reason,
    }
    degraded.pop("document_context", None)
    return degraded


async def resolve_run_decision(
    *,
    run_id: str,
    session_id: int | None,
    user_id: int,
    execution_context: dict[str, Any],
    messages: list[dict[str, Any]] | None,
    agent_config: dict[str, Any],
    provider: dict[str, Any],
    model_config: dict[str, Any],
    mode: str | None = None,
    auto_team: bool = False,
    tools: list[str] | None = None,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """返回 (最终执行上下文, 决策事件数据)。非决策 Run 原样返回。"""
    mode = mode or settings.AGENT_DECISION_MODE
    if mode not in {"shadow", "llm"}:
        # 运行中切换为 off（回滚）：按规则路线执行并清理未消费的决策输入
        execution_context.pop("decision_pending", None)
        execution_context.pop("decision_input", None)
        return execution_context, None
    if not execution_context.get("decision_pending"):
        return execution_context, None
    if execution_context.get("decision"):
        execution_context.pop("decision_pending", None)
        return execution_context, None

    decision_input = dict(execution_context.get("decision_input") or {})
    turns = _turns(messages)
    if not turns:
        payload = await load_input_payload(run_id)
        turns = _turns(payload.get("messages"))
    message, history = _current_and_history(turns)
    message = message or str(decision_input.get("message") or "")
    history = history or list(decision_input.get("history") or [])

    with platform_span(
        "decision.context_build", run_id=run_id,
        attributes={"history_turns": len(history), "attachment_count": len(
            decision_input.get("attachments") or []
        )},
    ):
        context = build_decision_context(
            message,
            history=history,
            artifacts=decision_input.get("artifacts") or [],
            recent_runs=decision_input.get("recent_runs") or [],
            preview_document=decision_input.get("preview_document"),
            attachments=decision_input.get("attachments") or [],
            capabilities=agent_config.get("capability_ids") or [],
            # 决策器看到的必须是运行时真实工具面（能力包解析 + 联网开关收窄/补齐后）
            tools=tools if tools is not None else _tool_names(agent_config),
            explicit=decision_input.get("explicit") or {},
        )

    outcome = await decide(
        context, provider=provider, model_config=model_config, run_id=run_id,
    )
    notes: list[str] = []
    validated: ValidatedDecision | None = None
    with platform_span("decision.validation", run_id=run_id):
        if outcome.decision is not None:
            validated = validate_decision(
                outcome.decision, context=context, auto_team=auto_team,
                user_format=execution_context.get("deliverable_format"),
            )
            notes.extend(validated.notes)

    rules = {key: value for key, value in execution_context.items()
             if key not in {"decision_pending", "decision_input", "decision"}}
    applied = mode == "llm" and validated is not None
    if applied:
        route = apply_decision(validated, rules, context=context)
        allow_upgrade = validated.allow_upgrade
    else:
        route = rules
        allow_upgrade = True

    async def _prepare(candidate: dict[str, Any], upgrade: bool):
        from app.core.database import AsyncSessionLocal

        async with AsyncSessionLocal() as db:
            return await prepare_execution(
                db,
                message=message, history=history, route=candidate,
                user_id=user_id, agent_id=agent_config.get("id") or 0,
                session_id=session_id,
                is_superuser=bool(decision_input.get("is_superuser")),
                attachment_refs=rules.get("attachment_refs") or [],
                document_id=decision_input.get("document_id"),
                document_profile_id=decision_input.get("document_profile_id"),
                task_options=decision_input.get("task_options") or {},
                allow_upgrade=upgrade,
                artifacts=decision_input.get("artifacts") or [],
            )

    try:
        prepared = await _prepare(route, allow_upgrade)
    except Exception as exc:  # noqa: BLE001 文档资源不可用时安全降级，不误杀 Run
        from app.services.documents.errors import DocumentError

        if not isinstance(exc, DocumentError):
            raise
        notes.append(f"资源解析失败，已降级为通用对话：{exc.message}")
        logger.info("run %s 决策后资源解析失败，降级为对话：%s", run_id, exc.message)
        prepared = await _prepare(
            _degrade_to_chat(route, "本轮没有可用的文档资源，按对话处理"),
            allow_upgrade=False,
        )

    execution_context = prepared.execution_context
    execution_context.pop("decision_pending", None)
    execution_context.pop("decision_input", None)
    if validated is not None:
        snapshot = _decision_snapshot(
            validated, mode=mode, applied=applied, effective=execution_context,
            latency_ms=outcome.latency_ms, model_id=outcome.model_id,
            input_tokens=outcome.input_tokens, output_tokens=outcome.output_tokens,
            repair_attempted=outcome.repair_attempted, notes=notes,
        )
    else:
        snapshot = _rules_snapshot(
            execution_context, mode=mode, latency_ms=outcome.latency_ms,
            input_tokens=outcome.input_tokens, output_tokens=outcome.output_tokens,
            error=outcome.error, notes=notes,
        )
    execution_context["decision"] = snapshot

    await persist_execution_context(run_id, execution_context)
    if (session_id and decision_input.get("message_id")
            and execution_context.get("task_type") in {
                "document_generate", "document_edit", "profile_extract",
            }):
        try:
            await mark_message_document_run(
                session_id, str(decision_input["message_id"]), run_id,
            )
        except Exception:  # noqa: BLE001 标记失败不影响执行
            logger.warning("run %s 写入文档消息标记失败（忽略）", run_id)
    return execution_context, snapshot
