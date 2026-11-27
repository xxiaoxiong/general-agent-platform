"""Work Orchestrator Graph：依赖顺序、有限修复、暂停恢复、崩溃恢复与最终验收。"""
from __future__ import annotations

from typing import Any

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from app.services.work_orchestrator import graph as graph_module
from app.services.work_orchestrator.adapters.base import StepRuntimeEnv
from app.services.work_orchestrator.graph import WorkOrchestrator
from app.services.work_orchestrator.schemas import (
    WorkArtifactRef, WorkCheck, WorkPlan, WorkPlanRecord, WorkStep, WorkStepOutcome,
    WorkVerificationResult, new_plan_record,
)


class FakeBus:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    async def publish(self, event) -> None:
        self.events.append((event.type, dict(event.data or {})))


class FakeRun:
    run_id = "run-work-1"
    user_id = 7
    status = "running"
    final_text_parts: list[str] = []
    summary: dict[str, Any] = {}

    def execution_lease(self):
        return None


class FakeAdapter:
    def __init__(self, outcomes: dict[str, list[Any]]) -> None:
        self.outcomes = {key: list(value) for key, value in outcomes.items()}
        self.calls: list[str] = []
        self.resume_calls: list[Any] = []

    async def execute(self, step, record, env, *, resume=None, attempt=1):
        self.calls.append(step.id)
        self.resume_calls.append(resume)
        queue = self.outcomes.get(step.id) or []
        if not queue:
            raise AssertionError(f"步骤 {step.id} 没有配置结果")
        result = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(result, Exception):
            raise result
        return result


def _outcome(step_id: str, *, status: str = "completed", text: str = "", artifacts=None,
             retryable: bool = False, paused: bool = False) -> WorkStepOutcome:
    checks = [WorkCheck(id="answer_present", passed=bool(text or artifacts))]
    return WorkStepOutcome(
        step_id=step_id, status=status, summary=f"{step_id} {status}",
        final_text=text, artifact_refs=artifacts or [],
        verification=WorkVerificationResult(
            status="passed" if status == "completed" else ("partial" if paused else "failed"),
            checks=checks, artifact_refs=artifacts or [],
            repairable=retryable,
        ),
        retryable=retryable, paused=paused,
        checkpoint_ref=f"work:{FakeRun.run_id}:step:{step_id}",
    )


def _record(*steps: WorkStep, mode: str = "auto") -> WorkPlanRecord:
    return new_plan_record(WorkPlan(objective="复合目标", steps=list(steps)), mode=mode)


def _env() -> StepRuntimeEnv:
    env = StepRuntimeEnv(
        service=None, run=FakeRun(), ctx=type("Ctx", (), {
            "goal_mode": False, "agent_config": {}, "work_step_id": None,
        })(), bus=FakeBus(), provider={}, model_config={}, agent_config={},
        policy=None, tool_specs=[], checkpointer=InMemorySaver(), store=None,
        route={"team_mode": False}, messages=[{"role": "user", "content": "复合目标"}],
        user_id=7,
    )
    return env


def _step(step_id: str, profile: str, depends: list[str] | None = None) -> WorkStep:
    return WorkStep(
        id=step_id, title=step_id, objective=f"完成 {step_id}",
        executor_profile=profile, depends_on=depends or [],
        input_refs=[f"step:{item}" for item in (depends or [])],
        acceptance_criteria=["有可核对结果"],
    )


@pytest.fixture(autouse=True)
def no_plan_persistence(monkeypatch):
    async def _noop(*args, **kwargs):
        return None

    monkeypatch.setattr(graph_module, "persist_work_plan_record", _noop)


