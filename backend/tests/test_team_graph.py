"""验证团队分工、并行依赖、容错与同一任务图续跑。"""

import asyncio
import json
from collections import Counter

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from app.services.agent_runtime.repositories.errors import PersistenceError
from app.services.agent_runtime.team_graph import (
    TeamExecutionError,
    TeamGraphContext,
    TeamResponseTruncated,
    build_team_graph,
    drive_team_graph,
    terminal_team_snapshot,
)

_CONFIG = {"configurable": {"thread_id": "team:team-tests"}}
_INPUT = {"objective": "核对交付方案", "reference_context": "验收日期为周五"}


def _task(task_id, role, dependencies=()):
    return {"id": task_id, "role": role, "title": f"任务 {task_id}", "brief": f"交付 {task_id}",
            "depends_on": list(dependencies)}


def _plan(*tasks):
    return json.dumps({"tasks": tasks}, ensure_ascii=False)


def _build(invoke, worker=None, saver=None, remaining=None):
    events = []

    async def emit(event_type, board):
        assert event_type == "team.updated"
        events.append(board)

    return build_team_graph(saver or InMemorySaver(), TeamGraphContext(invoke, emit, worker, remaining)), events


@pytest.mark.asyncio
async def test_parallel_members_pass_only_declared_results_and_lead_synthesizes():
    started = set()
    parallel = asyncio.Event()
    reviewed = []

    async def invoke(stage, system, user):
        context = json.loads(user)
        assert context["objective"] == _INPUT["objective"]
        assert context["reference_context"] == _INPUT["reference_context"]
        if stage == "planning":
            assert "团队规划" in system
            return _plan(_task("facts", "事实核对"), _task("delivery", "交付设计"), _task("review", "风险复核", ("facts",)))
        if stage == "synthesis":
            assert "团队综合" in system
            assert len(context["reports"]) == 3
            return "交付物、风险与核对清单"
        assert "团队成员" in system
        task = context["task"]["id"]
        if task == "review":
            reviewed.extend(context["predecessors"])
        else:
            assert context["predecessors"] == []
            started.add(task)
            if len(started) == 2:
                parallel.set()
            await asyncio.wait_for(parallel.wait(), timeout=2)
        return f"已完成 {task}"

    graph, events = _build(invoke)
    result = await drive_team_graph(graph, _CONFIG, _INPUT)
    assert result["final_text"] == "交付物、风险与核对清单"
    assert reviewed == [{"id": "facts", "role": "事实核对", "title": "任务 facts", "result": "已完成 facts"}]
    assert [event["phase"] for event in events] == ["planning", "working", "working", "working", "synthesizing", "completed"]
    assert [task["status"] for task in events[1]["tasks"]] == ["running", "running", "pending"]
    assert [task["status"] for task in events[2]["tasks"]] == ["completed", "completed", "running"]
    assert all(task["status"] == "completed" for task in events[-1]["tasks"])
    # Run 终态写入之前进程退出，恢复时只读取已完成的综合结果。
    assert await drive_team_graph(graph, _CONFIG, _INPUT) == result
    assert len(events) == 6


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", ["cycle", "missing", "same-role"])
async def test_invalid_plan_retries_once_and_never_calls_members(invalid):
    attempts = []

    async def invoke(stage, system, user):
        assert stage == "planning"
        attempts.append(json.loads(user))
        if invalid == "cycle":
            return _plan(_task("a", "财务", ("b",)), _task("b", "工程", ("a",)))
        if invalid == "missing":
            return _plan(_task("a", "财务", ("other",)), _task("b", "工程"))
        return _plan(_task("a", "Review"), _task("b", "review"))

    graph, events = _build(invoke)
    with pytest.raises(TeamExecutionError, match="分工计划"):
        await drive_team_graph(graph, _CONFIG, _INPUT)
    assert len(attempts) == 2
    assert "correction" in attempts[1]
    assert [event["phase"] for event in events] == ["planning", "failed"]


@pytest.mark.asyncio
async def test_roles_failure_skips_dependents_preserving_partial_output():
    workers = []

    async def worker(task, system, user):
        workers.append(task["id"])
        if task["id"] == "fail":
            raise RuntimeError("不应公开供应商异常内容")
        return "独立成员的实际结果"

    async def invoke(stage, system, user):
        if stage == "planning":
            return _plan(_task("dependent", "总检", ("review",)), _task("review", "复核", ("fail",)),
                         _task("fail", "调研"), _task("success", "流程"))
        assert stage == "synthesis"
        raise RuntimeError("综合供应商不可用")

    graph, events = _build(invoke, worker)
    result = await drive_team_graph(graph, _CONFIG, _INPUT)
    assert sorted(workers) == ["fail", "success"]
    assert "独立成员的实际结果" in result["final_text"]
    assert "不应公开供应商异常内容" not in str(events)
    assert [task["status"] for task in result["team"]["tasks"]] == ["skipped", "skipped", "failed", "completed"]


