"""P4-03：工具瞬态失败按风险策略重试 + run.retrying/recovering 事件。

运行：
    pytest backend/tests/test_tool_retry.py -q
"""
import asyncio

import pytest

from app.services.agent_runtime.events import EventBus, RUN_RECOVERING, RUN_RETRYING
from app.services.agent_runtime.tools.adapter import execute_with_retry
from app.services.agent_runtime.tools.classification import (
    CLASS_EXTERNAL_IDEMPOTENT,
    CLASS_NON_IDEMPOTENT,
    CLASS_READ_ONLY,
    RETRY_IDEMPOTENT_KEY,
    RETRY_NONE,
    RETRY_SAFE,
    SideEffectContract,
)
from app.services.tools import ToolExecutor
from app.services.tools.result import ToolResult


def _events_of(bus: EventBus, ev_type: str):
    out = []
    while not bus.empty():
        ev = bus._queue.get_nowait()
        if ev.type == ev_type:
            out.append(ev)
    return out


class FlakyExecutor(ToolExecutor):
    """前 n 次抛/失败，之后成功。"""

    def __init__(self, failures: int, mode: str = "exception", retryable: bool = True):
        super().__init__(None, {})
        self.remaining = failures
        self.mode = mode
        self.retryable = retryable
        self.calls = 0

    async def execute(self, tool_name, tool_input, *, raise_on_error=False):
        self.calls += 1
        if self.remaining > 0:
            self.remaining -= 1
            if self.mode == "exception":
                raise ConnectionError("瞬态网络错误")
            return ToolResult.failed("503 服务暂不可用", retryable=self.retryable)
        return "OK"


@pytest.fixture()
def bus():
    return EventBus(persist=False)


READ_ONLY_CONTRACT = SideEffectContract(
    tool_name="http_get", side_effect_class=CLASS_READ_ONLY, retry_policy=RETRY_SAFE,
)
NON_IDEMPOTENT_CONTRACT = SideEffectContract(tool_name="http_post")  # retry_policy=none
IDEMPOTENT_CONTRACT = SideEffectContract(
    tool_name="http_post", side_effect_class=CLASS_EXTERNAL_IDEMPOTENT,
    downstream_idempotency_supported=True, idempotency_key_header="Idempotency-Key",
    retry_policy=RETRY_IDEMPOTENT_KEY,
)


