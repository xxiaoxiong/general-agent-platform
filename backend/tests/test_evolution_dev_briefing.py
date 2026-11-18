"""自进化：开发迭代简报（dev_briefing.py）——客观数据汇总与 Markdown 渲染。

运行：
    pytest backend/tests/test_evolution_dev_briefing.py -q
"""
from datetime import datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import delete

from app.core.database import AsyncSessionLocal, engine, init_db
from app.models.agent import (
    Agent, AgentApproval, AgentRun, AgentToolExecution, AgentVersion,
)
from app.models.evolution import AgentEvaluation, EvolutionChangeLog
from app.services.evolution.dev_briefing import briefing_to_markdown, build_briefing

_CLEAN = (AgentApproval, AgentToolExecution, AgentEvaluation,
          EvolutionChangeLog, AgentRun, AgentVersion, Agent)


@pytest_asyncio.fixture(autouse=True)
async def _db():
    await init_db()
    async with AsyncSessionLocal() as s:
        for model in _CLEAN:
            await s.execute(delete(model))
        await s.commit()
    yield
    await engine.dispose()


async def _seed():
    async with AsyncSessionLocal() as s:
        s.add(AgentVersion(id=1, agent_id=1, version_no=1, snapshot={}))
        s.add(AgentVersion(id=2, agent_id=1, version_no=2, snapshot={}))
        # v1：1 成功 + 1 失败；v2：1 成功
        s.add(AgentRun(
            id="dbf-run-ok1", agent_id=1, agent_version_id=1, thread_id="t1",
            status="completed", budget_steps=4, budget_model_calls=4,
            budget_tool_calls=3, budget_tokens=800,
            started_at=datetime.utcnow() - timedelta(seconds=40),
            finished_at=datetime.utcnow()))
        s.add(AgentRun(
            id="dbf-run-fail", agent_id=1, agent_version_id=1, thread_id="t2",
            status="failed", budget_steps=2, budget_tokens=300,
            error="ToolError: search 超时", created_at=datetime.utcnow()))
        s.add(AgentRun(
            id="dbf-run-ok2", agent_id=1, agent_version_id=2, thread_id="t3",
            status="completed", budget_steps=3, budget_tokens=600,
            created_at=datetime.utcnow()))
        # 工具账本：search 失败1次+重复1次；write 成功
        s.add(AgentToolExecution(
            run_id="dbf-run-ok1", tool_call_id="c1", tool_name="search",
            status="failed", args_hash="h1", attempt_count=2,
            error="timeout after 30s"))
        s.add(AgentToolExecution(
            run_id="dbf-run-ok1", tool_call_id="c2", tool_name="search",
            status="succeeded", args_hash="h1", attempt_count=1))
        s.add(AgentToolExecution(
            run_id="dbf-run-ok2", tool_call_id="c3", tool_name="write",
            status="succeeded", args_hash="h2", attempt_count=1))
        # HITL：1 拒绝
        s.add(AgentApproval(
            run_id="dbf-run-ok1", tool_name="write",
            decision="reject", action_summary="写入生产库",
            message="风险过高"))
        # 评估记录（含裁判维度分与问题记录）
        s.add(AgentEvaluation(
            run_id="dbf-run-ok1", agent_id=1, agent_version_id=1,
            status="computed", success=True, overall=8.0,
            judge_scores={
                "dimensions": {"task_completion": 8, "answer_quality": 8},
                "verdict": "pass",
                "issues": [{"dimension": "efficiency",
                            "evidence": "重复调用 search",
                            "suggestion": "复用结果"}],
            }))
        s.add(AgentEvaluation(
            run_id="dbf-run-fail", agent_id=1, agent_version_id=1,
            status="computed", success=False, overall=3.0,
            judge_scores={
                "dimensions": {"task_completion": 3, "answer_quality": 4},
                "verdict": "fail", "issues": [],
            }))
        # 变更审计
        s.add(EvolutionChangeLog(
            report_id=1, agent_id=1, change_type="prompt",
            target="system_prompt", before={"value": "a"}, after={"value": "b"},
            status="applied", validation={"verdict": "improved"}))
        await s.commit()


