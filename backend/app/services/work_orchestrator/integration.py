"""Work Orchestrator 与现有 Run 执行入口的集成辅助。

只被 ``service/stream.py::_drive`` 调用；负责：
- 复合任务判定 → Planner → Validator → 计划持久化/事件（shadow/auto）；
- 构建步骤执行环境并驱动编排图；
- 把编排结果转换为现有 Run 收尾所需的 verification / final_text / summary。
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Iterable

from app.core.config import settings
from app.observability import metrics

from .complexity import is_composite
from .graph import WorkOrchestrator
from .persistence import persist_work_plan_diagnostic, persist_work_plan_record
from .planner import plan_work
from .registry import ExecutorRegistry
from .schemas import (
    WorkPlanRecord, WorkStepOutcome, WorkVerificationResult, new_plan_record,
    plan_record_from_snapshot,
)
from .validator import candidate_refs_from_context, validate_plan
from .adapters.base import StepRuntimeEnv

logger = logging.getLogger(__name__)

_WORK_MODES = {"shadow", "auto"}


@dataclass
class WorkDriverResult:
    executed: bool = False
    paused: bool = False
    record: WorkPlanRecord | None = None
    final_text: str = ""
    verification: dict[str, Any] | None = None
    summary: dict[str, Any] = field(default_factory=dict)


def _message_and_history(messages: Iterable[dict[str, Any]] | None) -> tuple[str, list[dict[str, Any]]]:
    turns = [item for item in (messages or []) if isinstance(item, dict)]
    index = next(
        (position for position in range(len(turns) - 1, -1, -1)
         if turns[position].get("role") == "user"),
        None,
    )
    if index is None:
        return "", turns
    message = str(turns[index].get("content") or "").split("\n\n[附件]", 1)[0].strip()
    return message, turns[:index]


def _diagnostic(reason: str, *, mode: str, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "type": "work_plan_degraded",
        "mode": mode,
        "reason": reason[:500],
        "at": time.time(),
        **(extra or {}),
    }


async def prepare_plan(
    *,
    service: Any,
    run: Any,
    ctx: Any,
    bus: Any,
    provider: dict[str, Any],
    model_config: dict[str, Any],
    tool_specs: list[dict[str, Any]],
    execution_context: dict[str, Any],
    messages: Iterable[dict[str, Any]] | None,
    decision_input: dict[str, Any] | None = None,
    mode: str | None = None,
    auto_team: bool = False,
    explicit_team: bool = False,
    flush_budget: Any = None,
) -> WorkPlanRecord | None:
    """返回可执行的 auto 计划；shadow/降级/不复合返回 None（原路线继续）。"""
    mode = str(mode or settings.AGENT_WORK_ORCHESTRATOR_MODE)
    existing = plan_record_from_snapshot(execution_context.get("work_plan"))
    if existing is not None and existing.mode == "auto":
        # 已持久化计划：恢复时直接复用，不重新规划
        metrics.inc_work_plan_recovery("plan_reused")
        return existing
    if existing is not None and existing.mode == "shadow":
        return None
    if mode not in _WORK_MODES:
        return None
    if int(getattr(run, "attempt", 1) or 1) > 1:
        # 恢复中的 Run 不临时改路线：只有已持久化的 auto 计划会被复用（见上）
        return None
    if ctx.goal_mode or execution_context.get("team_mode") or execution_context.get("completion_resume"):
        return None
    if execution_context.get("task_type") in {"document_edit", "document_analyze", "profile_extract"}:
        return None

    message, history = _message_and_history(messages)
    if not message:
        return None
    decision = execution_context.get("decision") or {}
    verdict = is_composite(
        message=message, decision=decision,
        execution_context=execution_context, goal_mode=bool(ctx.goal_mode),
    )
    if not verdict.composite:
        return None

    agent_config = ctx.agent_config or {}
    registry = ExecutorRegistry(
        tool_names=[spec["name"] for spec in tool_specs],
        capability_ids=agent_config.get("capability_ids") or [],
        auto_team=auto_team,
        explicit_team=explicit_team,
        database_bound=bool(agent_config.get("database_connection_id")),
        web_enabled=agent_config.get("web_enabled") is not False,
    )
    if len(registry.allowed_profiles()) < 2:
        return None

    resources = decision_input or {}
    def meter(_input_tokens: int, output_tokens: int) -> None:
        ctx.model_calls += 1
        ctx.steps += 1
        ctx.tokens_generated += max(0, int(output_tokens or 0))

    outcome = await plan_work(
        message=message,
        decision=decision,
        history=history,
        artifacts=resources.get("artifacts") or [],
        attachments=resources.get("attachments") or [],
        preview_document=resources.get("preview_document"),
        registry=registry,
        provider=provider,
        model_config=model_config,
        run_id=run.run_id,
        extra_constraints=execution_context.get("decision_constraints") or [],
        meter=meter,
    )
    metrics.observe_work_plan_generation(
        outcome.latency_ms / 1000.0, mode=mode,
        outcome="fallback" if outcome.fallback else "ok",
    )
    metrics.inc_work_plan_model_overhead("planner")
    if flush_budget is not None:
        try:
            await flush_budget(ctx, run)
        except Exception:  # noqa: BLE001 预算落库失败由后续执行兜底
            logger.warning("run %s 规划预算落库失败（忽略）", run.run_id, exc_info=True)

    if outcome.plan is None:
        metrics.inc_work_plan_fallback("planner_fallback")
        reason = outcome.error or "规划失败"
        await _publish_degraded(service, run, bus, reason=reason, mode=mode)
        await persist_work_plan_diagnostic(
            run.run_id, _diagnostic(reason, mode=mode), lease=run.execution_lease(),
        )
        return None

    refs = candidate_refs_from_context(
        history=history,
        artifacts=resources.get("artifacts") or [],
        attachments=resources.get("attachments") or [],
        preview_document=resources.get("preview_document"),
    )
    validated = validate_plan(
        outcome.plan, registry=registry, context_refs=refs, user_message=message,
        max_steps=settings.AGENT_WORK_MAX_STEPS,
    )
    if not validated.valid:
        metrics.inc_work_plan_invalid("schema_or_security")
        reason = "；".join(validated.errors[:4]) or "计划校验失败"
        await _publish_degraded(service, run, bus, reason=reason, mode=mode)
        await persist_work_plan_diagnostic(
            run.run_id,
            _diagnostic(reason, mode=mode, extra={"errors": validated.errors[:8]}),
            lease=run.execution_lease(),
        )
        return None
    if len(outcome.plan.steps) <= 1:
        metrics.inc_work_plan_fallback("single_step_plan")
        await _publish_degraded(
            service, run, bus, reason="计划只有一个步骤，直接使用原执行路径", mode=mode,
        )
        return None

    record = new_plan_record(outcome.plan, mode="shadow" if mode == "shadow" else "auto")
    if mode == "shadow":
        try:
            await persist_work_plan_record(run.run_id, record, lease=run.execution_lease())
        except Exception:  # noqa: BLE001
            logger.warning("run %s shadow WorkPlan 持久化失败（忽略）", run.run_id, exc_info=True)
        execution_context["work_plan"] = record.model_dump(mode="json")
        await _publish_plan_created(service, run, bus, record)
        metrics.observe_work_plan_steps(record.step_count)
        logger.info(
            "run %s WorkPlan shadow：%s 个步骤，耗时 %sms",
            run.run_id, record.step_count, outcome.latency_ms,
        )
        return None

    await persist_work_plan_record(run.run_id, record, lease=run.execution_lease())
    execution_context["work_plan"] = record.model_dump(mode="json")
    await _publish_plan_created(service, run, bus, record)
    metrics.observe_work_plan_steps(record.step_count)
    return record


async def _publish_plan_created(service: Any, run: Any, bus: Any, record: WorkPlanRecord) -> None:
    from app.services.agent_runtime.events import RuntimeEvent

    await bus.publish(RuntimeEvent(
        type="work_plan_created", run_id=run.run_id,
        data={
            **record.plan_summary(),
            "objective": record.plan.objective,
            "constraints": list(record.plan.constraints),
            "deliverables": list(record.plan.deliverables),
            "acceptance_criteria": list(record.plan.acceptance_criteria),
            "steps": [
                {
                    **step,
                    "status": (record.steps.get(step["id"]).status
                               if record.steps.get(step["id"]) else "pending"),
                }
                for step in record.plan_summary()["steps"]
            ],
        },
    ))


async def _publish_degraded(service: Any, run: Any, bus: Any, *, reason: str, mode: str) -> None:
    from app.services.agent_runtime.events import RuntimeEvent

    try:
        await bus.publish(RuntimeEvent(
            type="work_plan_degraded", run_id=run.run_id,
            data={"reason": reason[:500], "mode": mode, "fallback_route": "existing_decision_route"},
        ))
    except Exception:  # noqa: BLE001 事件失败不影响回退
        logger.warning("run %s work.plan.degraded 事件发布失败", run.run_id, exc_info=True)


async def drive_plan(
    *,
    service: Any,
    run: Any,
    ctx: Any,
    bus: Any,
    provider: dict[str, Any],
    model_config: dict[str, Any],
    policy: Any,
    tool_specs: list[dict[str, Any]],
    checkpointer: Any,
    store: Any,
    execution_context: dict[str, Any],
    messages: Iterable[dict[str, Any]] | None,
    record: WorkPlanRecord,
    resume: dict[str, Any] | None = None,
) -> WorkDriverResult:
    env = StepRuntimeEnv(
        service=service, run=run, ctx=ctx, bus=bus,
        provider=provider, model_config=model_config,
        agent_config=ctx.agent_config or {}, policy=policy, tool_specs=tool_specs,
        checkpointer=checkpointer, store=store,
        route=execution_context, messages=list(messages) if messages else None,
        user_id=run.user_id,
        auto_team=bool(getattr(settings, "AGENT_AUTO_TEAM_ENABLED", False)),
        explicit_team=bool(execution_context.get("team_mode")),
    )
    orchestrator = WorkOrchestrator(env)
    final = await orchestrator.drive(record, resume=resume)
    last = orchestrator.last_result
    if final.status == "paused":
        return WorkDriverResult(executed=True, paused=True, record=final)

    verification_payload = last.get("final_verification") or (
        final.final_verification.model_dump(mode="json") if final.final_verification else None
    )
    verification = _to_run_verification(service, run, ctx, final, verification_payload)
    outcomes = {
        step_id: WorkStepOutcome.model_validate(payload)
        for step_id, payload in (last.get("outcomes") or {}).items()
        if isinstance(payload, dict)
    }
    final_text = str(last.get("final_text") or "")
    if not final_text:
        final_text = _fallback_final_text(final, outcomes)
    run.final_text_parts = [final_text]
    return WorkDriverResult(
        executed=True, paused=False, record=final,
        final_text=final_text, verification=verification,
        summary=last.get("summary") or {},
    )


def _fallback_final_text(record: WorkPlanRecord, outcomes: dict[str, WorkStepOutcome]) -> str:
    from .verification import compose_final_text

    return compose_final_text(record, outcomes)


def _to_run_verification(
    service: Any,
    run: Any,
    ctx: Any,
    record: WorkPlanRecord,
    payload: dict[str, Any] | None,
) -> dict[str, Any]:
    if not payload:
        payload = {
            "status": "failed", "checks": [], "artifact_refs": [],
            "stop_reason": "step_failed",
        }
    result = WorkVerificationResult.model_validate(payload)
    checks = [item.model_dump(mode="json") for item in result.checks]
    artifacts = [item.model_dump(mode="json") for item in result.artifact_refs]
    verification: dict[str, Any] = {
        "version": 2,
        "status": result.status,
        "checks": checks,
        "artifacts": artifacts,
        "fix_rounds": record.repairs,
        "stop_reason": _stop_reason(record, result),
        "work_plan": {
            "plan_id": record.plan.plan_id,
            "revision": record.plan.revision,
            "step_count": record.step_count,
            "completed_count": record.completed_count,
        },
    }
    return verification


def _stop_reason(record: WorkPlanRecord, result: WorkVerificationResult) -> str:
    if result.stop_reason:
        return result.stop_reason
    for state in record.steps.values():
        if state.error_code == "budget_exceeded":
            return "budget"
        if state.error_code == "policy_rejected":
            return "policy"
    return ""


def outcome_summary(
    result: WorkDriverResult,
) -> dict[str, Any]:
    record = result.record
    if record is None:
        return {}
    return {
        "plan_id": record.plan.plan_id,
        "revision": record.plan.revision,
        "mode": record.mode,
        "status": record.status,
        "step_count": record.step_count,
        "completed_count": record.completed_count,
        "repairs": record.repairs,
        "steps": [
            {
                "id": step.id, "title": step.title,
                "executor_profile": step.executor_profile,
                "status": (record.steps.get(step.id).status if record.steps.get(step.id) else "pending"),
                "summary": (record.steps.get(step.id).summary if record.steps.get(step.id) else ""),
            }
            for step in record.plan.steps
        ],
    }
