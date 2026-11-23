"""自进化：进化顾问——报告生成 / 一键应用 / 回滚 / 验证闭环（evolution_advisor.py）。

运行：
    pytest backend/tests/test_evolution_advisor_apply.py -q
"""
from datetime import datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import delete, select

from app.core.config import settings
from app.core.database import AsyncSessionLocal, engine, init_db
from app.models.agent import Agent, AgentRun, AgentVersion
from app.models.evolution import AgentEvaluation, EvolutionChangeLog, EvolutionReport
from app.services.evolution.evolution_advisor import (
    SuggestionError, _rule_suggestions, apply_suggestion, check_validations,
    generate_report, rollback_change,
)

_CLEAN = (AgentEvaluation, EvolutionChangeLog, EvolutionReport,
          AgentRun, AgentVersion, Agent)


@pytest_asyncio.fixture(autouse=True)
async def _db():
    await init_db()
    async with AsyncSessionLocal() as s:
        for model in _CLEAN:
            await s.execute(delete(model))
        await s.commit()
    yield
    await engine.dispose()


async def _mk_agent(**kw) -> int:
    async with AsyncSessionLocal() as s:
        agent = Agent(
            name=kw.get("name", "进化测试 Agent"),
            system_prompt=kw.get("system_prompt", "旧的系统提示词内容基线"),
            model_config_id=kw.get("model_config_id", 1),
            tools=["calculator"], temperature="0.7",
            max_iterations=kw.get("max_iterations", 10),
            max_tokens=2048, is_active=True,
        )
        s.add(agent)
        await s.commit()
        return agent.id


async def _mk_report(agent_id: int, suggestions: list) -> int:
    async with AsyncSessionLocal() as s:
        report = EvolutionReport(
            agent_id=agent_id,
            window_start=datetime.utcnow() - timedelta(hours=24),
            window_end=datetime.utcnow(),
            status="draft", summary_text="# 报告",
            suggestions=suggestions,
            metrics_before={"sample_count": 3, "success_rate": 0.5, "avg_overall": 5.0},
        )
        s.add(report)
        await s.commit()
        return report.id


class TestRuleSuggestions:
    def test_thresholds_trigger(self):
        metrics = {
            "sample_count": 10, "tool_error_rate": 0.3,
            "avg_tool_repeat_calls": 1.5, "limit_exceeded_rate": 0.2,
            "success_rate": 0.5, "avg_overall": 4.0,
        }
        out = _rule_suggestions(Agent(name="a"), metrics, ["r1"])
        targets = {(s["type"], s["target"]) for s in out}
        assert ("param", "max_iterations") in targets
        assert sum(1 for s in out if s["target"] == "system_prompt") >= 3
        assert all(s["auto_applicable"] for s in out)
        assert all(s["evidence_run_ids"] == ["r1"] for s in out)

    def test_healthy_metrics_no_suggestions(self):
        metrics = {
            "sample_count": 10, "tool_error_rate": 0.0,
            "avg_tool_repeat_calls": 0.0, "limit_exceeded_rate": 0.0,
            "success_rate": 1.0, "avg_overall": 9.0,
        }
        assert _rule_suggestions(Agent(name="a"), metrics, []) == []

    def test_no_samples_no_suggestions(self):
        assert _rule_suggestions(Agent(name="a"), {"sample_count": 0}, []) == []


