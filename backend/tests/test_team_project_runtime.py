"""确认取消不会提前释放工程工作区写入的执行租约。"""

import asyncio
import threading
from contextlib import asynccontextmanager

import pytest

from app.services.agent_runtime import team_project_runtime


@pytest.mark.asyncio
async def test_cancellation_keeps_fence_until_dispatched_write_finishes(monkeypatch):
    loop = asyncio.get_running_loop()
    entered = asyncio.Event()
    allow_return = threading.Event()
    released = asyncio.Event()

    @asynccontextmanager
    async def session():
        try:
            yield object()
        finally:
            released.set()

    class Run:
        async def _check_fence(self, db):
            assert db is not None

    def write_file():
        loop.call_soon_threadsafe(entered.set)
        assert allow_return.wait(3)

    monkeypatch.setattr(team_project_runtime, "AsyncSessionLocal", session)
    task = asyncio.create_task(team_project_runtime._fenced_io(Run(), write_file))
    try:
        await asyncio.wait_for(entered.wait(), timeout=3)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert not released.is_set()
    finally:
        allow_return.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert released.is_set()