@pytest.mark.asyncio
async def test_graph_runs_three_stage_research_analysis_document_in_order(monkeypatch):
    research = FakeAdapter({"research": [_outcome("research", text="研究结论与来源")]})
    analysis = FakeAdapter({"analysis": [_outcome("analysis", text="架构比较分析")]})
    document = FakeAdapter({"document": [_outcome(
        "document", text="已生成 Word 报告",
        artifacts=[WorkArtifactRef(path="document:9:1", name="report.docx")],
    )]})
    monkeypatch.setitem(graph_module._ADAPTERS, "research", research)
    monkeypatch.setitem(graph_module._ADAPTERS, "general_agent", analysis)
    monkeypatch.setitem(graph_module._ADAPTERS, "document_docx", document)
    env = _env()
    record = _record(
        _step("research", "research"),
        _step("analysis", "general_agent", ["research"]),
        _step("document", "document_docx", ["analysis"]),
    )
    final = await WorkOrchestrator(env).drive(record)

    assert research.calls == ["research"]
    assert analysis.calls == ["analysis"]
    assert document.calls == ["document"]
    assert env.step_outputs["research"] == "研究结论与来源"
    assert env.step_outputs["analysis"] == "架构比较分析"
    assert final.status == "completed"
    assert final.completed_count == 3


@pytest.mark.asyncio
async def test_external_side_effect_unknown_needs_reconciliation_and_is_not_retried(monkeypatch):
    external = _outcome("research", status="failed", retryable=True)
    external.verification.stop_reason = "external_operation"
    research = FakeAdapter({"research": [external]})
    document = FakeAdapter({"document": [_outcome(
        "document", text="不应执行",
        artifacts=[WorkArtifactRef(path="document:1:1", name="report.docx")],
    )]})
    monkeypatch.setitem(graph_module._ADAPTERS, "research", research)
    monkeypatch.setitem(graph_module._ADAPTERS, "document_docx", document)
    env = _env()
    record = _record(_step("research", "research"), _step("document", "document_docx", ["research"]))
    final = await WorkOrchestrator(env).drive(record)

    assert research.calls == ["research"]  # 外部副作用未知不得自动重试
    assert final.steps["research"].status == "needs_reconciliation"
    assert final.steps["document"].status == "skipped"
    assert final.status == "failed"


@pytest.mark.asyncio
async def test_graph_runs_dependencies_in_order_and_delivers_artifact(monkeypatch):
    research = FakeAdapter({"research": [_outcome("research", text="研究结论")]})
    document = FakeAdapter({"document": [_outcome(
        "document", text="报告已生成",
        artifacts=[WorkArtifactRef(
            path="document:1:1", name="report.docx",
            download_url="/api/v2/documents/1/versions/1/download",
        )],
    )]})
    monkeypatch.setitem(graph_module._ADAPTERS, "research", research)
    monkeypatch.setitem(graph_module._ADAPTERS, "document_docx", document)
    env = _env()
    record = _record(_step("research", "research"), _step("document", "document_docx", ["research"]))
    orchestrator = WorkOrchestrator(env)
    final = await orchestrator.drive(record)

    assert research.calls == ["research"]
    assert document.calls == ["document"]
    assert env.step_outputs["research"] == "研究结论"
    assert final.status == "completed"
    assert orchestrator.last_result["final_text"] == "报告已生成"
    kinds = [event[0] for event in env.bus.events]
    assert kinds.count("work_step_started") == 2
    assert kinds.count("work_step_completed") == 2
    assert "work_plan_completed" in kinds


@pytest.mark.asyncio
async def test_graph_skips_dependents_after_permanent_failure(monkeypatch):
    research = FakeAdapter({"research": [_outcome("research", status="failed", text="")]})
    document = FakeAdapter({"document": [_outcome(
        "document", text="不应执行",
        artifacts=[WorkArtifactRef(path="document:1:1", name="report.docx")],
    )]})
    monkeypatch.setitem(graph_module._ADAPTERS, "research", research)
    monkeypatch.setitem(graph_module._ADAPTERS, "document_docx", document)
    env = _env()
    record = _record(_step("research", "research"), _step("document", "document_docx", ["research"]))
    final = await WorkOrchestrator(env).drive(record)

    assert research.calls == ["research"]
    assert document.calls == []
    assert final.steps["document"].status == "skipped"
    assert final.status == "failed"