class TestApplyRollback:
    @pytest.mark.asyncio
    async def test_apply_prompt_publishes_version(self):
        agent_id = await _mk_agent(system_prompt="原始提示词基线内容")
        rid = await _mk_report(agent_id, [{
            "type": "prompt", "target": "system_prompt",
            "current": "原始提示词基线内容", "proposed": "全新的系统提示词全文内容",
            "auto_applicable": True, "priority": 5,
        }])
        async with AsyncSessionLocal() as s:
            result = await apply_suggestion(s, rid, 0, applied_by="tester")
        assert result["new_version_no"] == 1
        async with AsyncSessionLocal() as s:
            agent = (await s.execute(
                select(Agent).where(Agent.id == agent_id))).scalar_one()
            assert agent.system_prompt == "全新的系统提示词全文内容"
            change = (await s.execute(select(EvolutionChangeLog))).scalar_one()
            assert change.before["value"] == "原始提示词基线内容"
            assert change.status == "applied"
            assert change.applied_version_no == 1
            report = (await s.execute(
                select(EvolutionReport).where(EvolutionReport.id == rid))).scalar_one()
            assert report.status == "applied"
            ver = (await s.execute(select(AgentVersion))).scalar_one()
            assert ver.snapshot["system_prompt"] == "全新的系统提示词全文内容"

    @pytest.mark.asyncio
    async def test_apply_noop_rejected(self):
        """回归：LLM 建议值与当前值相同（原样返回）不得空转发布版本/变更。"""
        agent_id = await _mk_agent(system_prompt="与建议相同的提示词内容")
        rid = await _mk_report(agent_id, [{
            "type": "prompt", "target": "system_prompt",
            "proposed": "与建议相同的提示词内容", "auto_applicable": True,
        }])
        with pytest.raises(SuggestionError):
            async with AsyncSessionLocal() as s:
                await apply_suggestion(s, rid, 0)
        async with AsyncSessionLocal() as s:
            assert (await s.execute(select(AgentVersion))).scalars().all() == []
            assert (await s.execute(select(EvolutionChangeLog))).scalars().all() == []

    @pytest.mark.asyncio
    async def test_apply_param_validation(self):
        agent_id = await _mk_agent()
        rid = await _mk_report(agent_id, [{
            "type": "param", "target": "max_iterations", "proposed": 9999,
            "auto_applicable": True,
        }])
        with pytest.raises(SuggestionError):
            async with AsyncSessionLocal() as s:
                await apply_suggestion(s, rid, 0)

    @pytest.mark.asyncio
    async def test_apply_prompt_too_short_rejected(self):
        agent_id = await _mk_agent()
        rid = await _mk_report(agent_id, [{
            "type": "prompt", "target": "system_prompt", "proposed": "短",
            "auto_applicable": True,
        }])
        with pytest.raises(SuggestionError):
            async with AsyncSessionLocal() as s:
                await apply_suggestion(s, rid, 0)

    @pytest.mark.asyncio
    async def test_tools_flow_not_applicable(self):
        agent_id = await _mk_agent()
        rid = await _mk_report(agent_id, [{
            "type": "tools_flow", "target": "tools", "proposed": [],
            "auto_applicable": False,
        }])
        with pytest.raises(SuggestionError):
            async with AsyncSessionLocal() as s:
                await apply_suggestion(s, rid, 0)

    @pytest.mark.asyncio
    async def test_model_target_validated(self):
        agent_id = await _mk_agent()
        rid = await _mk_report(agent_id, [{
            "type": "model", "target": "model_config_id", "proposed": 424242,
            "auto_applicable": True,
        }])
        with pytest.raises(SuggestionError):
            async with AsyncSessionLocal() as s:
                await apply_suggestion(s, rid, 0)

    @pytest.mark.asyncio
    async def test_rollback_restores_before(self):
        agent_id = await _mk_agent(system_prompt="回滚前的提示词内容")
        rid = await _mk_report(agent_id, [{
            "type": "prompt", "target": "system_prompt",
            "proposed": "迭代后的提示词内容全量替换版", "auto_applicable": True,
        }])
        async with AsyncSessionLocal() as s:
            result = await apply_suggestion(s, rid, 0)
        change_id = result["change_id"]
        async with AsyncSessionLocal() as s:
            rollback = await rollback_change(s, change_id, applied_by="tester")
        assert rollback["restored_version_no"] == 2
        async with AsyncSessionLocal() as s:
            agent = (await s.execute(
                select(Agent).where(Agent.id == agent_id))).scalar_one()
            assert agent.system_prompt == "回滚前的提示词内容"
            original = (await s.execute(
                select(EvolutionChangeLog).where(
                    EvolutionChangeLog.id == change_id))).scalar_one()
            assert original.status == "reverted"
            revert_row = (await s.execute(
                select(EvolutionChangeLog).where(
                    EvolutionChangeLog.revert_of_change_id == change_id
                ))).scalar_one()
            assert revert_row.after["value"] == "回滚前的提示词内容"

        # 二次回滚拒绝
        with pytest.raises(SuggestionError):
            async with AsyncSessionLocal() as s:
                await rollback_change(s, change_id)


