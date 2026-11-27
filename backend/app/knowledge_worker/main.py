"""Worker 主循环。"""
from __future__ import annotations

import asyncio
import logging
import os
import signal
import socket
import uuid
from typing import Optional

from app.core.config import settings
from app.core.database import AsyncSessionLocal, init_db
from app.services.knowledge.ingestion import (
    IngestionRunner,
    claim_job,
)

logger = logging.getLogger(__name__)


def _worker_id() -> str:
    return f"knowledge-{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:6]}"


async def run_once(worker_id: str = "") -> bool:
    """领取并执行一个 Job；返回是否有任务被执行（测试/运维单步用）。"""
    worker_id = worker_id or _worker_id()
    lease_seconds = int(settings.KNOWLEDGE_JOB_LEASE_SECONDS or 120)
    async with AsyncSessionLocal() as db:
        job = await claim_job(db, worker_id, lease_seconds=lease_seconds)
    if job is None:
        return False
    await IngestionRunner(worker_id).run(job.id)
    return True


async def _loop(stop: asyncio.Event, worker_id: str, index: int) -> None:
    poll = max(0.5, float(settings.KNOWLEDGE_WORKER_POLL_INTERVAL or 2.0))
    while not stop.is_set():
        try:
            worked = await run_once(worker_id)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Ingestion Worker #%s 轮询异常：%s", index, exc)
            worked = False
        if not worked:
            try:
                await asyncio.wait_for(stop.wait(), timeout=poll)
            except asyncio.TimeoutError:
                pass


async def run_worker() -> None:
    settings.validate_startup()
    await init_db()
    worker_id = _worker_id()
    concurrency = max(1, int(settings.KNOWLEDGE_WORKER_CONCURRENCY or 2))
    if settings.DATABASE_URL.startswith("sqlite"):
        # SQLite 单写者：多并发只会互相抢锁
        concurrency = 1
    stop = asyncio.Event()

    def _stop(*_args):
        logger.info("收到停止信号，等待当前任务结束…")
        stop.set()

    try:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, _stop)
            except (NotImplementedError, RuntimeError):
                pass
    except Exception:  # noqa: BLE001
        pass

    logger.info("Knowledge Ingestion Worker 启动：%s（并发 %s）", worker_id, concurrency)
    tasks = [asyncio.create_task(_loop(stop, worker_id, i)) for i in range(concurrency)]
    try:
        await asyncio.gather(*tasks)
    finally:
        for task in tasks:
            task.cancel()
        logger.info("Knowledge Ingestion Worker 已停止：%s", worker_id)


def main() -> None:
    logging.basicConfig(
        level=getattr(logging, (settings.LOG_LEVEL or "INFO").upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    asyncio.run(run_worker())


if __name__ == "__main__":
    main()
