"""Workspace / Sandbox 生命周期编排（控制面）。

职责：
- Project 1:1 Workspace 懒创建（Volume 名由服务端生成）；
- Provision / Start / Stop / Restart / Delete 转为持久化 Job；
- Runner 上报结果的落库（Desired / Observed 分离、资源预留推进/释放）；
- Runner 心跳与真实容器对账、租约回收、空闲回收。

本模块不调用 Docker SDK，不做容器操作；只写数据库并发布事件。
"""
from __future__ import annotations

from datetime import timedelta
from typing import Any, Optional

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.agent import AgentRun, AgentSession
from app.models.sandbox import (
    SandboxCommandOperation, SandboxEvent, SandboxInstance, SandboxJob,
    SandboxResourceProfile, SandboxRunnerNode, Workspace, WorkspaceService,
)

from . import jobs, scheduler
from .common import (
    SandboxNotFoundError, SandboxNotReadyError, SandboxOperationError,
    SandboxQuotaError, SandboxResourceUnavailable, emit_event, emit_event_detached,
    sha256_text, utcnow,
)
from .profiles import get_profile

ACTIVE_OBSERVED_STATES = scheduler.ACTIVE_OBSERVED_STATES
TERMINAL_RUN_STATUSES = (
    "completed", "failed", "cancelled", "limit_exceeded", "needs_reconciliation",
)


# ── Workspace ─────────────────────────────────────────────────────────────

def build_volume_id(project_id: int) -> str:
    """Volume 名由服务端生成（不可由用户指定任意 Docker Volume）。"""
    digest = sha256_text(f"project:{project_id}")[:24]
    return f"{settings.SANDBOX_VOLUME_PREFIX}{digest}"


async def ensure_workspace(db: AsyncSession, project_id: int, owner_user_id: int,
                           tenant_id: Optional[str] = None) -> Workspace:
    """按 Project 懒创建唯一 Workspace（任务书 §1.1）。"""
    workspace = (await db.execute(
        select(Workspace).where(
            Workspace.project_id == project_id, Workspace.deleted_at.is_(None),
        )
    )).scalar_one_or_none()
    if workspace is not None:
        return workspace
    profile = await get_profile(db)
    workspace = Workspace(
        project_id=project_id,
        tenant_id=tenant_id,
        owner_user_id=owner_user_id,
        status="provisioning",
        volume_id=build_volume_id(project_id),
        volume_driver="local",
        storage_quota_bytes=int(profile.disk_quota_bytes),
        resource_profile_id=profile.id,
        version=0,
    )
    db.add(workspace)
    await db.flush()
    await emit_event(
        db, "workspace.provisioning", tenant_id=tenant_id, user_id=owner_user_id,
        project_id=project_id, workspace_id=workspace.id,
        payload={"volume_id": workspace.volume_id},
    )
    return workspace


async def active_sandbox(db: AsyncSession, workspace_id: int) -> Optional[SandboxInstance]:
    return (await db.execute(
        select(SandboxInstance)
        .where(SandboxInstance.workspace_id == workspace_id,
               SandboxInstance.desired_state != "deleted")
        .order_by(SandboxInstance.generation.desc())
        .limit(1)
    )).scalars().first()


async def latest_sandbox(db: AsyncSession, workspace_id: int) -> Optional[SandboxInstance]:
    return (await db.execute(
        select(SandboxInstance)
        .where(SandboxInstance.workspace_id == workspace_id)
        .order_by(SandboxInstance.generation.desc())
        .limit(1)
    )).scalars().first()


async def _next_generation(db: AsyncSession, workspace_id: int) -> int:
    value = (await db.execute(
        select(func.max(SandboxInstance.generation)).where(
            SandboxInstance.workspace_id == workspace_id)
    )).scalar()
    return int(value or 0) + 1


