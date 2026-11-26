"""Sandbox Runner 内部 API（/internal）。

- 服务身份认证：`X-Runner-Token`（普通用户 Token 不能调用）；
- Job 领取 / 续租 / 完成 / 失败均带 Fencing Token；
- Runner 只提交白名单字段，镜像、Volume、网络与安全配置由服务端策略生成。
"""
from __future__ import annotations

import hmac
from datetime import datetime
from typing import Any, Dict, Optional

from fastapi import APIRouter, Body, Depends, Header, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import get_db
from app.models.sandbox import SandboxJob, SandboxRunnerNode
from app.observability import metrics
from app.services.sandbox import jobs, runtime
from app.services.sandbox.common import ensure_enabled


def require_runner_token(x_runner_token: str = Header(default="")) -> None:
    ensure_enabled()
    expected = (settings.SANDBOX_RUNNER_TOKEN or "").strip()
    if not expected:
        raise HTTPException(status_code=503, detail="Sandbox 内部令牌未配置")
    if not hmac.compare_digest((x_runner_token or "").strip(), expected):
        raise HTTPException(status_code=401, detail="Runner 身份校验失败")


router = APIRouter(prefix="/internal", tags=["sandbox-internal"],
                   dependencies=[Depends(require_runner_token)])


async def _get_runner(db: AsyncSession, runner_id: int) -> SandboxRunnerNode:
    runner = (await db.execute(
        select(SandboxRunnerNode).where(SandboxRunnerNode.id == runner_id)
    )).scalar_one_or_none()
    if runner is None:
        raise HTTPException(status_code=404, detail="Runner 未注册")
    return runner


@router.post("/runners/register")
async def register_runner(
    payload: Dict[str, Any] = Body(default_factory=dict),
    db: AsyncSession = Depends(get_db),
):
    runner = await runtime.register_runner(db, payload)
    await db.commit()
    return {"runner_id": runner.id, "node_name": runner.node_name, "status": runner.status}


@router.post("/runners/{runner_id}/heartbeat")
async def runner_heartbeat(
    runner_id: int,
    payload: Dict[str, Any] = Body(default_factory=dict),
    db: AsyncSession = Depends(get_db),
):
    runner = await _get_runner(db, runner_id)
    await runtime.runner_heartbeat(db, runner, payload)
    await db.commit()
    return {"status": "ok", "runner_id": runner.id}


@router.post("/runners/{runner_id}/jobs/claim")
async def claim_jobs(
    runner_id: int,
    payload: Dict[str, Any] = Body(default_factory=dict),
    db: AsyncSession = Depends(get_db),
):
    runner = await _get_runner(db, runner_id)
    limit = max(1, min(8, int(payload.get("limit") or 1)))
    claimed = await jobs.claim_jobs(db, runner, limit=limit)
    await db.commit()
    return {
        "jobs": [
            {
                "id": job.id,
                "job_type": job.job_type,
                "workspace_id": job.workspace_id,
                "sandbox_id": job.sandbox_id,
                "attempt": job.attempt,
                "max_attempts": job.max_attempts,
                "lease_token": job.lease_token,
                "payload": job.payload_json or {},
            }
            for job in claimed
        ],
    }


@router.post("/jobs/{job_id}/heartbeat")
async def heartbeat_job(
    job_id: str,
    payload: Dict[str, Any] = Body(default_factory=dict),
    db: AsyncSession = Depends(get_db),
):
    runner = await _get_runner(db, int(payload.get("runner_id") or 0))
    ok = await jobs.heartbeat_job(
        db, job_id, runner, int(payload.get("lease_token") or 0),
    )
    await db.commit()
    if not ok:
        raise HTTPException(status_code=409, detail="Job 租约已失效（fencing）")
    return {"status": "ok"}


def _observe_job(job: SandboxJob) -> None:
    try:
        now = datetime.utcnow()
        if job.started_at and job.created_at:
            metrics.observe_sandbox_queue_wait(
                (job.started_at - job.created_at).total_seconds()
            )
        if job.finished_at and job.started_at and job.job_type in ("provision", "start", "restart"):
            seconds = (job.finished_at - job.started_at).total_seconds()
            if job.job_type == "provision":
                metrics.observe_sandbox_provision(seconds)
            else:
                metrics.observe_sandbox_start(seconds)
        if job.attempt and int(job.attempt) > 1:
            metrics.inc_sandbox_job_retry()
    except Exception:  # noqa: BLE001 指标失败不影响业务
        pass


@router.post("/jobs/{job_id}/complete")
async def complete_job(
    job_id: str,
    payload: Dict[str, Any] = Body(default_factory=dict),
    db: AsyncSession = Depends(get_db),
):
    runner = await _get_runner(db, int(payload.get("runner_id") or 0))
    result = payload.get("result") or {}
    job = await jobs.complete_job(
        db, job_id, runner, int(payload.get("lease_token") or 0), result,
    )
    await runtime.apply_job_complete(db, job, result)
    _observe_job(job)
    await db.commit()
    return {"status": "completed"}


@router.post("/jobs/{job_id}/fail")
async def fail_job(
    job_id: str,
    payload: Dict[str, Any] = Body(default_factory=dict),
    db: AsyncSession = Depends(get_db),
):
    runner = await _get_runner(db, int(payload.get("runner_id") or 0))
    job, retry = await jobs.fail_job(
        db, job_id, runner, int(payload.get("lease_token") or 0),
        str(payload.get("error_code") or "sandbox_job_failed"),
        str(payload.get("error_message") or ""),
    )
    if not retry:
        await runtime.apply_job_failure(
            db, job, str(payload.get("error_code") or "sandbox_job_failed"),
            str(payload.get("error_message") or ""),
        )
    if payload.get("oom"):
        metrics.inc_sandbox_oom()
    await db.commit()
    return {"status": "queued" if retry else "failed", "retry": retry}


@router.post("/runners/{runner_id}/sandboxes/report")
async def report_sandboxes(
    runner_id: int,
    payload: Dict[str, Any] = Body(default_factory=dict),
    db: AsyncSession = Depends(get_db),
):
    runner = await _get_runner(db, runner_id)
    result = await runtime.report_sandbox_states(db, runner, payload.get("items") or [])
    await db.commit()
    return result
