"""会话索引 Worker：独立轮询 Durable Job，不阻塞 Run Worker / 请求进程。

- Job 与消息写入同事务入队；本 Worker 只负责执行；
- CAS 领取 + lease 过期回收；失败按 attempts 重试，超限标记 failed（不静默成功）；
- Embedding 故障只降级条目（degraded），不影响对话与全文检索。
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime
from typing import Optional

from app.core.config import settings
from app.core.database import AsyncSessionLocal
from app.models.conversation_memory import ConversationIndexJob

from . import indexer, repository

logger = logging.getLogger("ai_plat.conversation_memory")


class ConversationIndexWorker:
    def __init__(self) -> None:
        self._task: Optional[asyncio.Task] = None
        self._stopping = False
        self.interval = max(0.5, float(
            getattr(settings, "CONVERSATION_MEMORY_INDEX_INTERVAL", 2.0) or 2.0
        ))
        self.batch = max(1, int(
            getattr(settings, "CONVERSATION_MEMORY_INDEX_BATCH", 20) or 20
        ))

    async def start(self) -> None:
        if self._task is not None:
            return
        if not bool(getattr(settings, "CONVERSATION_MEMORY_INDEX_ENABLED", True)):
            logger.info("会话索引 Worker 未启用（CONVERSATION_MEMORY_INDEX_ENABLED=false）")
            return
        self._stopping = False
        self._task = asyncio.create_task(self._loop(), name="conversation-index-worker")
        logger.info("会话索引 Worker 启动（interval=%.1fs batch=%d）", self.interval, self.batch)

    async def stop(self) -> None:
        self._stopping = True
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except (asyncio.CancelledError, Exception):
            pass
        self._task = None

    async def _loop(self) -> None:
        while not self._stopping:
            try:
                await self.pump()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 索引失败不影响主链路
                logger.exception("会话索引 Worker 轮询失败")
            await asyncio.sleep(self.interval)

    async def pump(self, limit: Optional[int] = None) -> int:
        """处理最多 ``limit`` 个任务，返回完成任务数（测试可直接调用）。"""
        processed = 0
        rounds = limit if limit is not None else self.batch
        for _ in range(max(1, rounds)):
            async with AsyncSessionLocal() as db:
                job = await repository.claim_next_job(db)
                await db.commit()
            if job is None:
                break
            processed += 1
            await self._run_job(job)
        return processed

    async def _run_job(self, job: ConversationIndexJob) -> None:
        started = time.monotonic()
        try:
            async with AsyncSessionLocal() as db:
                stats = await self._process(db, job)
                await repository.finish_job(db, job.id, status="done")
                await db.commit()
            elapsed = time.monotonic() - started
            self._record_metrics(job, "done", elapsed)
            logger.info(
                "会话索引任务完成 type=%s session=%s user=%s stats=%s",
                job.job_type, job.session_id, job.user_id,
                getattr(stats, "units", "-"),
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning("会话索引任务失败 id=%s type=%s: %s", job.id, job.job_type, exc)
            self._record_metrics(job, "failed", time.monotonic() - started)
            async with AsyncSessionLocal() as db:
                fresh = await db.get(ConversationIndexJob, int(job.id))
                if fresh is not None:
                    status = await repository.retry_or_fail_job(db, fresh, error=str(exc))
                    await db.commit()
                    logger.info("会话索引任务 id=%s 重试状态=%s", job.id, status)

    @staticmethod
    def _record_metrics(job: ConversationIndexJob, status: str, elapsed: float) -> None:
        try:
            from app.observability import metrics

            metrics.observe_conversation_index(job.job_type, status, elapsed)
            if status == "done" and job.created_at is not None:
                metrics.observe_conversation_index_lag(
                    max(0.0, (datetime.utcnow() - job.created_at).total_seconds())
                )
        except Exception:  # noqa: BLE001 指标失败不影响索引
            pass

    @staticmethod
    async def _process(db, job: ConversationIndexJob):
        if job.job_type == "delete_conversation":
            if job.session_id is not None:
                await repository.revoke_shares_for_session(db, int(job.session_id))
                return await indexer.delete_session_index(db, int(job.session_id))
            return 0
        if job.job_type == "reindex_user":
            if job.user_id is None:
                return indexer.IndexStats(errors=["reindex_user 缺少 user_id"])
            return await indexer.reindex_user(db, int(job.user_id))
        # 默认 index：按消息增量
        if job.session_id is None:
            return indexer.IndexStats(errors=["index 任务缺少 session_id"])
        ids = [str(mid) for mid in (job.message_ids or []) if mid]
        if not ids:
            return indexer.IndexStats()
        return await indexer.index_message(db, int(job.session_id), ids[0])


_WORKER: Optional[ConversationIndexWorker] = None


def get_conversation_index_worker() -> ConversationIndexWorker:
    global _WORKER
    if _WORKER is None:
        _WORKER = ConversationIndexWorker()
    return _WORKER