def _job_payload(workspace: Workspace, sandbox: SandboxInstance,
                 profile: SandboxResourceProfile, services: list[WorkspaceService],
                 *, purge_volume: bool = False) -> dict:
    """Job payload 仅白名单字段：镜像/Volume/网络/安全配置由服务端策略生成。"""
    return {
        "workspace_id": workspace.id,
        "project_id": workspace.project_id,
        "sandbox_id": sandbox.id,
        "generation": sandbox.generation,
        "volume_id": workspace.volume_id,
        "image": settings.SANDBOX_IMAGE,
        "network_policy": profile.network_policy or settings.SANDBOX_NETWORK_POLICY,
        "profile": {
            "cpu_request_millis": int(profile.cpu_request_millis),
            "cpu_limit_millis": int(profile.cpu_limit_millis),
            "memory_request_bytes": int(profile.memory_request_bytes),
            "memory_limit_bytes": int(profile.memory_limit_bytes),
            "disk_quota_bytes": int(profile.disk_quota_bytes),
            "pid_limit": int(profile.pid_limit),
        },
        "auto_services": [
            {
                "name": svc.name,
                "command": svc.command,
                "working_directory": svc.working_directory,
                "port": svc.port,
                "protocol": svc.protocol,
                "health_check_type": svc.health_check_type,
                "health_check_path": svc.health_check_path,
                "health_check_timeout": svc.health_check_timeout,
                "auto_restart": bool(svc.auto_restart),
            }
            for svc in services if svc.auto_start_on_sandbox_start and svc.desired_state == "running"
        ],
        "purge_volume": purge_volume,
    }


async def _workspace_services(db: AsyncSession, workspace_id: int) -> list[WorkspaceService]:
    return list((await db.execute(
        select(WorkspaceService).where(WorkspaceService.workspace_id == workspace_id)
    )).scalars().all())


async def _interrupt_operations(db: AsyncSession, workspace_id: int,
                                sandbox_id: Optional[int]) -> int:
    """Generation 变化 / 容器停止或重启：旧命令标记 interrupted，不假设自动继续。"""
    conditions = [
        SandboxCommandOperation.workspace_id == workspace_id,
        SandboxCommandOperation.status == "running",
    ]
    if sandbox_id is not None:
        conditions.append(SandboxCommandOperation.sandbox_id != int(sandbox_id))
    result = await db.execute(
        update(SandboxCommandOperation)
        .where(*conditions)
        .values(status="interrupted", finished_at=utcnow())
    )
    return result.rowcount or 0


async def provision_workspace(
    db: AsyncSession, workspace: Workspace, *, profile_name: Optional[str] = None,
    requested_by: Optional[int] = None,
) -> SandboxInstance:
    """确保 Workspace 有一个正在运行的 Sandbox（幂等）。

    - 已有活动 Sandbox：直接返回；已停止则复用同一 Generation 启动（文件不丢）；
    - 上一代失败/已删除：创建新 Generation（同 Volume）。
    """
    sandbox = await active_sandbox(db, workspace.id)
    if sandbox is not None and sandbox.observed_state in ACTIVE_OBSERVED_STATES:
        if sandbox.desired_state == "running":
            return sandbox
        await request_lifecycle(db, workspace, sandbox, "start", requested_by)
        return sandbox
    if sandbox is not None and sandbox.observed_state in ("stopped",) and sandbox.desired_state != "deleted":
        await request_lifecycle(db, workspace, sandbox, "start", requested_by)
        return sandbox

    try:
        await scheduler.lock_owner_for_quota(db, workspace.owner_user_id)
        await scheduler.check_quota(db, workspace.owner_user_id, workspace.tenant_id)
        profile = await get_profile(db, profile_name)
        if workspace.resource_profile_id != profile.id:
            workspace.resource_profile_id = profile.id
            workspace.storage_quota_bytes = int(profile.disk_quota_bytes)
        runner = await scheduler.select_runner(db, profile)
    except SandboxQuotaError as exc:
        await emit_event_detached(
            "resource.denied", tenant_id=workspace.tenant_id,
            user_id=requested_by or workspace.owner_user_id,
            project_id=workspace.project_id, workspace_id=workspace.id,
            payload={"reason": "quota", "message": str(exc)[:300]},
        )
        raise
    except SandboxResourceUnavailable as exc:
        await emit_event_detached(
            "resource.waiting", tenant_id=workspace.tenant_id,
            user_id=requested_by or workspace.owner_user_id,
            project_id=workspace.project_id, workspace_id=workspace.id,
            payload={"reason": "runner_capacity", "message": str(exc)[:300]},
        )
        raise

    generation = await _next_generation(db, workspace.id)
    sandbox = SandboxInstance(
        workspace_id=workspace.id,
        generation=generation,
        runner_id=runner.id,
        runtime_provider=settings.SANDBOX_PROVIDER,
        desired_state="running",
        observed_state="queued",
        cpu_request_millis=int(profile.cpu_request_millis),
        cpu_limit_millis=int(profile.cpu_limit_millis),
        memory_request_bytes=int(profile.memory_request_bytes),
        memory_limit_bytes=int(profile.memory_limit_bytes),
        disk_quota_bytes=int(profile.disk_quota_bytes),
        pid_limit=int(profile.pid_limit),
    )
    db.add(sandbox)
    await db.flush()
    # 新 Generation 不继承旧容器的命令：旧 Operation 标记 interrupted
    await _interrupt_operations(db, workspace.id, sandbox.id)

    try:
        await scheduler.reserve(db, runner, workspace, profile, sandbox_id=sandbox.id)
    except SandboxResourceUnavailable as exc:
        await emit_event_detached(
            "resource.waiting", tenant_id=workspace.tenant_id,
            user_id=requested_by or workspace.owner_user_id,
            project_id=workspace.project_id, workspace_id=workspace.id,
            sandbox_id=sandbox.id, generation=generation,
            payload={"reason": "reserve_race", "message": str(exc)[:300]},
        )
        raise
    services = await _workspace_services(db, workspace.id)
    job, _created = await jobs.enqueue_job(
        db,
        job_type="provision",
        workspace_id=workspace.id,
        sandbox_id=sandbox.id,
        runner_id=runner.id,
        payload=_job_payload(workspace, sandbox, profile, services),
        idempotency_key=f"provision:{workspace.id}:{generation}",
    )
    workspace.current_sandbox_id = sandbox.id
    workspace.status = "provisioning"
    workspace.last_active_at = utcnow()
    workspace.version = int(workspace.version or 0) + 1
    await emit_event(
        db, "sandbox.queued",
        tenant_id=workspace.tenant_id, user_id=requested_by or workspace.owner_user_id,
        project_id=workspace.project_id, workspace_id=workspace.id,
        sandbox_id=sandbox.id, generation=generation, job_id=job.id,
        payload={"runner_id": runner.id, "profile": profile.name,
                 "cpu_request_millis": profile.cpu_request_millis,
                 "memory_request_bytes": profile.memory_request_bytes},
    )
    return sandbox