@pytest.mark.asyncio
async def test_no_worker_results_marks_the_team_failed_without_synthesis():
    async def invoke(stage, system, user):
        if stage == "planning":
            return _plan(_task("a", "调研"), _task("b", "财务"))
        assert stage == "worker"
        return " "

    graph, events = _build(invoke)
    with pytest.raises(TeamExecutionError, match="团队成员未能交付"):
        await drive_team_graph(graph, _CONFIG, _INPUT)
    assert events[-1]["phase"] == "failed"
    assert all(task["status"] == "failed" for task in events[-1]["tasks"])


@pytest.mark.asyncio
async def test_resume_reuses_successful_parallel_member_and_produces_document_brief():
    first_saved = asyncio.Event()

    class Saver(InMemorySaver):
        async def aput_writes(self, config, writes, task_id, task_path=""):
            await super().aput_writes(config, writes, task_id, task_path)
            if any(channel == "outcomes" and "one" in value for channel, value in writes):
                first_saved.set()

    saver = Saver()
    calls = Counter()

    async def invoke(stage, system, user):
        assert stage == "planning"
        calls["plan"] += 1
        return _plan(_task("one", "分析"), _task("two", "设计"))

    async def worker(task, system, user):
        task_id = task["id"]
        calls[task_id] += 1
        if task_id == "two" and calls[task_id] == 1:
            # 模拟同一批中一个成员结果已提交，另一成员执行权意外中断。
            await asyncio.wait_for(first_saved.wait(), timeout=2)
            raise PersistenceError("checkpoint interruption")
        return f"成员成果 {task_id}"

    graph, events = _build(invoke, worker, saver)
    with pytest.raises(PersistenceError):
        await drive_team_graph(graph, _CONFIG, {**_INPUT, "synthesize": False})
    recovered_graph, recovered_events = _build(invoke, worker, saver)
    recovered = await drive_team_graph(recovered_graph, _CONFIG, {**_INPUT, "synthesize": False})
    assert calls == {"plan": 1, "one": 1, "two": 2}
    assert recovered["final_text"] == ""
    assert "成员成果 one" in recovered["team_brief"] and "成员成果 two" in recovered["team_brief"]
    assert not any(event["phase"] == "synthesizing" for event in events + recovered_events)


@pytest.mark.asyncio
async def test_cancellation_interrupts_running_members_without_starting_synthesis():
    started = asyncio.Event()
    stopped = []

    async def invoke(stage, system, user):
        assert stage == "planning"
        return _plan(_task("a", "分析"), _task("b", "设计"))

    async def worker(task, system, user):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.append(task["id"])

    graph, events = _build(invoke, worker)
    run = asyncio.create_task(drive_team_graph(graph, _CONFIG, _INPUT))
    await asyncio.wait_for(started.wait(), timeout=2)
    run.cancel()
    with pytest.raises(asyncio.CancelledError):
        await run
    assert sorted(stopped) == ["a", "b"]
    assert [event["phase"] for event in events] == ["planning", "working"]


def test_cancel_preserves_delivered_results():
    previous = {"lead": "团队负责人", "tasks": [
        {"id": "ready", "status": "completed", "result": "已交付"},
        {"id": "active", "status": "running"},
        {"id": "pending", "status": "pending"},
    ]}
    result = terminal_team_snapshot(previous, "cancelled")
    assert result["phase"] == "cancelled"
    assert [task["status"] for task in result["tasks"]] == ["completed", "cancelled", "cancelled"]
    assert result["tasks"][0]["result"] == "已交付" and previous["tasks"][1]["status"] == "running"


