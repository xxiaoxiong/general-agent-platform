"""Work Orchestrator 集成辅助：shadow/auto/降级/计划复用与事件协议。"""
from __future__ import annotations

from typing import Any

import pytest

from app.services.agent_runtime.event_protocol import to_v2_event
from app.services.agent_runtime.events import (
    WORK_PLAN_CREATED, RuntimeEvent, event_storage_payload,
)
from app.services.work_orchestrator import integration
from app.services.work_orchestrator.planner import PlannerOutcome
from app.services.work_orchestrator.schemas import (
    PlannerMetadata, WorkPlan, WorkStep, WorkPlanRecord, new_plan_record,
)


class FakeBus:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    async def publish(self, event) -> None:
        self.events.append((event.type, dict(event.data or {})))


class FakeRun:
    run_id = "run-int-1"
    user_id = 3
    status = "running"
    final_text_parts: list[str] = []
    summary: dict[str, Any] = {}

    def execution_lease(self):
        return None


class FakeCtx:
    goal_mode = False
    work_step_id = None
    agent_config = {"capability_ids": ["web_research"], "database_connection_id": None}

    def __init__(self) -> None:
        self.model_calls = 0
        self.steps = 0
        self.tokens_generated = 0


def _plan() -> WorkPlan:
    return WorkPlan(
        objective="调研并生成 Word 报告",
        steps=[
            WorkStep(id="research", title="资料检索", objective="检索来源",
                     executor_profile="research", deliverables=["research_sources"],
                     acceptance_criteria=["有来源"]),
            WorkStep(id="document", title="生成报告", objective="整理为 Word",
                     executor_profile="document_docx", depends_on=["research"],
                     input_refs=["step:research"], deliverables=["report.docx"],
                     acceptance_criteria=["文件可下载"]),
        ],
    )


def _outcome(plan: WorkPlan | None, *, fallback: bool = False, error: str = "") -> PlannerOutcome:
    return PlannerOutcome(plan=plan, fallback=fallback, error=error, latency_ms=12)


def _execution_context(**overrides) -> dict[str, Any]:
    values = {
        "task_type": "document_generate",
        "deliverable_format": "docx",
        "decision": {"intent": "document_generate", "constraints": ["中文"]},
        "attachment_refs": [],
    }
    values.update(overrides)
    return values


def _kwargs(monkeypatch, *, mode: str, message: str = "调研并生成 Word 报告"):
    bus = FakeBus()
    run = FakeRun()
    ctx = FakeCtx()
    context = _execution_context()
    monkeypatch.setattr(integration, "persist_work_plan_record", _noop)
    monkeypatch.setattr(integration, "persist_work_plan_diagnostic", _noop)
    return dict(
        service=None, run=run, ctx=ctx, bus=bus,
        provider={}, model_config={}, tool_specs=[
            {"name": "web_search"}, {"name": "write_file"}, {"name": "read_file"},
        ],
        execution_context=context,
        messages=[{"role": "user", "content": message}],
        decision_input={},
        mode=mode,
        auto_team=False, explicit_team=False,
    ), bus, context, ctx


async def _noop(*args, **kwargs):
    return None


@pytest.mark.asyncio
async def test_simple_task_does_not_call_planner(monkeypatch):
    called = {"count": 0}

    async def fail(*args, **kwargs):
        called["count"] += 1
        raise AssertionError("简单任务不得调用 Planner")

    monkeypatch.setattr(integration, "plan_work", fail)
    kwargs, bus, context, _ctx = _kwargs(monkeypatch, mode="auto", message="解释一下什么是向量数据库")
    kwargs["execution_context"] = {
        "task_type": "general_chat", "deliverable_format": "none",
        "decision": {"intent": "general_chat"}, "attachment_refs": [],
    }
    record = await integration.prepare_plan(**kwargs)
    assert record is None
    assert called["count"] == 0
    assert bus.events == []
    assert "work_plan" not in context