async def request_lifecycle(
    db: AsyncSession, workspace: Workspace, sandbox: SandboxInstance, action: str,
    requested_by: Optional[int] = None,
) -> SandboxJob:
    """Start / Stop / Restart / Delete：更新 Desired State 并确保有对应 Job。"""
    if action not in ("start", "stop", "restart", "delete"):
        raise SandboxOperationError(f"不支持的 Sandbox 生命周期操作: {action}")
    if action == "start":
        if sandbox.desired_state == "running" and sandbox.observed_state in ACTIVE_OBSERVED_STATES:
            # 幂等：已在运行/启动中，不重复入队
            existing = (await db.execute(
                select(SandboxJob).where(
                    SandboxJob.idempotency_key
                    == f"start:{sandbox.id}:{sandbox.generation}:{sandbox.lifecycle_version}"
                )
            )).scalar_one_or_none()
            if existing is None:
                raise SandboxNotReadyError("Sandbox 已在运行")
        sandbox.desired_state = "running"
        if sandbox.observed_state in ("stopped", "failed") and sandbox.runner_id:
            # 停止/失败时可能已释放 CPU/RAM，启动前必须重新原子预留（不超卖）
            try:
                await scheduler.reserve_compute_for_sandbox(db, sandbox)
            except SandboxResourceUnavailable as exc:
                await emit_event_detached(
                    "resource.waiting", tenant_id=workspace.tenant_id,
                    user_id=requested_by or workspace.owner_user_id,
                    project_id=workspace.project_id, workspace_id=workspace.id,
                    sandbox_id=sandbox.id, generation=sandbox.generation,
                    payload={"reason": "restart_capacity", "message": str(exc)[:300]},
                )
                raise
    elif action == "stop":
        if sandbox.observed_state in ("stopped", "queued") and sandbox.desired_state == "stopped":
            raise SandboxNotReadyError("Sandbox 已停止或尚未启动")
        sandbox.desired_state = "stopped"
    elif action == "restart":
        sandbox.desired_state = "running"
    else:
        sandbox.desired_state = "deleted"

    sandbox.lifecycle_version = int(sandbox.lifecycle_version or 0) + 1
    if action in ("stop", "restart", "delete"):
        await _interrupt_operations(db, workspace.id, sandbox.id)
    profile = await get_profile(db, None)
    if workspace.resource_profile_id:
        profile = (await db.execute(
            select(SandboxResourceProfile).where(
                SandboxResourceProfile.id == workspace.resource_profile_id)
        )).scalar_one_or_none() or profile
    services = await _workspace_services(db, workspace.id)
    job, _created = await jobs.enqueue_job(
        db,
        job_type=action,
        workspace_id=workspace.id,
        sandbox_id=sandbox.id,
        runner_id=sandbox.runner_id,
        payload=_job_payload(workspace, sandbox, profile, services,
                             purge_volume=False),
        idempotency_key=f"{action}:{sandbox.id}:{sandbox.generation}:{sandbox.lifecycle_version}",
    )
    workspace.last_active_at = utcnow()
    workspace.version = int(workspace.version or 0) + 1
    await emit_event(
        db, f"sandbox.{action}_requested",
        tenant_id=workspace.tenant_id, user_id=requested_by or workspace.owner_user_id,
        project_id=workspace.project_id, workspace_id=workspace.id,
        sandbox_id=sandbox.id, generation=sandbox.generation, job_id=job.id,
        payload={"desired_state": sandbox.desired_state},
    )
    return job


