"""自进化：评估器——UNIQUE(run_id) 抢占 / judge 编排 / 待评估扫描（evaluator.py）。

运行：
    pytest backend/tests/test_evolution_evaluator_claim.py -q
"""
from datetime import datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import delete, select

from app.core.database import AsyncSessionLocal, engine, init_db
from app.models.agent import Agent, AgentRun
from app.models.evolution import AgentEvaluation
from app.services.evolution.evaluator import evaluate_pending_runs, evaluate_run

_CLEAN = (AgentEvaluation, AgentRun, Agent)


@pytest_asyncio.fixture(autouse=True)
async def _db():
    await init_db()
    async with AsyncSessionLocal() as s:
        for model in _CLEAN:
            await s.execute(delete(model))
        await s.commit()
    yield
    await engine.dispose()


async def _mk_run(run_id: str, status: str = "cancelled",
                  finished_minutes_ago: float = 5) -> str:
    async with AsyncSessionLocal() as s:
        s.add(AgentRun(
            id=run_id, agent_id=1, thread_id=f"t-{run_id}",
            status=status,
            budget_steps=2, budget_model_calls=2,
            budget_tool_calls=1, budget_tokens=300,
            finished_at=datetime.utcnow() - timedelta(minutes=finished_minutes_ago),
        ))
        await s.commit()
        return run_id


@pytest.fixture()
def no_langfuse(monkeypatch):
    calls = []

    async def fake_write(trace_id, scores):
        calls.append((trace_id, scores))

    monkeypatch.setattr(
        "app.services.evolution.langfuse_api.write_scores", fake_write)
    return calls


class TestEvaluateRun:
    @pytest.mark.asyncio
    async def test_claim_idempotent(self, no_langfuse):
        run_id = await _mk_run("claim-1")
        async with AsyncSessionLocal() as s:
            run = (await s.get(AgentRun, run_id))
            first = await evaluate_run(s, run)
        assert first is not None
        async with AsyncSessionLocal() as s:
            run = (await s.get(AgentRun, run_id))
            assert await evaluate_run(s, run) is None, "UNIQUE(run_id) 防重复评估"
        async with AsyncSessionLocal() as s:
            rows = (await s.execute(select(AgentEvaluation))).scalars().all()
        assert len(rows) == 1

    @pytest.mark.asyncio
    async def test_cancelled_skips_judge(self, no_langfuse, monkeypatch):
        async def must_not_call(*args, **kwargs):
            raise AssertionError("cancelled 不应触发 judge")

        monkeypatch.setattr(
            "app.services.evolution.judge.judge_run", must_not_call)
        run_id = await _mk_run("claim-2")
        async with AsyncSessionLocal() as s:
            run = (await s.get(AgentRun, run_id))
            await evaluate_run(s, run)
        async with AsyncSessionLocal() as s:
            ev = (await s.execute(select(AgentEvaluation))).scalar_one()
        assert ev.status == "computed"
        assert ev.success is False
        assert ev.overall == ev.deterministic["deterministic_score"]
        assert not ev.judge_scores

    @pytest.mark.asyncio
    async def test_completed_uses_judge_and_writes_scores(self, no_langfuse, monkeypatch):
        async def fake_judge(db, run, deterministic):
            return {"dimensions": {"efficiency": 6}, "verdict": "fail",
                    "overall": 5.5, "judge_model_config_id": 7}

        monkeypatch.setattr(
            "app.services.evolution.judge.judge_run", fake_judge)
        run_id = await _mk_run("claim-3", status="completed")
        async with AsyncSessionLocal() as s:
            run = (await s.get(AgentRun, run_id))
            await evaluate_run(s, run)
        async with AsyncSessionLocal() as s:
            ev = (await s.execute(select(AgentEvaluation))).scalar_one()
        assert ev.status == "computed"
        assert ev.overall == 5.5
        assert ev.success is False          # verdict=fail
        assert ev.judge_model_config_id == 7
        assert len(no_langfuse) == 1
        names = [x["name"] for x in no_langfuse[0][1]]
        assert "agent_deterministic_score" in names
        assert "agent_overall" in names
        assert "agent_efficiency" in names

    @pytest.mark.asyncio
    async def test_judge_error_keeps_deterministic(self, no_langfuse, monkeypatch):
        from app.services.evolution.judge import JudgeError

        async def fake_judge(db, run, deterministic):
            raise JudgeError("judge 输出不含 JSON")

        monkeypatch.setattr(
            "app.services.evolution.judge.judge_run", fake_judge)
        run_id = await _mk_run("claim-4", status="completed")
        async with AsyncSessionLocal() as s:
            run = (await s.get(AgentRun, run_id))
            await evaluate_run(s, run)
        async with AsyncSessionLocal() as s:
            ev = (await s.execute(select(AgentEvaluation))).scalar_one()
        assert ev.status == "computed"
        assert ev.overall == ev.deterministic["deterministic_score"]
        assert "judge_failed" in (ev.error or "")

    @pytest.mark.asyncio
    async def test_unexpected_error_marks_failed_and_retries(self, no_langfuse, monkeypatch):
        async def boom(db, run):
            raise RuntimeError("指标计算崩溃")

        monkeypatch.setattr(
            "app.services.evolution.metrics.compute_deterministic", boom)
        run_id = await _mk_run("claim-5")
        async with AsyncSessionLocal() as s:
            run = (await s.get(AgentRun, run_id))
            assert await evaluate_run(s, run) is None
        async with AsyncSessionLocal() as s:
            ev = (await s.execute(select(AgentEvaluation))).scalar_one()
        assert ev.status == "failed"
        assert ev.attempts == 1
        assert "指标计算崩溃" in (ev.error or "")

        # 接管重试：故障恢复后二次评估成功（走 UNIQUE 冲突的更新路径）
        async def recovered(db, run):
            return {"deterministic_score": 6.0, "success": True}

        monkeypatch.setattr(
            "app.services.evolution.metrics.compute_deterministic", recovered)
        async with AsyncSessionLocal() as s:
            run = (await s.get(AgentRun, run_id))
            assert await evaluate_run(s, run) is not None
        async with AsyncSessionLocal() as s:
            ev = (await s.execute(select(AgentEvaluation))).scalar_one()
        assert ev.status == "computed"
        assert ev.overall == 6.0


class TestEvaluatePending:
    @pytest.mark.asyncio
    async def test_scan_filters(self, no_langfuse):
        await _mk_run("pend-old-1", finished_minutes_ago=5)
        await _mk_run("pend-old-2", finished_minutes_ago=10)
        await _mk_run("pend-fresh", finished_minutes_ago=0)
        await _mk_run("pend-running", status="running", finished_minutes_ago=30)
        async with AsyncSessionLocal() as s:
            s.add(AgentEvaluation(run_id="pend-evaluated", agent_id=1,
                                  status="computed"))
            s.add(AgentRun(id="pend-evaluated", agent_id=1, thread_id="t",
                           status="cancelled",
                           finished_at=datetime.utcnow() - timedelta(minutes=9)))
            await s.commit()

        done = await evaluate_pending_runs(limit=10)
        assert done == 2, "只评估终态、30s 前完成、未评估过的 Run"
        async with AsyncSessionLocal() as s:
            evaluated = set((await s.execute(
                select(AgentEvaluation.run_id))).scalars().all())
        assert {"pend-old-1", "pend-old-2"} <= evaluated
        assert "pend-fresh" not in evaluated
        assert "pend-running" not in evaluated
