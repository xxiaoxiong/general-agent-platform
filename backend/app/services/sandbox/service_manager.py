"""Workspace Service 注册表（控制面，任务书 §10.1）。

- Desired State 持久化在数据库；调用 workspace-agent 启停并回写 Observed State；
- 同一 Workspace 名称唯一、端口唯一；
- 服务日志经 Runner Gateway 读取并脱敏，不向普通用户暴露凭据；
- Agent Run 结束不自动停止 Service。
"""
from __future__ import annotations

from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.sandbox import (
    SandboxInstance, SandboxResourceProfile, Workspace, WorkspaceService,
)
from app.models.user import User

from . import agent_client
from .common import (
    SandboxNotReadyError, SandboxOperationError, SandboxQuotaError, emit_event, utcnow,
)


async def _profile(db: AsyncSession, workspace: Workspace) -> Optional[SandboxResourceProfile]:
    if not workspace.resource_profile_id:
        return None
    return (await db.execute(
        select(SandboxResourceProfile).where(
            SandboxResourceProfile.id == workspace.resource_profile_id)
    )).scalar_one_or_none()


async def _check_limits(db: AsyncSession, workspace: Workspace) -> None:
    profile = await _profile(db, workspace)
    if profile is None:
        return
    count = (await db.execute(
        select(func.count(WorkspaceService.id)).where(
            WorkspaceService.workspace_id == workspace.id)
    )).scalar() or 0
    if int(profile.max_services or 0) > 0 and count >= int(profile.max_services):
        raise SandboxQuotaError(
            f"项目最大 Service 数为 {profile.max_services}，请先停止/删除不再使用的服务"
        )


async def list_services(db: AsyncSession, workspace: Workspace) -> list[WorkspaceService]:
    return list((await db.execute(
        select(WorkspaceService)
        .where(WorkspaceService.workspace_id == workspace.id)
        .order_by(WorkspaceService.id.asc())
    )).scalars().all())


async def get_service(db: AsyncSession, workspace: Workspace,
                      service_id: int) -> WorkspaceService:
    service = (await db.execute(
        select(WorkspaceService).where(
            WorkspaceService.id == service_id,
            WorkspaceService.workspace_id == workspace.id,
        )
    )).scalar_one_or_none()
    if service is None:
        raise SandboxNotReadyError("服务不存在")
    return service


async def create_service(
    db: AsyncSession,
    workspace: Workspace,
    sandbox: SandboxInstance,
    user: User,
    *,
    name: str,
    command: str,
    port: int,
    working_directory: str = "/workspace",
    protocol: str = "http",
    health_check_type: str = "http",
    health_check_path: str = "/",
    health_check_timeout: int = 60,
    auto_restart: bool = True,
    auto_start_on_sandbox_start: bool = True,
) -> WorkspaceService:
    await _check_limits(db, workspace)
    existing = (await db.execute(
        select(WorkspaceService).where(
            WorkspaceService.workspace_id == workspace.id,
            (WorkspaceService.name == name) | (WorkspaceService.port == int(port)),
        )
    )).scalar_one_or_none()
    if existing is not None:
        if existing.name == name:
            raise SandboxQuotaError(f"服务名 '{name}' 已被占用（同一项目内名称唯一）")
        raise SandboxQuotaError(f"端口 {port} 已被服务 '{existing.name}' 占用")
    service = WorkspaceService(
        workspace_id=workspace.id,
        sandbox_id=sandbox.id,
        name=name,
        command=command,
        working_directory=working_directory or "/workspace",
        port=int(port),
        protocol=protocol or "http",
        health_check_type=health_check_type or "http",
        health_check_path=health_check_path or "/",
        health_check_timeout=int(health_check_timeout),
        desired_state="running",
        observed_state="starting",
        auto_restart=bool(auto_restart),
        auto_start_on_sandbox_start=bool(auto_start_on_sandbox_start),
        last_started_at=utcnow(),
    )
    try:
        async with db.begin_nested():
            db.add(service)
            await db.flush()
    except IntegrityError as exc:
        raise SandboxQuotaError("服务名或端口冲突，请更换后重试") from exc

    await emit_event(
        db, "service.starting", user_id=user.id, tenant_id=workspace.tenant_id,
        project_id=workspace.project_id, workspace_id=workspace.id,
        sandbox_id=sandbox.id, generation=sandbox.generation,
        payload={"name": name, "port": port},
    )
    try:
        result = await agent_client.start_service(sandbox.id, {
            "name": name,
            "command": command,
            "working_directory": working_directory or "/workspace",
            "port": int(port),
            "protocol": protocol or "http",
            "health_check_type": health_check_type or "http",
            "health_check_path": health_check_path or "/",
            "health_check_timeout": int(health_check_timeout),
            "auto_restart": bool(auto_restart),
        })
        service.observed_state = str(result.get("observed_state") or "starting")
        service.process_id = str(result.get("process_id") or "") or None
    except SandboxOperationError as exc:
        service.observed_state = "failed"
        await emit_event(
            db, "service.failed", user_id=user.id, tenant_id=workspace.tenant_id,
            project_id=workspace.project_id, workspace_id=workspace.id,
            sandbox_id=sandbox.id, payload={"name": name, "error": str(exc)[:500]},
        )
        raise
    workspace.last_active_at = utcnow()
    return service