# ── Runner 注册 / 心跳 / 上报 ──────────────────────────────────────────────

async def register_runner(db: AsyncSession, payload: dict) -> SandboxRunnerNode:
    node_name = str(payload.get("node_name") or settings.SANDBOX_RUNNER_ID or "runner-1")
    runner = (await db.execute(
        select(SandboxRunnerNode).where(SandboxRunnerNode.node_name == node_name)
    )).scalar_one_or_none()
    if runner is None:
        runner = SandboxRunnerNode(node_name=node_name, status="online")
        db.add(runner)
        await db.flush()
    runner.status = "online"
    runner.runtime_provider = str(payload.get("runtime_provider") or "docker")
    runner.runtime_version = str(payload.get("runtime_version") or "")[:64]
    runner.cpu_total_millis = int(payload.get("cpu_total_millis") or 0)
    runner.cpu_allocatable_millis = int(payload.get("cpu_allocatable_millis") or 0)
    runner.memory_total_bytes = int(payload.get("memory_total_bytes") or 0)
    runner.memory_allocatable_bytes = int(payload.get("memory_allocatable_bytes") or 0)
    runner.disk_total_bytes = int(payload.get("disk_total_bytes") or 0)
    runner.disk_allocatable_bytes = int(payload.get("disk_allocatable_bytes") or 0)
    runner.capabilities_json = payload.get("capabilities") or {}
    runner.last_heartbeat_at = utcnow()
    runner.version = int(runner.version or 0) + 1
    await scheduler.recompute_runner_reserved(db, runner.id)
    await emit_event(
        db, "sandbox.runner_registered",
        payload={"node_name": runner.node_name, "runtime": runner.runtime_provider,
                 "cpu_allocatable_millis": runner.cpu_allocatable_millis,
                 "memory_allocatable_bytes": runner.memory_allocatable_bytes},
    )
    return runner


async def runner_heartbeat(db: AsyncSession, runner: SandboxRunnerNode, payload: dict) -> None:
    runner.status = str(payload.get("status") or "online")
    if payload.get("runtime_version"):
        runner.runtime_version = str(payload["runtime_version"])[:64]
    for key in ("cpu_total_millis", "cpu_allocatable_millis",
                "memory_total_bytes", "memory_allocatable_bytes",
                "disk_total_bytes", "disk_allocatable_bytes",
                "active_sandbox_count"):
        if payload.get(key) is not None:
            setattr(runner, key, int(payload[key]))
    if payload.get("capabilities") is not None:
        runner.capabilities_json = payload["capabilities"]
    runner.last_heartbeat_at = utcnow()


async def report_sandbox_states(
    db: AsyncSession, runner: SandboxRunnerNode, items: list[dict],
) -> dict:
    """对账上报：更新真实 Observed State；未知容器只隔离告警，不自动接管。"""
    updated = 0
    unknown = 0
    oom = 0
    for item in items or []:
        sandbox_id = item.get("sandbox_id")
        if not sandbox_id:
            unknown += 1
            await emit_event(
                db, "sandbox.unknown_container", job_id=None,
                payload={"container_id": item.get("container_id"),
                         "container_name": item.get("container_name"),
                         "reason": "missing_platform_labels"},
            )
            continue
        sandbox = (await db.execute(
            select(SandboxInstance).where(SandboxInstance.id == int(sandbox_id))
        )).scalar_one_or_none()
        if sandbox is None or int(item.get("workspace_id") or 0) != int(sandbox.workspace_id):
            unknown += 1
            await emit_event(
                db, "sandbox.unknown_container",
                workspace_id=item.get("workspace_id"),
                sandbox_id=int(sandbox_id),
                payload={"container_id": item.get("container_id"),
                         "reason": "sandbox_not_registered_or_workspace_mismatch"},
            )
            continue
        sandbox.container_id = item.get("container_id") or sandbox.container_id
        sandbox.container_name = item.get("container_name") or sandbox.container_name
        sandbox.container_ip = item.get("container_ip") or sandbox.container_ip
        sandbox.image_digest = item.get("image_digest") or sandbox.image_digest
        sandbox.last_observed_at = utcnow()
        observed = str(item.get("observed_state") or "")
        if observed and observed in (
            "queued", "provisioning", "starting", "ready", "stopping",
            "stopped", "failed", "recovering",
        ):
            sandbox.observed_state = observed
        if observed == "failed" and item.get("oom_killed") and sandbox.failure_code != "oom":
            sandbox.failure_code = "oom"
            sandbox.failure_message = "容器因内存超限被 OOM Kill"
            oom += 1
            await emit_event(
                db, "sandbox.oom", workspace_id=sandbox.workspace_id,
                sandbox_id=sandbox.id, generation=sandbox.generation,
                payload={"container_id": sandbox.container_id},
            )
        updated += 1
    return {"updated": updated, "unknown": unknown, "oom": oom}


