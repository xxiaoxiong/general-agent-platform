"""Evaluation Worker：Campaign 的持久化调度（Lease / Heartbeat / 重启恢复）。

职责边界（任务书 §4.8）：
- Evaluation Worker 只负责 Campaign / Trial 的领取与状态推进；
- 真实 Agent 执行仍由现有 Agent Runtime Worker 负责；
- 进程重启后扫描 pending 或租约过期的 running Campaign 并重新领取；
- execute_eval_run 内部先检查已有 AgentRun 状态，不盲目重复执行。
"""
from __future__ import annotations

import asyncio
import logging
import socket
import uuid
from datetime import datetime
from typing import Optional, Set

from sqlalchemy import or_, select

from app.core.config import settings
from app.core.database import AsyncSessionLocal
from app.models.evaluation import EvalRun

logger = logging.getLogger("ai_plat.evaluation")


def _worker_id() -> str:
    if getattr(settings, "EVALUATION_WORKER_ID", ""):
        return str(settings.EVALUATION_WORKER_ID)
    return f"{socket.gethostname()}-{uuid.uuid4().hex[:6]}-eval"


class EvaluationWorker:
    """进程级 Campaign 调度器（多 Worker 之间靠数据库租约互斥）。"""

    def __init__(
        self,
        *,
        poll_interval: Optional[float] = None,
        batch_size: Optional[int] = None,
    ) -> None:
        self.poll_interval = (
            poll_interval if poll_interval is not None
            else float(getattr(settings, "EVALUATION_POLL_INTERVAL", 5.0) or 5.0)
        )
        self.batch_size = (
            batch_size if batch_size is not None
            else int(getattr(settings, "EVALUATION_WORKER_BATCH", 2) or 2)
        )
        self._task: Optional[asyncio.Task] = None
        self._inflight: Set[asyncio.Task] = set()
        self._stopping = False

    async def start(self) -> None:
        if not bool(getattr(settings, "EVALUATION_WORKER_ENABLED", True)):
            logger.info("Evaluation Worker 已关闭（EVALUATION_WORKER_ENABLED=false）")
            return
        if self._task is not None:
            return
        self._stopping = False
        self._task = asyncio.create_task(self._poll_loop(), name="evaluation-worker")
        logger.info("Evaluation Worker 启动（poll=%.1fs）", self.poll_interval)

    async def stop(self) -> None:
        self._stopping = True
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._task = None
        tasks = {t for t in self._inflight if not t.done()}
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        logger.info("Evaluation Worker 已停止")

    async def run_once(self) -> int:
        """扫描并领取一批 Campaign；返回本轮启动的 Campaign 数。"""
        now = datetime.utcnow()
        async with AsyncSessionLocal() as db:
            rows = (await db.execute(
                select(EvalRun.id)
                .where(
                    EvalRun.status.in_(("pending", "running")),
                    or_(EvalRun.lease_expires_at.is_(None),
                        EvalRun.lease_expires_at <= now),
                )
                .order_by(EvalRun.id)
                .limit(self.batch_size)
            )).scalars().all()
        started = 0
        for campaign_id in rows:
            if any(not t.done() for t in self._inflight):
                # 每进程串行执行 Campaign，避免并发写库与资源争抢
                break
            from app.services.evaluation.runner import execute_eval_run

            task = asyncio.create_task(execute_eval_run(campaign_id))
            self._inflight.add(task)
            task.add_done_callback(self._inflight.discard)
            started += 1
        return started

    async def _poll_loop(self) -> None:
        while not self._stopping:
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                logger.exception("Evaluation Worker 轮询失败（继续）")
            await asyncio.sleep(self.poll_interval)


_worker: Optional[EvaluationWorker] = None


def get_evaluation_worker() -> EvaluationWorker:
    global _worker
    if _worker is None:
        _worker = EvaluationWorker()
    return _worker