class TestBuildBriefing:
    @pytest.mark.asyncio
    async def test_runs_and_failed_runs(self):
        await _seed()
        async with AsyncSessionLocal() as s:
            data = await build_briefing(s, window_hours=24)
        runs = data["runs"]
        assert runs["total"] == 3
        assert runs["by_status"] == {"completed": 2, "failed": 1}
        assert runs["success_rate"] == round(2 / 3, 4)
        assert runs["with_error"] == 1
        assert runs["tokens_total"] == 1700
        fail_ids = [f["run_id"] for f in data["failed_runs"]]
        assert fail_ids == ["dbf-run-fail"]
        assert "search 超时" in data["failed_runs"][0]["error"]

    @pytest.mark.asyncio
    async def test_tool_stats(self):
        await _seed()
        async with AsyncSessionLocal() as s:
            data = await build_briefing(s, window_hours=24)
        tools = data["tools"]
        assert tools["total_calls"] == 3
        assert tools["total_failed"] == 1
        per_tool = {t["tool_name"]: t for t in tools["per_tool"]}
        assert per_tool["search"]["calls"] == 2
        assert per_tool["search"]["failed"] == 1
        assert per_tool["search"]["retried"] == 1
        assert per_tool["search"]["repeat_calls"] == 1  # 同 run 同参数重复 1 次
        assert per_tool["search"]["error_rate"] == 0.5
        assert per_tool["write"]["failed"] == 0
        assert tools["failure_samples"][0]["tool_name"] == "search"

    @pytest.mark.asyncio
    async def test_hitl_and_versions(self):
        await _seed()
        async with AsyncSessionLocal() as s:
            data = await build_briefing(s, window_hours=24)
        hitl = data["hitl"]
        assert hitl["total"] == 1
        assert hitl["by_decision"] == {"reject": 1}
        assert hitl["rejected_records"][0]["tool_name"] == "write"
        by_ver = {v["agent_version_id"]: v for v in data["by_version"]}
        assert by_ver[1]["runs"] == 2 and by_ver[1]["failed"] == 1
        assert by_ver[2]["runs"] == 1 and by_ver[2]["failed"] == 0
        assert by_ver[1]["version_no"] == 1

    @pytest.mark.asyncio
    async def test_evaluations_and_changes(self):
        await _seed()
        async with AsyncSessionLocal() as s:
            data = await build_briefing(s, window_hours=24)
        ev = data["evaluations"]
        assert ev["count"] == 2
        assert ev["avg_overall"] == 5.5
        assert ev["verdicts"] == {"pass": 1, "fail": 1}
        assert ev["avg_dimensions"]["task_completion"] == 5.5
        assert ev["issue_records"][0]["run_id"] == "dbf-run-ok1"
        assert len(data["changes"]) == 1
        assert data["changes"][0]["validation"]["verdict"] == "improved"

    @pytest.mark.asyncio
    async def test_agent_filter(self):
        await _seed()
        async with AsyncSessionLocal() as s:
            data = await build_briefing(s, window_hours=24, agent_id=99)
        assert data["runs"]["total"] == 0
        assert data["failed_runs"] == []
        assert data["tools"]["total_calls"] == 0


class TestBriefingMarkdown:
    @pytest.mark.asyncio
    async def test_markdown_sections(self):
        await _seed()
        async with AsyncSessionLocal() as s:
            data = await build_briefing(s, window_hours=24)
        md = briefing_to_markdown(data)
        for header in [
            "# Agent 执行数据简报（客观数据）",
            "## 1. 执行概览", "## 2. 失败 Run 记录", "## 3. 工具调用统计",
            "## 4. 工具失败记录", "## 5. HITL 审批数据", "## 6. 分版本趋势",
            "## 7. 评估数据", "## 8. 配置变更审计",
        ]:
            assert header in md
        assert "不含主观优化建议" in md
        assert "dbf-run-fail" in md
        assert "search" in md