async def check_volume_quotas(db: AsyncSession, usages: list[dict]) -> dict:
    """软磁盘配额预警 / 停止策略（任务书 §6.5）。

    - Runner 上报单个 Sandbox 的 Volume 实际占用；
    - 达到 ``SANDBOX_VOLUME_QUOTA_WARN_PERCENT`` 发布一次
      ``sandbox.volume_quota_warning``；
    - 软配额模式下达到配额上限发布 ``sandbox.volume_quota_exceeded`` 并停止
      Sandbox（只释放计算资源，Volume 保留），避免继续写入拖垮宿主机；
    - 硬配额模式由宿主文件系统负责，这里只预警。
    """
    summary = {"warned": 0, "stopped": 0}
    warn_percent = max(1, min(99, int(settings.SANDBOX_VOLUME_QUOTA_WARN_PERCENT or 85)))
    for item in usages or []:
        try:
            sandbox_id = int(item.get("sandbox_id") or 0)
            used = int(item.get("volume_bytes") or 0)
        except (TypeError, ValueError):
            continue
        if sandbox_id <= 0 or used <= 0:
            continue
        sandbox = await db.get(SandboxInstance, sandbox_id)
        if sandbox is None or sandbox.desired_state == "deleted":
            continue
        quota = int(sandbox.disk_quota_bytes or 0)
        if quota <= 0 or used * 100 < quota * warn_percent:
            continue

        async def _event_seen(event_type: str) -> bool:
            row = (await db.execute(
                select(SandboxEvent.id).where(
                    SandboxEvent.sandbox_id == sandbox.id,
                    SandboxEvent.generation == sandbox.generation,
                    SandboxEvent.event_type == event_type,
                ).limit(1)
            )).first()
            return row is not None

        workspace = await db.get(Workspace, sandbox.workspace_id)
        if workspace is None or workspace.deleted_at is not None:
            continue
        if not await _event_seen("sandbox.volume_quota_warning"):
            await emit_event(
                db, "sandbox.volume_quota_warning",
                tenant_id=workspace.tenant_id, project_id=workspace.project_id,
                workspace_id=workspace.id, sandbox_id=sandbox.id,
                generation=sandbox.generation,
                payload={"used_bytes": used, "quota_bytes": quota,
                         "warn_percent": warn_percent},
            )
            summary["warned"] += 1
        if used < quota or settings.SANDBOX_VOLUME_QUOTA_MODE != "soft":
            continue
        if sandbox.observed_state != "ready" or await _event_seen(
            "sandbox.volume_quota_exceeded"
        ):
            continue
        await emit_event(
            db, "sandbox.volume_quota_exceeded",
            tenant_id=workspace.tenant_id, project_id=workspace.project_id,
            workspace_id=workspace.id, sandbox_id=sandbox.id,
            generation=sandbox.generation,
            payload={"used_bytes": used, "quota_bytes": quota,
                     "action": "stop_sandbox"},
        )
        try:
            await request_lifecycle(
                db, workspace, sandbox, "stop", workspace.owner_user_id,
            )
            summary["stopped"] += 1
        except Exception as exc:  # noqa: BLE001 停止失败不影响上报主流程
            await emit_event(
                db, "sandbox.volume_quota_stop_failed",
                tenant_id=workspace.tenant_id, project_id=workspace.project_id,
                workspace_id=workspace.id, sandbox_id=sandbox.id,
                generation=sandbox.generation,
                payload={"error": str(exc)[:300]},
            )
    return summary


# ── Job 结果落库 ───────────────────────────────────────────────────────────

