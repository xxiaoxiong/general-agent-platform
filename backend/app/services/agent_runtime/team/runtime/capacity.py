"""Provider / Model 维度并发容量（可插拔）。

默认实现为进程内 per-key 信号量；多 Worker 部署时各进程独立限流，
不会阻塞主事件循环。后续可替换为 Redis 容量服务的同接口实现。
"""
from __future__ import annotations

import asyncio
import contextlib
from typing import AsyncIterator, Optional


class ModelCapacityLimiter:
    """按模型键（provider:model）限流的进程内信号量。

    信号量绑定创建时的事件循环：检测到运行循环变化（测试每用例一个新循环、
    Worker 重启等）时丢弃旧信号量并重建，避免在新循环上 await 旧循环的
    Semaphore 造成挂起。
    """

    def __init__(self, max_concurrency: int = 4, min_interval: float = 0.0):
        self.max_concurrency = max(0, int(max_concurrency))
        self.min_interval = max(0.0, float(min_interval))
        self._semaphores: dict[str, asyncio.Semaphore] = {}
        self._last_started: dict[str, float] = {}
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    def _ensure_loop(self) -> None:
        loop = asyncio.get_running_loop()
        if self._loop is loop:
            return
        self._loop = loop
        self._semaphores.clear()
        self._last_started.clear()

    def _semaphore(self, key: str) -> Optional[asyncio.Semaphore]:
        if self.max_concurrency <= 0:
            return None
        self._ensure_loop()
        semaphore = self._semaphores.get(key)
        if semaphore is None:
            semaphore = asyncio.Semaphore(self.max_concurrency)
            self._semaphores[key] = semaphore
        return semaphore

    @contextlib.asynccontextmanager
    async def slot(self, key: str) -> AsyncIterator[None]:
        semaphore = self._semaphore(key or "default")
        if semaphore is None:
            await self._pace(key)
            yield
            return
        async with semaphore:
            await self._pace(key)
            yield

    async def _pace(self, key: str) -> None:
        """同一模型相邻调用最小间隔（供应商限流保护；0=不限制）。"""
        if self.min_interval <= 0:
            return
        loop = asyncio.get_running_loop()
        now = loop.time()
        last = self._last_started.get(key, 0.0)
        wait = last + self.min_interval - now
        if wait > 0:
            await asyncio.sleep(wait)
        self._last_started[key] = loop.time()


_GLOBAL_LIMITER: Optional[ModelCapacityLimiter] = None


def get_capacity_limiter() -> ModelCapacityLimiter:
    global _GLOBAL_LIMITER
    if _GLOBAL_LIMITER is None:
        from app.core.config import settings

        _GLOBAL_LIMITER = ModelCapacityLimiter(
            getattr(settings, "AGENT_TEAM_MODEL_MAX_CONCURRENCY", 4),
            getattr(settings, "AGENT_TEAM_MODEL_MIN_INTERVAL", 0.0),
        )
    return _GLOBAL_LIMITER
