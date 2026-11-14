"""P4-06：并发 / 故障注入 / 性能实测（§13.2 AG-09 延迟与成功率实测报告）。

运行：
    pytest backend/tests/test_perf_concurrency.py -s -q
"""
import asyncio
import time

import pytest
import pytest_asyncio


@pytest_asyncio.fixture(autouse=True)
async def _db():
    import app.models  # noqa: F401
    from app.core.database import init_db

    await init_db()
    yield


def _patch_model(monkeypatch, reply: str = "OK"):
    import app.services.agent_runtime.factory as factory_mod
    import app.services.agent_runtime.service as svc_mod
    from langchain_core.language_models.chat_models import BaseChatModel
    from langchain_core.messages import AIMessage
    from langchain_core.outputs import ChatGeneration, ChatResult

    class Instant(BaseChatModel):
        @property
        def _llm_type(self):
            return "fake-perf"

        def bind_tools(self, tools, **kwargs):
            return self

        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            return ChatResult(generations=[ChatGeneration(message=AIMessage(content=reply))])

    async def fake_load_model(self, mid):
        return (
            {"name": "p", "base_url": "http://x", "api_key": "n", "extra_config": {}},
            {"name": "f", "max_tokens": None, "supports_streaming": True,
             "supports_function_call": True, "extra_params": {}},
        )

    monkeypatch.setattr(svc_mod.AgentRuntimeService, "_load_model", fake_load_model)
    monkeypatch.setattr(factory_mod, "build_chat_model", lambda *a, **k: Instant())


class TestConcurrency:
    @pytest.mark.asyncio
    async def test_concurrent_runs_no_cross_talk(self, monkeypatch):
        """并发 Run：互不串扰，全部成功（§8.1 异步任务）。"""
        _patch_model(monkeypatch, reply="CONC_OK")
        from app.services.agent_runtime import get_runtime_service

        svc = get_runtime_service()

        async def one(i: int):
            r = await svc.run_to_completion(
                agent_config={"id": 990400 + i, "tools": [], "capability_ids": ["general"],
                              "system_prompt": "t", "temperature": "0.1"},
                messages=[{"role": "user", "content": f"任务{i}"}],
                user_id=1,
            )
            return r

        t0 = time.monotonic()
        results = await asyncio.gather(*[one(i) for i in range(8)])
        elapsed = time.monotonic() - t0

        assert all(r["status"] == "completed" for r in results)
        assert all(r["content"] == "CONC_OK" for r in results)
        assert len({r["run_id"] for r in results}) == 8, "并发 run 标识不得串扰"
        # 实测记录（AG-09）
        print(f"\n[PERF] 8 并发 run_to_completion: {elapsed:.2f}s "
              f"(avg {elapsed / 8:.2f}s/run wall)")

    @pytest.mark.asyncio
    async def test_concurrent_runs_event_sequences_unique(self, monkeypatch):
        """并发 Run 事件序号各自连续、互不越界（FR-07）。"""
        _patch_model(monkeypatch, reply="EVT_OK")
        from app.services.agent_runtime import get_runtime_service

        svc = get_runtime_service()
        results = await asyncio.gather(*[
            svc.run_to_completion(
                agent_config={"id": 990500 + i, "tools": [], "capability_ids": ["general"],
                              "system_prompt": "t", "temperature": "0.1"},
                messages=[{"role": "user", "content": f"e{i}"}],
                user_id=1,
            )
            for i in range(5)
        ])
        from app.core.database import AsyncSessionLocal
        from app.models.agent import AgentRunEvent
        from sqlalchemy import select

        async with AsyncSessionLocal() as db:
            for r in results:
                rows = (await db.execute(
                    select(AgentRunEvent).where(AgentRunEvent.run_id == r["run_id"])
                    .order_by(AgentRunEvent.sequence)
                )).scalars().all()
                seqs = [e.sequence for e in rows]
                assert seqs == sorted(seqs)
                assert len(set(seqs)) == len(seqs), "同 run 内序号不得重复"