async def apply_job_complete(
    db: AsyncSession, job: SandboxJob, result: dict
) -> None:
    sandbox = (await db.execute(
        select(SandboxInstance).where(SandboxInstance.id == job.sandbox_id)
    )).scalar_one_or_none() if job.sandbox_id else None
    workspace = (await db.execute(
        select(Workspace).where(Workspace.id == job.workspace_id)
    )).scalar_one_or_none()
    if sandbox is None or workspace is None:
        return
    now = utcnow()
    sandbox.last_observed_at = now
    if job.job_type in ("provision", "start", "restart"):
        sandbox.container_id = result.get("container_id") or sandbox.container_id
        sandbox.container_name = result.get("container_name") or sandbox.container_name
        sandbox.container_ip = result.get("container_ip") or sandbox.container_ip
        sandbox.image_digest = result.get("image_digest") or sandbox.image_digest
        sandbox.observed_state = result.get("observed_state") or "ready"
        sandbox.failure_code = None
        sandbox.failure_message = None
        sandbox.desired_state = "running"
        workspace.current_sandbox_id = sandbox.id
        workspace.status = "ready"
        workspace.last_active_at = now
        await scheduler.mark_active(db, sandbox.id)
        for svc_state in (result.get("services") or []):
            service = (await db.execute(
                select(WorkspaceService).where(
                    WorkspaceService.workspace_id == workspace.id,
                    WorkspaceService.name == svc_state.get("name"),
                )
            )).scalar_one_or_none()
            if service is not None:
                service.sandbox_id = sandbox.id
                if svc_state.get("observed_state"):
                    service.observed_state = svc_state["observed_state"]
                if svc_state.get("process_id"):
                    service.process_id = str(svc_state["process_id"])
        await emit_event(
            db, "sandbox.ready" if sandbox.observed_state == "ready" else "sandbox.starting",
            tenant_id=workspace.tenant_id, user_id=workspace.owner_user_id,
            project_id=workspace.project_id, workspace_id=workspace.id,
            sandbox_id=sandbox.id, generation=sandbox.generation, job_id=job.id,
            payload={"container_name": sandbox.container_name,
                     "observed_state": sandbox.observed_state},
        )
    elif job.job_type == "stop":
        sandbox.observed_state = "stopped"
        sandbox.desired_state = "stopped"
        await scheduler.release_compute(db, sandbox.id)
        await _interrupt_operations(db, workspace.id, sandbox.id)
        services = await _workspace_services(db, workspace.id)
        for service in services:
            if service.observed_state not in ("stopped", "exited"):
                service.observed_state = "stopped"
        await emit_event(
            db, "sandbox.stopped",
            tenant_id=workspace.tenant_id, user_id=workspace.owner_user_id,
            project_id=workspace.project_id, workspace_id=workspace.id,
            sandbox_id=sandbox.id, generation=sandbox.generation, job_id=job.id,
        )
    elif job.job_type == "delete":
        sandbox.observed_state = "stopped"
        sandbox.desired_state = "deleted"
        await scheduler.release_all(db, sandbox.id)
        await _interrupt_operations(db, workspace.id, sandbox.id)
        services = await _workspace_services(db, workspace.id)
        for service in services:
            service.observed_state = "stopped"
            service.desired_state = "stopped"
        if workspace.current_sandbox_id == sandbox.id:
            workspace.current_sandbox_id = None
        await emit_event(
            db, "sandbox.deleted",
            tenant_id=workspace.tenant_id, user_id=workspace.owner_user_id,
            project_id=workspace.project_id, workspace_id=workspace.id,
            sandbox_id=sandbox.id, generation=sandbox.generation, job_id=job.id,
            payload={"volume_retained": not bool(job.payload_json.get("purge_volume"))},
        )
    elif job.job_type == "restart":
        await _interrupt_operations(db, workspace.id, sandbox.id)
    elif job.job_type == "reconcile":
        if result.get("observed_state"):
            sandbox.observed_state = result["observed_state"]
        if result.get("observed_state") == "failed":
            sandbox.failure_code = str(result.get("reason") or "container_missing")[:64]
            sandbox.failure_message = "Sandbox 容器不存在或不可用，等待自动恢复"
            workspace.status = "degraded"
            await scheduler.release_compute(db, sandbox.id)
            await emit_event(
                db, "sandbox.reconcile_failed",
                tenant_id=workspace.tenant_id, user_id=workspace.owner_user_id,
                project_id=workspace.project_id, workspace_id=workspace.id,
                sandbox_id=sandbox.id, generation=sandbox.generation, job_id=job.id,
                payload={"reason": sandbox.failure_code},
            )
        elif result.get("observed_state") == "ready":
            sandbox.failure_code = None
            sandbox.failure_message = None
            workspace.status = "ready"