class TestCheckValidations:
    async def _setup_change(self, agent_id: int, baseline: dict) -> tuple:
        from app.services.assistant.versions import publish_version

        async with AsyncSessionLocal() as s:
            ver = await publish_version(s, agent_id)
            change = EvolutionChangeLog(
                report_id=1, agent_id=agent_id,
                change_type="param", target="max_iterations",
                before={"value": 10}, after={"value": 20},
                applied_version_no=ver.version_no, status="applied",
                validation={"baseline": baseline},
            )
            report = EvolutionReport(
                agent_id=agent_id,
                window_start=datetime.utcnow(), window_end=datetime.utcnow(),
                status="applied", summary_text="", suggestions=[],
                metrics_before=baseline,
            )
            s.add_all([change, report])
            await s.flush()
            change.report_id = report.id
            await s.commit()
            return ver.id, change.id, report.id

    async def _mk_evals(self, version_id: int, overalls: list):
        async with AsyncSessionLocal() as s:
            for i, ov in enumerate(overalls):
                s.add(AgentEvaluation(
                    run_id=f"val-run-{version_id}-{i}", agent_id=1,
                    agent_version_id=version_id, status="computed",
                    success=ov >= 6, overall=ov,
                    deterministic={"deterministic_score": ov}))
            await s.commit()

    @pytest.mark.asyncio
    async def test_improved_verdict(self, monkeypatch):
        monkeypatch.setattr(settings, "EVOLUTION_VALIDATION_MIN_RUNS", 2)
        agent_id = await _mk_agent()
        ver_id, change_id, report_id = await self._setup_change(
            agent_id, {"avg_overall": 5.0, "success_rate": 0.5})
        await self._mk_evals(ver_id, [8.0, 9.0])
        async with AsyncSessionLocal() as s:
            assert await check_validations(s) == 1
        async with AsyncSessionLocal() as s:
            change = (await s.execute(
                select(EvolutionChangeLog).where(
                    EvolutionChangeLog.id == change_id))).scalar_one()
            assert change.status == "validated"
            assert change.validation["verdict"] == "improved"
            assert change.validation["sample_count"] == 2
            report = (await s.execute(
                select(EvolutionReport).where(
                    EvolutionReport.id == report_id))).scalar_one()
            assert report.status == "validated"
            assert report.validated_at is not None

    @pytest.mark.asyncio
    async def test_regressed_verdict(self, monkeypatch):
        monkeypatch.setattr(settings, "EVOLUTION_VALIDATION_MIN_RUNS", 2)
        agent_id = await _mk_agent()
        ver_id, change_id, _rid = await self._setup_change(
            agent_id, {"avg_overall": 8.0, "success_rate": 1.0})
        await self._mk_evals(ver_id, [3.0, 4.0])
        async with AsyncSessionLocal() as s:
            await check_validations(s)
        async with AsyncSessionLocal() as s:
            change = (await s.execute(
                select(EvolutionChangeLog).where(
                    EvolutionChangeLog.id == change_id))).scalar_one()
            assert change.validation["verdict"] == "regressed"
            assert change.status == "validated"

    @pytest.mark.asyncio
    async def test_insufficient_samples_skipped(self, monkeypatch):
        monkeypatch.setattr(settings, "EVOLUTION_VALIDATION_MIN_RUNS", 5)
        agent_id = await _mk_agent()
        ver_id, change_id, _rid = await self._setup_change(
            agent_id, {"avg_overall": 5.0})
        await self._mk_evals(ver_id, [8.0, 9.0])
        async with AsyncSessionLocal() as s:
            assert await check_validations(s) == 0
        async with AsyncSessionLocal() as s:
            change = (await s.execute(
                select(EvolutionChangeLog).where(
                    EvolutionChangeLog.id == change_id))).scalar_one()
            assert change.status == "applied"


