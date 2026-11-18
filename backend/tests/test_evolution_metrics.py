"""自进化：确定性指标与聚合（metrics.py）。

运行：
    pytest backend/tests/test_evolution_metrics.py -q
"""
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import delete

from app.core.database import AsyncSessionLocal, engine, init_db
from app.models.agent import (
    Agent, AgentApproval, AgentRun, AgentRunEvent, AgentToolExecution,
)
from app.models.evolution import AgentEvaluation
from app.services.evolution.metrics import (
    aggregate_eval_rows, aggregate_metrics, compute_deterministic,
)

_CLEAN = (AgentRunEvent, AgentApproval, AgentToolExecution,
          AgentEvaluation, AgentRun, Agent)


@pytest_asyncio.fixture(autouse=True)
async def _db():
    await init_db()
    async with AsyncSessionLocal() as s:
        for model in _CLEAN:
            await s.execute(delete(model))
        await s.commit()
    yield
    await engine.dispose()


async def _mk_run(**overrides) -> str:
    async with AsyncSessionLocal() as s:
        run = AgentRun(
            id=overrides.get("id", "evm-run-1"),
            agent_id=overrides.get("agent_id", 1),
            thread_id="t-evm",
            status=overrides.get("status", "completed"),
            budget_steps=overrides.get("steps", 3),
            budget_model_calls=overrides.get("model_calls", 3),
            budget_tool_calls=overrides.get("tool_calls", 2),
            budget_tokens=overrides.get("tokens", 500),
            started_at=overrides.get("started_at", datetime.utcnow() - timedelta(seconds=30)),
            finished_at=overrides.get("finished_at", datetime.utcnow()),
            error=overrides.get("error"),
        )
        s.add(run)
        await s.commit()
        return run.id


class TestComputeDeterministic:
    @pytest.mark.asyncio
    async def test_completed_run_basic(self):
        run_id = await _mk_run()
        async with AsyncSessionLocal() as s:
            run = (await s.get(AgentRun, run_id))
            m = await compute_deterministic(s, run)
        assert m["success"] is True
        assert m["limit_exceeded"] is False
        assert m["steps"] == 3 and m["tokens"] == 500
        assert m["duration_s"] is not None and m["duration_s"] >= 29
        assert m["deterministic_score"] == 10.0

    @pytest.mark.asyncio
    async def test_tool_error_and_repeat(self):
        run_id = await _mk_run()
        async with AsyncSessionLocal() as s:
            for i, (status, args_hash) in enumerate([
                ("succeeded", "h1"), ("succeeded", "h1"), ("failed", "h2"),
            ]):
                s.add(AgentToolExecution(
                    run_id=run_id, tool_call_id=f"c{i}", tool_name="search",
                    status=status, args_hash=args_hash,
                    attempt_count=2 if status == "failed" else 1,
                ))
            await s.commit()
            run = (await s.get(AgentRun, run_id))
            m = await compute_deterministic(s, run)
        assert m["tool_total"] == 3
        assert m["tool_failed"] == 1
        assert m["tool_error_rate"] == pytest.approx(1 / 3, abs=1e-3)
        assert m["tool_repeat_calls"] == 1
        assert m["tool_retried"] == 1
        assert m["unique_tools"] == 1
        assert m["deterministic_score"] < 10.0

    @pytest.mark.asyncio
    async def test_hitl_and_recovery(self):
        run_id = await _mk_run()
        async with AsyncSessionLocal() as s:
            for i, decision in enumerate(["approve", "reject", "timeout"]):
                s.add(AgentApproval(
                    run_id=run_id, tool_name="danger", decision=decision,
                    action_id=f"a{i}"))
            s.add(AgentRunEvent(run_id=run_id, sequence=1, event_type="run.retrying"))
            s.add(AgentRunEvent(run_id=run_id, sequence=2, event_type="run.recovered"))
            await s.commit()
            run = (await s.get(AgentRun, run_id))
            m = await compute_deterministic(s, run)
        assert m["hitl_total"] == 3
        assert m["hitl_rejected"] == 2
        assert m["recovery_events"] == 2

    @pytest.mark.asyncio
    async def test_score_bounds(self):
        run_id = await _mk_run(
            id="evm-run-bad", status="limit_exceeded", error="boom")
        async with AsyncSessionLocal() as s:
            for i in range(4):
                s.add(AgentToolExecution(
                    run_id=run_id, tool_call_id=f"x{i}", tool_name="t",
                    status="failed", args_hash=f"h{i}"))
            await s.commit()
            run = (await s.get(AgentRun, run_id))
            m = await compute_deterministic(s, run)
        assert m["success"] is False
        assert 0.0 <= m["deterministic_score"] <= 10.0


class TestAggregate:
    def _row(self, **kw) -> SimpleNamespace:
        base = {
            "success": True, "overall": 8.0,
            "deterministic": {
                "deterministic_score": 8.5, "steps": 4, "tool_calls": 2,
                "tokens": 700, "duration_s": 12.0, "tool_error_rate": 0.0,
                "tool_repeat_calls": 0, "limit_exceeded": False,
                "hitl_total": 0, "hitl_rejected": 0,
            },
        }
        base.update(kw)
        return SimpleNamespace(**base)

    def test_aggregate_eval_rows(self):
        rows = [
            self._row(),
            self._row(success=False, overall=4.0, deterministic={
                "deterministic_score": 3.0, "steps": 10, "tool_calls": 8,
                "tokens": 3000, "duration_s": 60.0, "tool_error_rate": 0.5,
                "tool_repeat_calls": 2, "limit_exceeded": True,
                "hitl_total": 2, "hitl_rejected": 1,
            }),
        ]
        agg = aggregate_eval_rows(rows)
        assert agg["sample_count"] == 2
        assert agg["success_rate"] == 0.5
        assert agg["avg_overall"] == 6.0
        assert agg["tool_error_rate"] == 0.25
        assert agg["limit_exceeded_rate"] == 0.5
        assert agg["hitl_total"] == 2 and agg["hitl_rejected"] == 1

    def test_aggregate_empty(self):
        assert aggregate_eval_rows([]) == {"sample_count": 0}

    @pytest.mark.asyncio
    async def test_aggregate_metrics_filters(self):
        run_id = await _mk_run()
        async with AsyncSessionLocal() as s:
            s.add(AgentEvaluation(
                run_id=run_id, agent_id=1, status="computed",
                success=True, overall=7.0,
                deterministic={"deterministic_score": 7.0}))
            s.add(AgentEvaluation(
                run_id="evm-skip", agent_id=1, status="pending"))
            s.add(AgentEvaluation(
                run_id="evm-other", agent_id=2, status="computed",
                success=False, overall=1.0, deterministic={}))
            await s.commit()
            agg = await aggregate_metrics(s, 1, datetime.utcnow() - timedelta(hours=1))
        assert agg["sample_count"] == 1
        assert agg["success_rate"] == 1.0