@pytest.mark.asyncio
async def test_shadow_mode_persists_and_publishes_plan_without_execution(monkeypatch):
    async def fake_plan(*args, **kwargs):
        return _outcome(_plan())

    monkeypatch.setattr(integration, "plan_work", fake_plan)
    kwargs, bus, context, _ctx = _kwargs(monkeypatch, mode="shadow")
    record = await integration.prepare_plan(**kwargs)

    assert record is None
    assert context["work_plan"]["mode"] == "shadow"
    assert [item[0] for item in bus.events] == [WORK_PLAN_CREATED]
    assert len(bus.events[0][1]["steps"]) == 2


@pytest.mark.asyncio
async def test_auto_mode_returns_validated_record(monkeypatch):
    async def fake_plan(*args, **kwargs):
        return _outcome(_plan())

    monkeypatch.setattr(integration, "plan_work", fake_plan)
    kwargs, bus, context, _ctx = _kwargs(monkeypatch, mode="auto")
    record = await integration.prepare_plan(**kwargs)

    assert isinstance(record, WorkPlanRecord)
    assert record.mode == "auto" and record.status == "ready"
    assert context["work_plan"]["plan"]["plan_id"] == record.plan.plan_id
    assert bus.events[0][0] == WORK_PLAN_CREATED


@pytest.mark.asyncio
async def test_planner_fallback_and_invalid_plan_emit_degraded(monkeypatch):
    async def fallback(*args, **kwargs):
        return _outcome(None, fallback=True, error="timeout")

    monkeypatch.setattr(integration, "plan_work", fallback)
    kwargs, bus, _context, _ctx = _kwargs(monkeypatch, mode="auto")
    assert await integration.prepare_plan(**kwargs) is None
    assert bus.events[0][0] == "work_plan_degraded"

    async def invalid(*args, **kwargs):
        plan = _plan()
        # 不存在的上下文引用由 Validator 拒绝（模型不能凭空引用资源）
        plan.steps[0].input_refs = ["artifact_999"]
        return _outcome(plan)

    monkeypatch.setattr(integration, "plan_work", invalid)
    kwargs, bus, _context, _ctx = _kwargs(monkeypatch, mode="auto")
    assert await integration.prepare_plan(**kwargs) is None
    assert bus.events[0][0] == "work_plan_degraded"
    assert "不存在的上下文" in bus.events[0][1]["reason"]


@pytest.mark.asyncio
async def test_persisted_auto_plan_is_reused_without_replanning(monkeypatch):
    record = new_plan_record(_plan(), mode="auto")
    context = _execution_context(work_plan=record.model_dump(mode="json"))

    async def fail(*args, **kwargs):
        raise AssertionError("恢复时不得重新规划")

    monkeypatch.setattr(integration, "plan_work", fail)
    kwargs, _bus, _context, _ctx = _kwargs(monkeypatch, mode="auto")
    kwargs["execution_context"] = context
    reused = await integration.prepare_plan(**kwargs)
    assert reused is not None and reused.plan.plan_id == record.plan.plan_id


def test_work_events_map_to_v2_and_are_not_truncated_to_400_chars():
    steps = [f"步骤 {index} " + ("内容" * 200) for index in range(4)]
    event = RuntimeEvent(
        type=WORK_PLAN_CREATED, run_id="r1",
        data={"plan_id": "p1", "revision": 1, "status": "ready", "steps": steps},
    )
    payload = event_storage_payload(event)
    assert len(payload["steps"][0]) > 400

    envelope = to_v2_event(
        run_id="r1", sequence=3, event_type=WORK_PLAN_CREATED,
        payload={"plan_id": "p1", "steps": [{"id": "research"}]},
    )
    assert envelope["event"] == "work.plan.created"
    assert envelope["data"]["plan_id"] == "p1"

    step = to_v2_event(
        run_id="r1", sequence=4, event_type="work_step_started",
        payload={"plan_id": "p1", "step_id": "research", "title": "资料检索"},
    )
    assert step["event"] == "work.step.started"
