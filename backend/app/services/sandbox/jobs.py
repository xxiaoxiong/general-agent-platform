"""Sandbox Job 队列（任务书 §5.4 / §8.2 / §8.3）。

- 至少一次投递：状态变更使用条件 UPDATE，天然支持多 Runner 并发领取；
- Lease + Fencing：每次领取 lease_token 单调递增，旧 token 的 heartbeat/complete 被拒绝；
- 幂等：`idempotency_key` 唯一；Provision 由调用方按标签检查同名 Generation；
- 公平性：priority 升序 + 等待超过 SANDBOX_JOB_AGING_SECONDS 自动提升优先级（防饥饿）。
"""
from __future__ import annotations

from datetime import timedelta
from typing import Optional

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.sandbox import JOB_QUEUE_KINDS, SandboxJob, SandboxRunnerNode

from .common import emit_event, lease_expiry, new_id, utcnow

QUEUE_PRIORITY = {"lifecycle": 60, "provision": 80, "recovery": 100, "cleanup": 120}


async def enqueue_job(
    db: AsyncSession,
    *,
    job_type: str,
    workspace_id: int,
    sandbox_id: Optional[int] = None,
    runner_id: Optional[int] = None,
    payload: Optional[dict] = None,
    idempotency_key: Optional[str] = None,
    priority: Optional[int] = None,
    max_attempts: Optional[int] = None,
) -> tuple[SandboxJob, bool]:
    """入队（幂等）。返回 (job, created)。调用方负责 commit。"""
    key = idempotency_key or f"{job_type}:{workspace_id}:{new_id()}"
    existing = (await db.execute(
        select(SandboxJob).where(SandboxJob.idempotency_key == key)
    )).scalar_one_or_none()
    if existing is not None:
        return existing, False
    queue_kind = JOB_QUEUE_KINDS.get(job_type, "provision")
    job = SandboxJob(
        id=new_id(),
        workspace_id=workspace_id,
        sandbox_id=sandbox_id,
        target_runner_id=runner_id,
        job_type=job_type,
        status="queued",
        priority=int(priority if priority is not None else QUEUE_PRIORITY.get(queue_kind, 100)),
        idempotency_key=key,
        attempt=0,
        max_attempts=int(max_attempts if max_attempts is not None else settings.SANDBOX_JOB_MAX_ATTEMPTS),
        payload_json=payload or {},
    )
    try:
        async with db.begin_nested():
            db.add(job)
            await db.flush()
    except IntegrityError:
        # 并发同一幂等键：以已存在记录为准
        existing = (await db.execute(
            select(SandboxJob).where(SandboxJob.idempotency_key == key)
        )).scalar_one()
        return existing, False
    await emit_event(
        db, "sandbox.job_queued",
        workspace_id=workspace_id, sandbox_id=sandbox_id, job_id=job.id,
        payload={"job_type": job_type, "priority": job.priority},
    )
    return job, True


async def claim_jobs(
    db: AsyncSession, runner: SandboxRunnerNode, limit: int = 2,
) -> list[SandboxJob]:
    """原子领取。先做 Aging，再按优先级领取 queued 或租约过期的任务。"""
    now = utcnow()
    aging = float(getattr(settings, "SANDBOX_JOB_AGING_SECONDS", 60.0) or 0)
    if aging > 0:
        await db.execute(
            update(SandboxJob)
            .where(SandboxJob.status == "queued",
                   SandboxJob.created_at < now - timedelta(seconds=aging))
            .values(priority=SandboxJob.priority - 10)
        )

    candidates = (await db.execute(
        select(SandboxJob.id, SandboxJob.status)
        .where(
            ((SandboxJob.status == "queued")
             | ((SandboxJob.status.in_(("leased", "running")))
                & (SandboxJob.lease_expires_at.is_not(None))
                & (SandboxJob.lease_expires_at < now)))
        )
        .order_by(SandboxJob.priority.asc(), SandboxJob.created_at.asc())
        .limit(max(1, int(limit)))
        .with_for_update(skip_locked=True)
    )).all()

    claimed: list[SandboxJob] = []
    for job_id, status in candidates:
        result = await db.execute(
            update(SandboxJob)
            .where(
                SandboxJob.id == job_id,
                SandboxJob.status == status,
                (SandboxJob.lease_expires_at.is_(None))
                | (SandboxJob.lease_expires_at < now)
                | (SandboxJob.status == "queued"),
            )
            .values(
                status="leased",
                lease_owner=runner.node_name,
                lease_token=SandboxJob.lease_token + 1,
                lease_expires_at=lease_expiry(),
                attempt=SandboxJob.attempt + 1,
                started_at=now,
                updated_at=now,
            )
        )
        if result.rowcount == 0:
            continue
        job = (await db.execute(
            select(SandboxJob).where(SandboxJob.id == job_id)
        )).scalar_one()
        await emit_event(
            db, "sandbox.job_leased",
            workspace_id=job.workspace_id, sandbox_id=job.sandbox_id, job_id=job.id,
            payload={"runner": runner.node_name, "attempt": job.attempt,
                     "lease_token": job.lease_token},
        )
        claimed.append(job)
    return claimed