@pytest.mark.asyncio
async def test_deep_review_reworks_only_assigned_gap_and_preserves_unverified_findings():
    criteria = ["以数据核对迁移风险", "交叉核对供应商未公开的费用"]
    stages = []

    async def invoke(stage, system, user):
        data = json.loads(user)
        stages.append(stage)
        if stage == "planning":
            assert "深度验收" in system
            return json.dumps({"tasks": [_task("risk", "迁移评估"), _task("cost", "费用核对")], "acceptance": criteria})
        if stage == "worker":
            assert data["acceptance"] == criteria
            return f"原始成果 {data['task']['id']}"
        if stage == "review":
            assert "独立审阅者" in system
            assert len(data["reports"]) == 2
            recheck = data["previous_review"] is not None
            if recheck:
                assert any(item.get("result") == "补充量化的回滚窗口" for item in data["reports"])
            return json.dumps({"checks": [
                {"criterion": 1, "status": "supported" if recheck else "gap", "task_id": "risk",
                 "evidence": "给出量化的回滚窗口" if recheck else "缺少回滚窗口"},
                {"criterion": 2, "status": "unverified", "evidence": "缺少第三方报价单"},
            ]})
        if stage == "rework":
            assert data["task"]["id"] == "risk" and data["prior_result"] == "原始成果 risk"
            assert [item["evidence"] for item in data["findings"]] == ["缺少回滚窗口"]
            return "补充量化的回滚窗口"
        assert data["review"]["round"] == 2
        return "按回滚窗口排序并提出迁移建议。"

    graph, events = _build(invoke)
    result = await drive_team_graph(graph, _CONFIG, {"objective": "深入分析并比较迁移方案，交叉核对费用与风险"})
    assert stages == ["planning", "worker", "worker", "review", "rework", "review", "synthesis"]
    rework = next(event for event in events if event["phase"] == "repairing")
    assert rework["tasks"][0]["status"] == "running" and rework["tasks"][1]["status"] == "completed"
    assert [check["status"] for check in result["team"]["review"]["checks"]] == ["supported", "unverified"]
    assert "仍需核验" in result["final_text"] and "缺少第三方报价单" in result["final_text"]
    assert result["team"]["phase"] == "failed" and result["failure"]


@pytest.mark.asyncio
async def test_small_budget_retains_synthesis_and_does_not_claim_evidence_review():
    stages = []

    async def invoke(stage, system, user):
        stages.append(stage)
        if stage == "planning":
            return json.dumps({"tasks": [_task("a", "证据"), _task("b", "反例")],
                               "acceptance": ["完整收集证据", "逐项核对反例"]})
        if stage == "worker":
            return "可用的成员证据"
        assert stage == "synthesis" and "预算不足" in json.loads(user)["review"]["error"]
        return "综合回答"

    graph, _ = _build(invoke, remaining=lambda: 4 - len(stages))
    result = await drive_team_graph(graph, _CONFIG, {"objective": "全面评估两份技术方案的风险"})
    assert len(stages) == 4 and "review" not in stages
    assert "剩余运行预算不足" in result["final_text"]
    assert result["team"]["phase"] == "failed"


@pytest.mark.asyncio
async def test_multiple_owners_repair_until_all_review_targets_pass():
    reworks = Counter()
    calls = []

    async def invoke(stage, system, user):
        data = json.loads(user)
        calls.append(stage)
        if stage == "planning":
            return json.dumps({"tasks": [_task("a", "数据核对"), _task("b", "风险评审"), _task("c", "上线验收")],
                               "acceptance": ["核对原始数据", "补齐风险验证"]})
        if stage == "worker":
            return "各成员交付初稿"
        if stage == "review":
            return json.dumps({"checks": [
                {"criterion": index, "status": "supported" if reworks[owner] >= needed else "gap", "task_id": owner,
                 "evidence": "待补证据 " + str(reworks[owner])}
                for index, owner, needed in ((1, "a", 1), (2, "b", 2))
            ]})
        if stage == "rework":
            owner = data["task"]["id"]
            reworks[owner] += 1
            return "补齐证据 " + str(reworks[owner])
        return "根据两轮核对验收，已明确影响和验证结果。"

    graph, _ = _build(invoke, remaining=lambda: 14 - len(calls))
    result = await drive_team_graph(graph, _CONFIG, {"objective": "深入评估迁移风险并核对证据"})
    assert reworks == {"a": 1, "b": 2}
    assert result["team"]["review"]["round"] == 3
    assert result["team"]["phase"] == "completed"


@pytest.mark.asyncio
async def test_truncated_team_synthesis_preserves_partial_results_without_completion():
    async def invoke(stage, system, user):
        if stage == "planning":
            return _plan(_task("a", "资料"), _task("b", "复核"))
        if stage == "worker":
            return "成员交付内容"
        raise TeamResponseTruncated("综合回答只生成了第一部分")

    graph, _ = _build(invoke)
    result = await drive_team_graph(graph, _CONFIG, _INPUT)
    assert result["team"]["phase"] == "failed"
    assert "长度上限" in result["failure"]
    assert result["final_text"] == "综合回答只生成了第一部分"