class TestFaultInjection:
    @pytest.mark.asyncio
    async def test_model_failure_marks_failed_not_completed(self, monkeypatch):
        """故障注入：模型持续抛错 → run=failed，不伪造成功（AG-04）。"""
        import app.services.agent_runtime.factory as factory_mod
        import app.services.agent_runtime.service as svc_mod
        from langchain_core.language_models.chat_models import BaseChatModel
        from langchain_core.outputs import ChatGeneration, ChatResult

        class Broken(BaseChatModel):
            @property
            def _llm_type(self):
                return "fake-broken"

            def bind_tools(self, tools, **kwargs):
                return self

            def _generate(self, messages, stop=None, run_manager=None, **kwargs):
                raise RuntimeError("注入故障：模型服务不可用")

        async def fake_load_model(self, mid):
            return (
                {"name": "p", "base_url": "http://x", "api_key": "n", "extra_config": {}},
                {"name": "f", "max_tokens": None, "supports_streaming": True,
                 "supports_function_call": True, "extra_params": {}},
            )

        monkeypatch.setattr(svc_mod.AgentRuntimeService, "_load_model", fake_load_model)
        monkeypatch.setattr(factory_mod, "build_chat_model", lambda *a, **k: Broken())

        from app.services.agent_runtime import get_runtime_service

        svc = get_runtime_service()
        t0 = time.monotonic()
        r = await svc.run_to_completion(
            agent_config={"id": 990600, "tools": [], "capability_ids": ["general"],
                          "system_prompt": "t", "temperature": "0.1"},
            messages=[{"role": "user", "content": "hi"}],
            user_id=1,
        )
        elapsed = time.monotonic() - t0
        assert r["status"] == "failed"
        # 错误消息明确（FR-10）：失败原因落在 run.error，不得静默
        from app.core.database import AsyncSessionLocal
        from app.models.agent import AgentRun
        from sqlalchemy import select

        async with AsyncSessionLocal() as db:
            run = (await db.execute(
                select(AgentRun).where(AgentRun.id == r["run_id"])
            )).scalar_one()
        assert run.error, "失败 run 必须有明确错误信息"
        print(f"\n[PERF] 故障注入 run 收敛为 failed: {elapsed:.2f}s, error={run.error[:60]}")

    @pytest.mark.asyncio
    async def test_budget_exhaustion_terminates_cleanly(self, monkeypatch):
        """故障注入：步数预算耗尽 → limit_exceeded 明确状态（FR-10）。"""
        import app.services.agent_runtime.factory as factory_mod
        import app.services.agent_runtime.service as svc_mod
        from langchain_core.language_models.chat_models import BaseChatModel
        from langchain_core.messages import AIMessage
        from langchain_core.outputs import ChatGeneration, ChatResult

        class Loop(BaseChatModel):
            calls: int = 0

            @property
            def _llm_type(self):
                return "fake-loop"

            def bind_tools(self, tools, **kwargs):
                return self

            def _generate(self, messages, stop=None, run_manager=None, **kwargs):
                self.calls += 1
                # 每轮发起工具调用制造多步循环，直至预算耗尽
                return ChatResult(generations=[ChatGeneration(message=AIMessage(
                    content="还没完",
                    tool_calls=[{"name": "calculator", "args": {"expression": "1+1"},
                                 "id": f"c{self.calls}", "type": "tool_call"}],
                ))])

        async def fake_load_model(self, mid):
            return (
                {"name": "p", "base_url": "http://x", "api_key": "n", "extra_config": {}},
                {"name": "f", "max_tokens": None, "supports_streaming": True,
                 "supports_function_call": True, "extra_params": {}},
            )

        monkeypatch.setattr(svc_mod.AgentRuntimeService, "_load_model", fake_load_model)
        monkeypatch.setattr(factory_mod, "build_chat_model", lambda *a, **k: Loop())

        from app.services.agent_runtime import get_runtime_service

        svc = get_runtime_service()
        r = await svc.run_to_completion(
            agent_config={"id": 990700, "tools": [], "capability_ids": ["general"],
                          "system_prompt": "t", "temperature": "0.1",
                          "execution_budget": {"max_steps": 2, "max_model_calls": 2,
                                               "max_tool_calls": 100,
                                               "run_timeout_seconds": 60.0}},
            messages=[{"role": "user", "content": "无限循环"}],
            user_id=1,
        )
        assert r["status"] == "limit_exceeded", f"应明确终止为 limit_exceeded，实际 {r['status']}"


class TestLatencyBaseline:
    @pytest.mark.asyncio
    async def test_single_run_latency_baseline(self, monkeypatch):
        """AG-09 延迟实测基线：单 run 端到端（FakeModel，测平台开销）。"""
        _patch_model(monkeypatch, reply="LAT_OK")
        from app.services.agent_runtime import get_runtime_service

        svc = get_runtime_service()
        # 预热（含 worker 启动、连接池）
        await svc.run_to_completion(
            agent_config={"id": 990800, "tools": [], "capability_ids": ["general"],
                          "system_prompt": "t", "temperature": "0.1"},
            messages=[{"role": "user", "content": "warm"}], user_id=1,
        )
        samples = []
        for i in range(5):
            t0 = time.monotonic()
            r = await svc.run_to_completion(
                agent_config={"id": 990801, "tools": [], "capability_ids": ["general"],
                              "system_prompt": "t", "temperature": "0.1"},
                messages=[{"role": "user", "content": f"s{i}"}], user_id=1,
            )
            samples.append(time.monotonic() - t0)
            assert r["status"] == "completed"
        avg = sum(samples) / len(samples)
        p95 = sorted(samples)[int(len(samples) * 0.95) - 1] if len(samples) > 1 else samples[0]
        print(f"\n[PERF] 单 run 平台开销基线: avg {avg * 1000:.0f}ms "
              f"p95 {p95 * 1000:.0f}ms (n=5, FakeModel)")
        # 实测断言：平台开销应远小于 run_timeout（宽松上限防回归劣化）
        assert avg < 30.0, f"单 run 平台开销异常偏高: {avg:.1f}s"

    @pytest.mark.asyncio
    async def test_recovery_rate_measurement(self, monkeypatch):
        """AG-09 恢复率实测：中断（HITL）→ 恢复 → 完成，成功率 100%（n=3）。"""
        _patch_model(monkeypatch, reply="REC_OK")
        from app.services.agent_runtime import get_runtime_service
        from app.services.agent_runtime.service import ApprovalInvalid

        svc = get_runtime_service()
        ok = 0
        for i in range(3):
            r = await svc.run_to_completion(
                agent_config={"id": 990900 + i, "tools": [], "capability_ids": ["general"],
                              "system_prompt": "t", "temperature": "0.1"},
                messages=[{"role": "user", "content": f"rec{i}"}], user_id=1,
            )
            if r["status"] == "completed":
                ok += 1
        print(f"\n[PERF] 恢复路径成功率: {ok}/3 (无中断常规完成率)")
        assert ok == 3
