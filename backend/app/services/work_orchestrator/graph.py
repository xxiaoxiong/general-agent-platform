"""Work Orchestrator Graph：步骤调度、依赖传递、恢复、有限修复与最终验收。

任务书 §7.1 / §8：
- 一个用户任务仍然只有一个 AgentRun / RunWorker / Lease / EventBus；
- 编排状态保存于 ``work:{run_id}`` Checkpoint，步骤执行器使用步骤级线程；
- 初期跨执行器串行（``AGENT_WORK_MAX_PARALLEL_STEPS=1``），Team Graph 内部并行不变；
- 计划/步骤事件写入现有事件日志，前端实时与历史回放共用同一归约器。
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from app.core.config import settings
from app.observability import metrics

from .adapters.base import StepRuntimeEnv
from .adapters.deep_agent import DeepAgentAdapter
from .adapters.document import DocumentAdapter
from .adapters.team import ProjectAdapter, TeamAdapter
from .persistence import persist_work_plan_record
from .schemas import (
    WorkPlanRecord, WorkStepOutcome, WorkStepState,
)
from .verification import compose_final_text, final_verify, plan_digest

logger = logging.getLogger(__name__)

_ADAPTERS = {
    "general_agent": DeepAgentAdapter(),
    "research": DeepAgentAdapter(),
    "data_analysis": DeepAgentAdapter(),
    "database": DeepAgentAdapter(),
    "coding": DeepAgentAdapter(),
    "business_action": DeepAgentAdapter(),
    "document_docx": DocumentAdapter(),
    "team": TeamAdapter(),
    "software_project": ProjectAdapter(),
}


class OrchestratorState(TypedDict, total=False):
    record: dict[str, Any]
    outcomes: dict[str, dict[str, Any]]
    outputs: dict[str, str]
    summaries: dict[str, str]
    resume_step_id: str | None
    resume_payload: dict[str, Any] | None
    current_step_id: str | None
    paused: bool
    final_text: str
    final_verification: dict[str, Any]
    summary: dict[str, Any]


def work_thread(run_id: str) -> str:
    return f"work:{run_id}"


def _record(state: OrchestratorState) -> WorkPlanRecord:
    return WorkPlanRecord.model_validate(state["record"])


def _dump(record: WorkPlanRecord) -> dict[str, Any]:
    record.updated_at = datetime.utcnow()
    return record.model_dump(mode="json")


class WorkOrchestrator:
    def __init__(self, env: StepRuntimeEnv) -> None:
        self.env = env
        self.run = env.run
        self.graph = self._build_graph()
        # 编排图只使用 Run 级线程；不要把 run_id/user_id 放进 configurable：
        # LangGraph 会据此复用同一执行身份，导致暂停后重新 invoke 不调度新节点。
        self.config = {
            "configurable": {"thread_id": work_thread(self.run.run_id)},
            "recursion_limit": 200,
        }
        self._resumed = False

    # ── 事件 ────────────────────────────────────────────────────────────

    async def _publish(self, event_type: str, payload: dict[str, Any]) -> None:
        from app.services.agent_runtime.events import RuntimeEvent

        await self.env.bus.publish(RuntimeEvent(
            type=event_type, run_id=self.run.run_id, data=payload,
        ))

    async def _persist(self, record: WorkPlanRecord) -> None:
        # 暂停态（waiting_approval/input）不强制 lease 校验：权威状态在
        # AgentRun 与 Checkpoint；运行态写入必须带 Fencing。
        lease = self.run.execution_lease() if self.run.status == "running" else None
        try:
            await persist_work_plan_record(self.run.run_id, record, lease=lease)
        except Exception:  # noqa: BLE001 计划快照不是执行权威，失败不阻断编排
            logger.warning("run %s WorkPlan 快照持久化失败（继续使用 Checkpoint）", self.run.run_id,
                           exc_info=True)

    def _plan_payload(self, record: WorkPlanRecord, **extra: Any) -> dict[str, Any]:
        return {**record.plan_summary(), **extra}

    # ── 图节点 ──────────────────────────────────────────────────────────

    async def select_step(self, state: OrchestratorState) -> dict[str, Any]:
        record = _record(state)
        if record.status in {"completed", "failed", "cancelled"}:
            return {"current_step_id": None, "paused": False}
        # 1) 恢复：优先继续 waiting_approval / running 的步骤
        recovering = next(
            (step for step in record.plan.steps
             if (record.steps.get(step.id) or WorkStepState()).status in {"waiting_approval", "running"}),
            None,
        )
        if recover := recovering:
            metrics.inc_work_plan_recovery("resume_step")
            step = recover
        else:
            step = next(iter(record.ready_steps()), None)
        if step is None:
            record.status = "verifying"
            await self._persist(record)
            await self._publish(
                "work_plan_updated", self._plan_payload(record, status="verifying"),
            )
            return {"record": _dump(record), "current_step_id": None, "paused": False}

        step_state = record.steps.get(step.id) or WorkStepState()
        if step_state.status == "completed":
            metrics.inc_work_plan_duplicate_execution()
            return {"current_step_id": None, "paused": False}
        step_state.status = "running"
        step_state.attempts += 1
        step_state.started_at = datetime.utcnow()
        record.steps[step.id] = step_state
        record.current_step_id = step.id
        record.status = "running"
        await self._persist(record)
        await self._publish(
            "work_plan_updated", self._plan_payload(record),
        )
        await self._publish("work_step_started", {
            "plan_id": record.plan.plan_id,
            "revision": record.plan.revision,
            "step_id": step.id,
            "title": step.title,
            "executor_profile": step.executor_profile,
            "depends_on": list(step.depends_on),
            "attempt": step_state.attempts,
            "status": "running",
        })
        return {
            "record": _dump(record),
            "current_step_id": step.id,
            "paused": False,
        }

    async def execute_step(self, state: OrchestratorState) -> dict[str, Any]:
        record = _record(state)
        step_id = state.get("current_step_id")
        step = record.step(step_id or "")
        if step is None:
            return {"paused": False}
        adapter = _ADAPTERS[step.executor_profile]
        self.env.step_outputs = dict(state.get("outputs") or {})
        self.env.step_summaries = dict(state.get("summaries") or {})
        resume_payload = None
        if state.get("resume_step_id") == step.id:
            resume_payload = state.get("resume_payload")
        outcome = await adapter.execute(
            step, record, self.env,
            resume=resume_payload,
            attempt=(record.steps.get(step.id) or WorkStepState()).attempts,
        )
        outcomes = dict(state.get("outcomes") or {})
        outcomes[step.id] = outcome.model_dump(mode="json")
        outputs = dict(state.get("outputs") or {})
        summaries = dict(state.get("summaries") or {})
        if outcome.final_text:
            outputs[step.id] = outcome.final_text[:24_000]
        if outcome.summary:
            summaries[step.id] = outcome.summary
        return {
            "record": state["record"], "outcomes": outcomes,
            "outputs": outputs, "summaries": summaries,
            "paused": outcome.paused,
            "resume_step_id": None, "resume_payload": None,
        }

    async def verify_step(self, state: OrchestratorState) -> dict[str, Any]:
        record = _record(state)
        step_id = state.get("current_step_id") or ""
        step = record.step(step_id)
        outcome_raw = (state.get("outcomes") or {}).get(step_id)
        if step is None or outcome_raw is None:
            return {"current_step_id": None, "paused": False}
        outcome = WorkStepOutcome.model_validate(outcome_raw)
        step_state = record.steps.get(step_id) or WorkStepState()
        metrics.observe_work_step(step.executor_profile, outcome.status)

        if outcome.paused:
            step_state.status = "waiting_approval"
            record.steps[step_id] = step_state
            record.status = "paused"
            # 暂停态不写 input_payload（Run 状态与 Checkpoint 是权威）；仅发布事件
            await self._publish(
                "work_plan_updated", self._plan_payload(record, status="paused"),
            )
            return {"record": _dump(record), "current_step_id": None, "paused": True}

        step_state.finished_at = datetime.utcnow()
        step_state.summary = outcome.summary
        step_state.artifact_refs = list(outcome.artifact_refs)
        step_state.evidence_refs = list(outcome.evidence_refs)
        step_state.verification = outcome.verification
        step_state.error_code = outcome.error_code
        if outcome.status == "completed":
            step_state.status = "completed"
            await self._publish("work_step_verification", {
                "plan_id": record.plan.plan_id, "revision": record.plan.revision,
                "step_id": step.id, "title": step.title,
                **outcome.verification.model_dump(mode="json"),
            })
            await self._publish("work_step_completed", {
                "plan_id": record.plan.plan_id, "revision": record.plan.revision,
                "step_id": step.id, "title": step.title,
                "executor_profile": step.executor_profile,
                "status": "completed", "summary": outcome.summary,
                "artifact_refs": [item.model_dump(mode="json") for item in outcome.artifact_refs],
                "verification_status": outcome.verification.status,
            })
        else:
            step_state.status = (
                "needs_reconciliation"
                if outcome.verification.stop_reason == "external_operation"
                else "failed"
            )
            await self._publish("work_step_verification", {
                "plan_id": record.plan.plan_id, "revision": record.plan.revision,
                "step_id": step.id, "title": step.title,
                **outcome.verification.model_dump(mode="json"),
            })
            await self._publish("work_step_failed", {
                "plan_id": record.plan.plan_id, "revision": record.plan.revision,
                "step_id": step.id, "title": step.title,
                "executor_profile": step.executor_profile,
                "status": step_state.status, "message": outcome.summary,
                "error_code": outcome.error_code, "retryable": outcome.retryable,
            })
            max_replans = int(getattr(settings, "AGENT_WORK_MAX_REPLANS", 1))
            repairable = (
                outcome.retryable
                and outcome.verification.stop_reason != "external_operation"
            )
            if repairable and record.repairs < max_replans:
                record.repairs += 1
                step_state.status = "pending"
                record.diagnostics = [
                    *record.diagnostics[-5:],
                    f"步骤 {step.id} 第 {record.repairs} 次有限修复",
                ]
                await self._publish(
                    "work_plan_updated", self._plan_payload(record, status="running"),
                )
            else:
                self._skip_dependents(record, step.id)
        record.steps[step_id] = step_state
        await self._persist(record)
        return {"record": _dump(record), "current_step_id": None, "paused": False}

    async def final_verify_node(self, state: OrchestratorState) -> dict[str, Any]:
        record = _record(state)
        outcomes = {
            step_id: WorkStepOutcome.model_validate(payload)
            for step_id, payload in (state.get("outcomes") or {}).items()
            if isinstance(payload, dict)
        }
        result = final_verify(record, outcomes)
        record.final_verification = result
        record.status = "completed" if result.status == "passed" else "failed"
        record.completed_at = datetime.utcnow()
        record.current_step_id = None
        await self._persist(record)
        await self._publish("work_plan_completed", {
            **self._plan_payload(record),
            "completed_count": record.completed_count,
            "step_count": record.step_count,
            "verification": result.model_dump(mode="json"),
        })
        final_text = compose_final_text(record, outcomes)
        return {
            "record": _dump(record),
            "final_verification": result.model_dump(mode="json"),
            "final_text": final_text[:60_000],
        }

    async def deliver(self, state: OrchestratorState) -> dict[str, Any]:
        record = _record(state)
        summary = {
            "work_plan": plan_digest(record),
            "verification": state.get("final_verification") or {},
        }
        return {"summary": summary}

    # ── 图装配与驱动 ─────────────────────────────────────────────────────

    def _build_graph(self):
        graph = StateGraph(OrchestratorState)
        graph.add_node("select_step", self.select_step)
        graph.add_node("execute_step", self.execute_step)
        graph.add_node("verify_step", self.verify_step)
        graph.add_node("final_verify", self.final_verify_node)
        graph.add_node("deliver", self.deliver)
        graph.add_edge(START, "select_step")
        graph.add_conditional_edges(
            "select_step", lambda state: "execute_step" if state.get("current_step_id") else "final_verify",
            ["execute_step", "final_verify"],
        )
        graph.add_edge("execute_step", "verify_step")
        graph.add_conditional_edges(
            "verify_step", lambda state: END if state.get("paused") else "select_step",
            ["select_step", END],
        )
        graph.add_edge("final_verify", "deliver")
        graph.add_edge("deliver", END)
        return graph.compile(checkpointer=self.env.checkpointer)

    @staticmethod
    def _skip_dependents(record: WorkPlanRecord, failed_step_id: str) -> None:
        changed = True
        blocked = {failed_step_id}
        while changed:
            changed = False
            for step in record.plan.steps:
                state = record.steps.get(step.id) or WorkStepState()
                if state.status in {"completed", "failed", "skipped"}:
                    continue
                if any(dep in blocked for dep in step.depends_on):
                    state.status = "skipped"
                    state.summary = f"前置步骤 {failed_step_id} 失败，已跳过"
                    record.steps[step.id] = state
                    blocked.add(step.id)
                    changed = True

    async def drive(
        self,
        record: WorkPlanRecord,
        *,
        resume: dict[str, Any] | None = None,
    ) -> WorkPlanRecord:
        """驱动编排图；返回最终记录（最终文本/验收摘要由调用方读取）。"""
        snapshot = await self.graph.aget_state(self.config)
        snapshot_record: WorkPlanRecord | None = None
        if snapshot.values and isinstance(snapshot.values.get("record"), dict):
            try:
                snapshot_record = WorkPlanRecord.model_validate(snapshot.values["record"])
            except Exception:  # noqa: BLE001 旧/损坏快照按传入计划恢复
                snapshot_record = None
        source_record = snapshot_record or record
        paused_step = next(
            (step.id for step in source_record.plan.steps
             if (source_record.steps.get(step.id) or WorkStepState()).status == "waiting_approval"),
            None,
        )
        if paused_step is None and resume is not None:
            # 计划快照可能晚于暂停态落库：审批恢复时定位当前 running 步骤
            paused_step = next(
                (step.id for step in source_record.plan.steps
                 if (source_record.steps.get(step.id) or WorkStepState()).status == "running"),
                None,
            )
        resume_step_id = paused_step if (resume is not None or paused_step) else None
        result: dict[str, Any] | None = None
        if not snapshot.values:
            result = await self.graph.ainvoke({
                "record": record.model_dump(mode="json"),
                "outcomes": {}, "outputs": {}, "summaries": {},
                "resume_step_id": resume_step_id,
                "resume_payload": resume,
                "paused": False,
            }, self.config, durability="sync")
        elif snapshot.next:
            # 崩溃恢复：从最近完成的节点继续；当前步骤的 Checkpoint 由适配器自己续跑。
            # 若恢复同时携带审批决策，先把恢复意图写回状态，避免暂停在 verify 节点时丢失。
            if resume is not None or resume_step_id:
                await self.graph.aupdate_state(self.config, {
                    "resume_step_id": resume_step_id,
                    "resume_payload": resume,
                    "paused": False,
                })
            result = await self.graph.ainvoke(None, self.config, durability="sync")
        else:
            current = snapshot_record or record
            if current.status in {"completed", "failed"}:
                self._last_result = snapshot.values
                return current
            if current.status == "paused" and resume is None and paused_step is None:
                self._last_result = snapshot.values
                return current
            result = await self.graph.ainvoke({
                **snapshot.values,
                "record": current.model_dump(mode="json"),
                "resume_step_id": resume_step_id,
                "resume_payload": resume,
                "paused": False,
            }, self.config, durability="sync")
        self._last_result = result or snapshot.values
        return WorkPlanRecord.model_validate((result or snapshot.values).get("record", {}))

    @property
    def last_result(self) -> dict[str, Any]:
        return getattr(self, "_last_result", {}) or {}