@pytest.mark.asyncio
async def test_graph_bounded_repair_retries_only_failed_step(monkeypatch):
    research = FakeAdapter({
        "research": [
            _outcome("research", status="failed", retryable=True),
            _outcome("research", text="修复后的研究"),
        ],
    })
    document = FakeAdapter({"document": [_outcome(
        "document", text="报告", artifacts=[WorkArtifactRef(path="document:1:1", name="report.docx")],
    )]})
    monkeypatch.setitem(graph_module._ADAPTERS, "research", research)
    monkeypatch.setitem(graph_module._ADAPTERS, "document_docx", document)
    env = _env()
    record = _record(_step("research", "research"), _step("document", "document_docx", ["research"]))
    final = await WorkOrchestrator(env).drive(record)

    assert research.calls == ["research", "research"]
    assert document.calls == ["document"]
    assert final.repairs == 1
    assert final.status == "completed"


@pytest.mark.asyncio
async def test_graph_pauses_for_approval_and_resumes_same_step(monkeypatch):
    research = FakeAdapter({"research": [_outcome("research", paused=True)]})
    document = FakeAdapter({"document": [_outcome(
        "document", text="完成", artifacts=[WorkArtifactRef(path="document:1:1", name="report.docx")],
    )]})
    monkeypatch.setitem(graph_module._ADAPTERS, "research", research)
    monkeypatch.setitem(graph_module._ADAPTERS, "document_docx", document)
    env = _env()
    record = _record(_step("research", "research"), _step("document", "document_docx", ["research"]))
    orchestrator = WorkOrchestrator(env)
    paused = await orchestrator.drive(record)
    assert paused.status == "paused"
    assert paused.steps["research"].status == "waiting_approval"
    assert document.calls == []

    research.outcomes["research"] = [_outcome("research", text="批准后的研究")]
    resumed = await orchestrator.drive(paused, resume={"decisions": [{"type": "approve"}]})
    assert research.resume_calls[-1] == {"decisions": [{"type": "approve"}]}
    assert resumed.status == "completed"
    assert document.calls == ["document"]


@pytest.mark.asyncio
async def test_graph_recovers_from_crash_without_repeating_completed_steps(monkeypatch):
    research = FakeAdapter({"research": [_outcome("research", text="已完成研究")]})
    document = FakeAdapter({"document": [RuntimeError("worker crashed")]})
    monkeypatch.setitem(graph_module._ADAPTERS, "research", research)
    monkeypatch.setitem(graph_module._ADAPTERS, "document_docx", document)
    env = _env()
    record = _record(_step("research", "research"), _step("document", "document_docx", ["research"]))
    orchestrator = WorkOrchestrator(env)
    with pytest.raises(RuntimeError):
        await orchestrator.drive(record)

    snapshot = await orchestrator.graph.aget_state(orchestrator.config)
    current = WorkPlanRecord.model_validate(snapshot.values["record"])
    assert current.steps["research"].status == "completed"
    assert current.steps["document"].status == "running"

    document.outcomes["document"] = [_outcome(
        "document", text="恢复完成", artifacts=[WorkArtifactRef(path="document:1:1", name="report.docx")],
    )]
    resumed = await WorkOrchestrator(env).drive(current)
    assert research.calls == ["research"]
    assert document.calls == ["document", "document"]
    assert resumed.status == "completed"


@pytest.mark.asyncio
async def test_final_verification_requires_declared_file_artifact(monkeypatch):
    research = FakeAdapter({"research": [_outcome("research", text="研究")]})
    document = FakeAdapter({"document": [_outcome("document", text="口头说生成了", artifacts=[])]})
    monkeypatch.setitem(graph_module._ADAPTERS, "research", research)
    monkeypatch.setitem(graph_module._ADAPTERS, "document_docx", document)
    env = _env()
    record = _record(_step("research", "research"), _step("document", "document_docx", ["research"]))
    final = await WorkOrchestrator(env).drive(record)

    assert final.status == "failed"
    checks = {item.id: item for item in final.final_verification.checks}
    assert checks["artifacts_present"].passed is False