async def stop_service(
    db: AsyncSession, workspace: Workspace, service: WorkspaceService, user: User,
) -> WorkspaceService:
    service.desired_state = "stopped"
    try:
        await agent_client.stop_service(service.sandbox_id or 0, service.name)
    except SandboxOperationError:
        # Sandbox 已停止/不可达：Desired State 仍然生效，Observed 标记停止
        pass
    service.observed_state = "stopped"
    service.last_exited_at = utcnow()
    await emit_event(
        db, "service.stopped", user_id=user.id, tenant_id=workspace.tenant_id,
        project_id=workspace.project_id, workspace_id=workspace.id,
        sandbox_id=service.sandbox_id,
        payload={"name": service.name, "port": service.port},
    )
    return service


async def restart_service(
    db: AsyncSession, workspace: Workspace, service: WorkspaceService, user: User,
) -> WorkspaceService:
    service.desired_state = "running"
    service.observed_state = "starting"
    service.last_started_at = utcnow()
    result = await agent_client.restart_service(service.sandbox_id or 0, service.name)
    service.observed_state = str(result.get("observed_state") or "starting")
    await emit_event(
        db, "service.starting", user_id=user.id, tenant_id=workspace.tenant_id,
        project_id=workspace.project_id, workspace_id=workspace.id,
        sandbox_id=service.sandbox_id,
        payload={"name": service.name, "port": service.port, "restart": True},
    )
    return service


async def fetch_logs(service: WorkspaceService, *, tail: int = 200) -> dict:
    if not service.sandbox_id:
        raise SandboxNotReadyError("服务尚未绑定 Sandbox")
    return await agent_client.service_logs(service.sandbox_id, service.name, tail=tail)


async def sync_service_states(
    db: AsyncSession, workspace: Workspace, sandbox: SandboxInstance,
) -> int:
    """从 workspace-agent 拉取真实状态并回写（Reconciler / 详情查询共用）。"""
    if sandbox.observed_state != "ready":
        return 0
    try:
        payload = await agent_client.list_services(sandbox.id)
    except SandboxOperationError:
        return 0
    items = {str(item.get("name")): item for item in (payload.get("services") or [])}
    services = await list_services(db, workspace)
    changed = 0
    for service in services:
        state = items.get(service.name)
        if state is None:
            if service.desired_state == "running" and service.observed_state in (
                "healthy", "starting", "unhealthy",
            ):
                service.observed_state = "stopped"
                changed += 1
            continue
        observed = str(state.get("observed_state") or "")
        if observed and observed != service.observed_state:
            service.observed_state = observed
            changed += 1
        if state.get("process_id"):
            service.process_id = str(state["process_id"])
        if state.get("exit_code") is not None:
            service.exit_code = int(state["exit_code"])
    return changed


def service_to_dict(service: WorkspaceService, *, include_command: bool = True) -> dict:
    data = {
        "id": service.id,
        "workspace_id": service.workspace_id,
        "sandbox_id": service.sandbox_id,
        "name": service.name,
        "port": service.port,
        "protocol": service.protocol,
        "working_directory": service.working_directory,
        "health_check_type": service.health_check_type,
        "health_check_path": service.health_check_path,
        "health_check_timeout": service.health_check_timeout,
        "desired_state": service.desired_state,
        "observed_state": service.observed_state,
        "auto_restart": bool(service.auto_restart),
        "auto_start_on_sandbox_start": bool(service.auto_start_on_sandbox_start),
        "process_id": service.process_id,
        "exit_code": service.exit_code,
        "last_started_at": service.last_started_at,
        "last_exited_at": service.last_exited_at,
        "created_at": service.created_at,
        "updated_at": service.updated_at,
    }
    if include_command:
        data["command"] = service.command
    return data


def preview_enabled() -> bool:
    return bool(settings.SANDBOX_ENABLED)