async def apply_job_failure(
    db: AsyncSession, job: SandboxJob, code: str, message: str,
) -> None:
    """Job 终态失败（重试耗尽）后同步 Sandbox / Workspace 状态。"""
    if job.job_type not in ("provision", "start", "restart"):
        if job.job_type == "delete" and job.sandbox_id:
            await scheduler.release_all(db, job.sandbox_id)
        return
    sandbox = (await db.execute(
        select(SandboxInstance).where(SandboxInstance.id == job.sandbox_id)
    )).scalar_one_or_none() if job.sandbox_id else None
    if sandbox is None:
        return
    sandbox.observed_state = "failed"
    sandbox.failure_code = (code or "sandbox_job_failed")[:64]
    sandbox.failure_message = (message or "")[:4000]
    workspace = (await db.execute(
        select(Workspace).where(Workspace.id == sandbox.workspace_id)
    )).scalar_one_or_none()
    if workspace is not None:
        workspace.status = "degraded"
        await emit_event(
            db, "sandbox.failed",
            tenant_id=workspace.tenant_id, user_id=workspace.owner_user_id,
            project_id=workspace.project_id, workspace_id=workspace.id,
            sandbox_id=sandbox.id, generation=sandbox.generation, job_id=job.id,
            payload={"error_code": sandbox.failure_code,
                     "error": sandbox.failure_message[:500]},
        )
    await scheduler.release_compute(db, sandbox.id)


# ── 后台对账：离线 Runner / 租约回收 / 空闲回收 ───────────────────────────

async def mark_stale_runners_offline(db: AsyncSession) -> int:
    threshold = utcnow() - timedelta(
        seconds=max(30.0, float(settings.SANDBOX_RUNNER_HEARTBEAT_INTERVAL) * 4)
    )
    runners = (await db.execute(
        select(SandboxRunnerNode).where(
            SandboxRunnerNode.status == "online",
            (SandboxRunnerNode.last_heartbeat_at.is_(None))
            | (SandboxRunnerNode.last_heartbeat_at < threshold),
        )
    )).scalars().all()
    for runner in runners:
        runner.status = "offline"
        await emit_event(
            db, "sandbox.runner_offline",
            payload={"node_name": runner.node_name,
                     "last_heartbeat_at": runner.last_heartbeat_at},
        )
    return len(runners)


async def _workspace_has_active_run(db: AsyncSession, project_id: int) -> bool:
    count = (await db.execute(
        select(func.count(AgentRun.id))
        .join(AgentSession, AgentSession.id == AgentRun.session_id)
        .where(
            AgentSession.project_id == project_id,
            AgentRun.status.notin_(TERMINAL_RUN_STATUSES),
        )
    )).scalar() or 0
    return count > 0


async def idle_reclaim(db: AsyncSession) -> int:
    """空闲 Sandbox 自动停止（只释放计算资源，保留 Volume；任务书 §8.6）。"""
    if int(settings.SANDBOX_IDLE_TIMEOUT or 0) <= 0:
        return 0
    now = utcnow()
    sandboxes = (await db.execute(
        select(SandboxInstance, Workspace)
        .join(Workspace, Workspace.id == SandboxInstance.workspace_id)
        .where(
            SandboxInstance.observed_state == "ready",
            SandboxInstance.desired_state == "running",
            Workspace.deleted_at.is_(None),
        )
    )).all()
    stopped = 0
    for sandbox, workspace in sandboxes:
        profile = None
        if workspace.resource_profile_id:
            profile = (await db.execute(
                select(SandboxResourceProfile).where(
                    SandboxResourceProfile.id == workspace.resource_profile_id)
            )).scalar_one_or_none()
        timeout = int(profile.idle_timeout_seconds if profile else settings.SANDBOX_IDLE_TIMEOUT)
        if timeout <= 0:
            timeout = int(settings.SANDBOX_IDLE_TIMEOUT)
        last_active = workspace.last_active_at or sandbox.last_observed_at or sandbox.created_at
        if last_active is None or (now - last_active).total_seconds() < timeout:
            continue
        if await _workspace_has_active_run(db, workspace.project_id):
            continue
        sandbox.desired_state = "stopped"
        sandbox.lifecycle_version = int(sandbox.lifecycle_version or 0) + 1
        profile_obj = profile or await get_profile(db)
        services = await _workspace_services(db, workspace.id)
        job, _ = await jobs.enqueue_job(
            db,
            job_type="stop",
            workspace_id=workspace.id,
            sandbox_id=sandbox.id,
            runner_id=sandbox.runner_id,
            payload=_job_payload(workspace, sandbox, profile_obj, services),
            idempotency_key=(
                f"idle-stop:{sandbox.id}:{sandbox.generation}:{sandbox.lifecycle_version}"
            ),
            priority=120,
        )
        await emit_event(
            db, "sandbox.idle_stop_queued",
            tenant_id=workspace.tenant_id, user_id=workspace.owner_user_id,
            project_id=workspace.project_id, workspace_id=workspace.id,
            sandbox_id=sandbox.id, generation=sandbox.generation, job_id=job.id,
            payload={"idle_timeout_seconds": timeout,
                     "last_active_at": last_active},
        )
        stopped += 1
    return stopped


