"""Sandbox Reconciler（后台循环）。

周期任务：
- Runner 心跳超时 -> offline；
- Job 租约过期 -> 重新入队 / 终态失败；
- 空闲 Sandbox -> 入队 stop（只释放计算资源，保留 Volume）；
- Ready Sandbox 的 Service Observed State 同步；
- Desired / Observed 不一致告警与指标；
- 运行指标（Runner 在线、队列、预留资源）。
"""
from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

from sqlalchemy import func, select

from app.core.config import settings
from app.core.database import AsyncSessionLocal
from app.models.sandbox import SandboxInstance, SandboxJob, SandboxRunnerNode, Workspace
from app.observability import metrics
from app.services.sandbox import jobs, runtime, scheduler
from app.services.sandbox.common import emit_event, utcnow
from app.services.sandbox.service_manager import sync_service_states

logger = logging.getLogger("ai_plat.sandbox.reconciler")


class SandboxReconciler:
    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._loop(), name="sandbox-reconciler")
        logger.info(
            "Sandbox Reconciler 启动（interval=%.1fs）",
            float(settings.SANDBOX_RECONCILE_INTERVAL),
        )

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._task = None

    async def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.warning("Sandbox Reconciler 本轮失败（下轮继续）: %s", exc)
            try:
                await asyncio.wait_for(
                    self._stop.wait(),
                    timeout=max(5.0, float(settings.SANDBOX_RECONCILE_INTERVAL)),
                )
            except asyncio.TimeoutError:
                continue

    async def run_once(self) -> dict:
        summary = {"offline": 0, "recovered": 0, "idle": 0, "services": 0, "mismatch": 0}
        async with AsyncSessionLocal() as db:
            summary["offline"] = await runtime.mark_stale_runners_offline(db)
            summary["recovered"] = await jobs.recover_expired_jobs(db)
            if summary["recovered"]:
                metrics.inc_sandbox_job_retry()
            summary["idle"] = await runtime.idle_reclaim(db)

            # Service Observed State 同步（每轮限量，避免阻塞 API 事件循环）
            ready_rows = (await db.execute(
                select(SandboxInstance, Workspace)
                .join(Workspace, Workspace.id == SandboxInstance.workspace_id)
                .where(
                    SandboxInstance.observed_state == "ready",
                    Workspace.deleted_at.is_(None),
                )
                .order_by(SandboxInstance.last_observed_at.asc())
                .limit(5)
            )).all()
            for sandbox, workspace in ready_rows:
                changed = await sync_service_states(db, workspace, sandbox)
                summary["services"] += changed

            # Desired / Observed 不一致（不自动接管，只告警 + 指标）
            mismatched = (await db.execute(
                select(SandboxInstance).where(
                    SandboxInstance.desired_state == "running",
                    SandboxInstance.observed_state.in_(("failed", "stopped")),
                )
            )).scalars().all()
            for sandbox in mismatched:
                last = sandbox.last_observed_at or sandbox.created_at
                if last is not None and utcnow() - last < timedelta(seconds=60):
                    continue
                summary["mismatch"] += 1
                metrics.inc_sandbox_reconcile_mismatch()
                await emit_event(
                    db, "sandbox.reconcile_mismatch",
                    workspace_id=sandbox.workspace_id, sandbox_id=sandbox.id,
                    generation=sandbox.generation,
                    payload={"desired_state": sandbox.desired_state,
                             "observed_state": sandbox.observed_state},
                )

            await self._refresh_metrics(db)
            await db.commit()
        return summary

    async def _refresh_metrics(self, db) -> None:
        runners = (await db.execute(select(SandboxRunnerNode))).scalars().all()
        now = utcnow()
        online = 0
        cpu_reserved = 0
        memory_reserved = 0
        for runner in runners:
            if runner.status == "online":
                online += 1
            age = (now - runner.last_heartbeat_at).total_seconds() if runner.last_heartbeat_at else 0
            metrics.set_runner_heartbeat_age(runner.node_name, age)
            cpu_reserved += int(runner.cpu_reserved_millis or 0)
            memory_reserved += int(runner.memory_reserved_bytes or 0)
        active_count = (await db.execute(
            select(func.count(SandboxInstance.id)).where(
                SandboxInstance.observed_state.in_(scheduler.ACTIVE_OBSERVED_STATES),
                SandboxInstance.desired_state != "deleted",
            )
        )).scalar() or 0
        queue_depth = (await db.execute(
            select(func.count(SandboxJob.id)).where(SandboxJob.status == "queued")
        )).scalar() or 0
        metrics.set_sandbox_capacity(
            online, int(active_count), int(queue_depth), cpu_reserved, memory_reserved,
        )


_reconciler = SandboxReconciler()


def get_reconciler() -> SandboxReconciler:
    return _reconciler