async def heartbeat_job(
    db: AsyncSession, job_id: str, runner: SandboxRunnerNode, lease_token: int,
) -> bool:
    result = await db.execute(
        update(SandboxJob)
        .where(
            SandboxJob.id == job_id,
            SandboxJob.lease_owner == runner.node_name,
            SandboxJob.lease_token == int(lease_token),
            SandboxJob.status.in_(("leased", "running")),
        )
        .values(
            status="running",
            lease_expires_at=lease_expiry(),
            updated_at=utcnow(),
        )
    )
    return result.rowcount > 0


async def complete_job(
    db: AsyncSession, job_id: str, runner: SandboxRunnerNode,
    lease_token: int, result_payload: Optional[dict] = None,
) -> SandboxJob:
    from .common import JobLeaseLost

    result = await db.execute(
        update(SandboxJob)
        .where(
            SandboxJob.id == job_id,
            SandboxJob.lease_owner == runner.node_name,
            SandboxJob.lease_token == int(lease_token),
            SandboxJob.status.in_(("leased", "running")),
        )
        .values(
            status="succeeded",
            result_json=result_payload or {},
            finished_at=utcnow(),
            updated_at=utcnow(),
        )
    )
    if result.rowcount == 0:
        raise JobLeaseLost(f"Job {job_id} 租约已失效（fencing），拒绝完成上报")
    job = (await db.execute(select(SandboxJob).where(SandboxJob.id == job_id))).scalar_one()
    await emit_event(
        db, "sandbox.job_succeeded",
        workspace_id=job.workspace_id, sandbox_id=job.sandbox_id, job_id=job.id,
        payload={"job_type": job.job_type, "attempt": job.attempt},
    )
    return job


async def fail_job(
    db: AsyncSession, job_id: str, runner: SandboxRunnerNode, lease_token: int,
    error_code: str, error_message: str,
) -> tuple[SandboxJob, bool]:
    """失败上报：未超过 max_attempts 时重新入队（幂等重试），否则终态 failed。

    返回 (job, retry_queued)。
    """
    from .common import JobLeaseLost

    job = (await db.execute(
        select(SandboxJob).where(SandboxJob.id == job_id).with_for_update()
    )).scalar_one_or_none()
    if job is None:
        raise JobLeaseLost(f"Job {job_id} 不存在")
    if (job.lease_owner != runner.node_name or job.lease_token != int(lease_token)
            or job.status not in ("leased", "running")):
        raise JobLeaseLost(f"Job {job_id} 租约已失效（fencing），拒绝失败上报")

    retry = int(job.attempt) < int(job.max_attempts)
    job.error_code = (error_code or "sandbox_job_failed")[:64]
    job.error_message = (error_message or "")[:4000]
    job.lease_owner = None
    job.lease_expires_at = None
    job.finished_at = None if retry else utcnow()
    job.status = "queued" if retry else "failed"
    if retry:
        job.priority = max(0, int(job.priority) - 10)
    job.updated_at = utcnow()
    await emit_event(
        db, "sandbox.job_requeued" if retry else "sandbox.job_failed",
        workspace_id=job.workspace_id, sandbox_id=job.sandbox_id, job_id=job.id,
        payload={"job_type": job.job_type, "attempt": job.attempt,
                 "error_code": job.error_code,
                 "error": job.error_message[:500]},
    )
    return job, retry


async def recover_expired_jobs(db: AsyncSession) -> int:
    """租约过期的 Job 重新入队（超过 max_attempts 则 failed）。由 Reconciler 周期调用。"""
    now = utcnow()
    expired = (await db.execute(
        select(SandboxJob).where(
            SandboxJob.status.in_(("leased", "running")),
            SandboxJob.lease_expires_at.is_not(None),
            SandboxJob.lease_expires_at < now,
        ).with_for_update()
    )).scalars().all()
    for job in expired:
        retry = int(job.attempt) < int(job.max_attempts)
        job.status = "queued" if retry else "failed"
        job.lease_owner = None
        job.lease_expires_at = None
        job.finished_at = None if retry else now
        job.error_code = "lease_expired"
        job.error_message = "Job 租约过期（Runner 可能已崩溃），已自动回收"
        if retry:
            job.priority = max(0, int(job.priority) - 10)
        job.updated_at = now
        await emit_event(
            db, "sandbox.job_requeued" if retry else "sandbox.job_failed",
            workspace_id=job.workspace_id, sandbox_id=job.sandbox_id, job_id=job.id,
            payload={"job_type": job.job_type, "attempt": job.attempt,
                     "reason": "lease_expired"},
        )
    return len(expired)


def job_to_dict(job: SandboxJob) -> dict:
    return {
        "id": job.id,
        "workspace_id": job.workspace_id,
        "sandbox_id": job.sandbox_id,
        "target_runner_id": job.target_runner_id,
        "job_type": job.job_type,
        "status": job.status,
        "priority": job.priority,
        "attempt": job.attempt,
        "max_attempts": job.max_attempts,
        "error_code": job.error_code,
        "error_message": job.error_message,
        "created_at": job.created_at,
        "started_at": job.started_at,
        "finished_at": job.finished_at,
    }