async def touch_workspace(db: AsyncSession, workspace_id: int) -> None:
    workspace = (await db.execute(
        select(Workspace).where(Workspace.id == workspace_id)
    )).scalar_one_or_none()
    if workspace is not None:
        workspace.last_active_at = utcnow()


async def require_ready_sandbox(
    db: AsyncSession, workspace: Workspace,
) -> SandboxInstance:
    """取当前可执行 Sandbox；未启动时按策略自动启动并返回明确错误。"""
    sandbox = await active_sandbox(db, workspace.id)
    if sandbox is None or sandbox.desired_state == "deleted":
        raise SandboxNotReadyError(
            "项目 Sandbox 尚未创建，请先在项目工作空间点击「启动环境」"
        )
    if sandbox.observed_state != "ready":
        if sandbox.desired_state == "running" and sandbox.observed_state in (
            "queued", "provisioning", "starting", "recovering",
        ):
            raise SandboxNotReadyError(
                f"Sandbox 正在启动（当前状态 {sandbox.observed_state}），请稍后重试"
            )
        raise SandboxNotReadyError(
            f"Sandbox 当前不可用（状态 {sandbox.observed_state}），请先启动环境"
        )
    return sandbox


def sandbox_to_dict(sandbox: Optional[SandboxInstance]) -> Optional[dict]:
    if sandbox is None:
        return None
    return {
        "id": sandbox.id,
        "generation": sandbox.generation,
        "desired_state": sandbox.desired_state,
        "observed_state": sandbox.observed_state,
        "failure_code": sandbox.failure_code,
        "failure_message": sandbox.failure_message,
        "cpu_request_millis": sandbox.cpu_request_millis,
        "cpu_limit_millis": sandbox.cpu_limit_millis,
        "memory_request_bytes": sandbox.memory_request_bytes,
        "memory_limit_bytes": sandbox.memory_limit_bytes,
        "disk_quota_bytes": sandbox.disk_quota_bytes,
        "pid_limit": sandbox.pid_limit,
        "created_at": sandbox.created_at,
        "last_observed_at": sandbox.last_observed_at,
    }


def workspace_to_dict(workspace: Optional[Workspace], sandbox: Optional[SandboxInstance]) -> Optional[dict]:
    if workspace is None:
        return None
    return {
        "id": workspace.id,
        "project_id": workspace.project_id,
        "status": workspace.status,
        "volume_driver": workspace.volume_driver,
        "storage_quota_bytes": workspace.storage_quota_bytes,
        "resource_profile_id": workspace.resource_profile_id,
        "last_active_at": workspace.last_active_at,
        "sandbox": sandbox_to_dict(sandbox),
        "created_at": workspace.created_at,
    }


def workspace_not_found() -> SandboxNotFoundError:
    return SandboxNotFoundError("工作空间不存在")


def job_belongs_to_workspace(job: SandboxJob, workspace_id: int) -> bool:
    return int(job.workspace_id) == int(workspace_id)


def sandbox_labels(workspace_id: int, sandbox_id: int, generation: int) -> dict:
    """容器平台标签（Runner 创建容器时使用；此处仅作为唯一契约定义）。"""
    return {
        "platform.managed": "true",
        "platform.workspace_id": str(workspace_id),
        "platform.sandbox_id": str(sandbox_id),
        "platform.generation": str(generation),
    }


def agent_token(sandbox_id: int, generation: int) -> str:
    """Sandbox 内 workspace-agent 的派生令牌（Runner 创建容器与 Gateway 共用）。"""
    return sha256_text(
        f"{settings.SANDBOX_RUNNER_TOKEN}:agent:{sandbox_id}:{generation}"
    )[:40]


def parse_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