class TestGenerateReport:
    @pytest.mark.asyncio
    async def test_rule_plus_llm_merged(self, monkeypatch):
        agent_id = await _mk_agent()
        # 构造一条评估证据（触发规则建议）
        async with AsyncSessionLocal() as s:
            s.add(AgentEvaluation(
                run_id="gen-run-1", agent_id=agent_id, status="computed",
                success=False, overall=3.0,
                deterministic={"deterministic_score": 3.0, "tool_error_rate": 0.5,
                               "avg_tool_repeat_calls": 0, "limit_exceeded": False,
                               "steps": 5, "tool_calls": 4, "tokens": 100,
                               "duration_s": 10, "hitl_total": 0, "hitl_rejected": 0},
                judge_scores={"issues": [
                    {"dimension": "efficiency", "evidence": "绕路",
                     "suggestion": "精简"}]},
            ))
            await s.commit()

        async def fake_llm(db, agent, metrics, by_version, issues):
            return [{"type": "model", "target": "model_config_id", "proposed": 2,
                     "rationale": "更好模型", "priority": 5, "auto_applicable": True}]

        monkeypatch.setattr(
            "app.services.evolution.evolution_advisor._llm_suggestions", fake_llm)
        async with AsyncSessionLocal() as s:
            report = await generate_report(s, agent_id, 24, created_by="tester")
        assert report.status == "draft"
        assert report.metrics_before["sample_count"] == 1
        types = {(x["type"], x["target"]) for x in report.suggestions}
        assert ("model", "model_config_id") in types  # LLM 建议
        assert any(t == "prompt" for t, _ in types)   # 规则建议
        assert len(report.suggestions) <= 10
        # 优先级降序
        priorities = [x["priority"] for x in report.suggestions]
        assert priorities == sorted(priorities, reverse=True)
        assert "优化报告" in report.summary_text

    @pytest.mark.asyncio
    async def test_llm_failure_keeps_rule_suggestions(self, monkeypatch):
        agent_id = await _mk_agent()
        async with AsyncSessionLocal() as s:
            s.add(AgentEvaluation(
                run_id="gen-run-2", agent_id=agent_id, status="computed",
                success=False, overall=2.0,
                deterministic={"deterministic_score": 2.0, "tool_error_rate": 0.4,
                               "avg_tool_repeat_calls": 2, "limit_exceeded": False,
                               "steps": 9, "tool_calls": 8, "tokens": 900,
                               "duration_s": 30, "hitl_total": 0, "hitl_rejected": 0},
            ))
            await s.commit()

        async def boom(*args, **kwargs):
            raise RuntimeError("LLM 不可用")

        monkeypatch.setattr(
            "app.services.evolution.evolution_advisor._llm_suggestions", boom)
        async with AsyncSessionLocal() as s:
            report = await generate_report(s, agent_id, 24)
        assert report.suggestions, "LLM 失败时规则建议兜底"
        assert all(x["rationale"] for x in report.suggestions)

    @pytest.mark.asyncio
    async def test_unknown_agent_raises(self):
        with pytest.raises(SuggestionError):
            async with AsyncSessionLocal() as s:
                await generate_report(s, 999999, 24)