class TestToolRetryByPolicy:
    @pytest.mark.asyncio
    async def test_readonly_retries_transient_exception_then_succeeds(self, bus):
        ex = FlakyExecutor(failures=2, mode="exception")
        result = await execute_with_retry(
            "http_get", {}, ex, bus=bus, run_id="r1", contract=READ_ONLY_CONTRACT,
        )
        assert result == "OK"
        assert ex.calls == 3
        retries = _events_of(bus, RUN_RETRYING)
        assert len(retries) == 2
        assert retries[0].data["attempt"] == 1
        assert retries[0].data["tool"] == "http_get"

    @pytest.mark.asyncio
    async def test_readonly_retries_retryable_result(self, bus):
        ex = FlakyExecutor(failures=1, mode="result", retryable=True)
        result = await execute_with_retry(
            "http_get", {}, ex, bus=bus, run_id="r1", contract=READ_ONLY_CONTRACT,
        )
        assert result == "OK"
        assert ex.calls == 2
        assert len(_events_of(bus, RUN_RETRYING)) == 1

    @pytest.mark.asyncio
    async def test_non_retryable_result_not_retried(self, bus):
        """retryable=False 的失败（业务错误）不重试。"""
        ex = FlakyExecutor(failures=5, mode="result", retryable=False)
        result = await execute_with_retry(
            "http_get", {}, ex, bus=bus, run_id="r1", contract=READ_ONLY_CONTRACT,
        )
        tr = result if isinstance(result, ToolResult) else None
        assert ex.calls == 1
        assert tr is not None and tr.status == "failed"
        assert not _events_of(bus, RUN_RETRYING)

    @pytest.mark.asyncio
    async def test_non_idempotent_never_retries(self, bus):
        """非幂等（retry_policy=none）：绝不自动重放，哪怕瞬态异常。"""
        ex = FlakyExecutor(failures=3, mode="exception")
        with pytest.raises(ConnectionError):
            await execute_with_retry(
                "http_post", {}, ex, bus=bus, run_id="r1",
                contract=NON_IDEMPOTENT_CONTRACT,
            )
        assert ex.calls == 1
        assert not _events_of(bus, RUN_RETRYING)

    @pytest.mark.asyncio
    async def test_idempotent_key_no_inrun_retry(self, bus):
        """idempotent_key 工具运行内不盲目重发：重试走账本重放层复用同一幂等键
        （T-05 基线语义），运行内只执行一次。"""
        ex = FlakyExecutor(failures=1, mode="exception")
        with pytest.raises(ConnectionError):
            await execute_with_retry(
                "http_post", {"headers": {"Idempotency-Key": "k-1"}}, ex,
                bus=bus, run_id="r1", contract=IDEMPOTENT_CONTRACT,
            )
        assert ex.calls == 1
        assert not _events_of(bus, RUN_RETRYING)

    @pytest.mark.asyncio
    async def test_retries_exhausted_raises(self, bus):
        ex = FlakyExecutor(failures=10, mode="exception")
        with pytest.raises(ConnectionError):
            await execute_with_retry(
                "http_get", {}, ex, bus=bus, run_id="r1", contract=READ_ONLY_CONTRACT,
            )
        # 初始 1 次 + AGENT_TOOL_MAX_RETRIES(2) 次重试
        assert ex.calls == 3
        assert len(_events_of(bus, RUN_RETRYING)) == 2

    @pytest.mark.asyncio
    async def test_definitive_error_not_retried(self, bus):
        """确定性本地错误（ValueError）不重试。"""
        class BadInput(ToolExecutor):
            def __init__(self):
                super().__init__(None, {})
                self.calls = 0

            async def execute(self, tool_name, tool_input, *, raise_on_error=False):
                self.calls += 1
                raise ValueError("参数错误")

        ex = BadInput()
        with pytest.raises(ValueError):
            await execute_with_retry(
                "http_get", {}, ex, bus=bus, run_id="r1", contract=READ_ONLY_CONTRACT,
            )
        assert ex.calls == 1
        assert not _events_of(bus, RUN_RETRYING)

    @pytest.mark.asyncio
    async def test_zero_max_retries_disables(self, bus, monkeypatch):
        import app.core.config as config_mod
        monkeypatch.setattr(config_mod.settings, "AGENT_TOOL_MAX_RETRIES", 0, raising=False)
        ex = FlakyExecutor(failures=3, mode="exception")
        with pytest.raises(ConnectionError):
            await execute_with_retry(
                "http_get", {}, ex, bus=bus, run_id="r1", contract=READ_ONLY_CONTRACT,
            )
        assert ex.calls == 1

    @pytest.mark.asyncio
    async def test_per_call_timeout_treated_as_transient(self, bus, monkeypatch):
        """P4-05：单次工具超时按瞬态失败（safe_retry 重试；非安全策略不重发）。"""
        import app.core.config as config_mod
        monkeypatch.setattr(config_mod.settings, "AGENT_TOOL_TIMEOUT_SECONDS", 0.05,
                            raising=False)

        class Hanging(ToolExecutor):
            def __init__(self):
                super().__init__(None, {})
                self.calls = 0

            async def execute(self, tool_name, tool_input, *, raise_on_error=False):
                self.calls += 1
                await asyncio.sleep(10)

        # safe_retry（只读）：超时重试至耗尽后 raise TimeoutError
        ex = Hanging()
        with pytest.raises(asyncio.TimeoutError):
            await execute_with_retry(
                "http_get", {}, ex, bus=bus, run_id="r1", contract=READ_ONLY_CONTRACT,
            )
        assert ex.calls == 3  # 1 次 + 2 次重试
        assert len(_events_of(bus, RUN_RETRYING)) == 2

        # 非安全策略：超时只执行一次（结果未知不盲目重发）
        ex2 = Hanging()
        with pytest.raises(asyncio.TimeoutError):
            await execute_with_retry(
                "http_post", {}, ex2, bus=bus, run_id="r1",
                contract=NON_IDEMPOTENT_CONTRACT,
            )
        assert ex2.calls == 1


class TestRunRecoveringEvent:
    @pytest.mark.asyncio
    async def test_recovering_event_emitted_on_resume(self, bus):
        """恢复/续跑路径发 run.recovering（协议映射断言）。"""
        from app.services.agent_runtime.event_protocol import to_v2_event
        from app.services.agent_runtime.events import RuntimeEvent

        await bus.publish(RuntimeEvent(
            type=RUN_RECOVERING, run_id="r1",
            data={"mode": "continue", "reason": "", "resume": False},
        ))
        ev = _events_of(bus, RUN_RECOVERING)[0]
        envelope = to_v2_event(
            run_id="r1", sequence=1, event_type=RUN_RECOVERING,
            payload=ev.data,
        )
        assert envelope["event"] == "run.recovering"
        assert envelope["data"]["mode"] == "continue"
