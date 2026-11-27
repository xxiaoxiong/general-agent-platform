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
from app.observability import metrics

from . import agent_client
from .common import (
    SandboxNotReadyError, SandboxOperationError, SandboxQuotaError, emit_event, utcnow,
)

# 状态同步时发布的事件（任务书 §12.3）；starting/stopping 由操作入口发布。
_OBSERVED_EVENT_TYPES = {
    "healthy": "service.healthy",
    "unhealthy": "service.unhealthy",
    "exited": "service.exited",
}


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
    if int(profile.max_ports or 0) > 0 and count >= int(profile.max_ports):
        raise SandboxQuotaError(
            f"项目最大 Preview 端口数为 {profile.max_ports}，请先清理不再使用的服务"
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
        if existing.name != name:
            raise SandboxQuotaError(f"端口 {port} 已被服务 '{existing.name}' 占用")
        if int(existing.port) != int(port):
            raise SandboxQuotaError(
                f"服务 '{name}' 已存在且端口为 {existing.port}，不能直接改端口；"
                "请先停止后由管理员清理旧服务记录"
            )
        if existing.desired_state == "running" and existing.observed_state in (
            "starting", "healthy", "unhealthy",
        ):
            raise SandboxQuotaError(
                f"服务 '{name}' 正在运行；请先 stop_service，再以新命令重新启动")
        # 已停止/失败的同名同端口服务：更新启动命令后重试（例如补上 --host 0.0.0.0）
        existing.command = command
        existing.working_directory = working_directory or "/workspace"
        existing.protocol = protocol or "http"
        existing.health_check_type = health_check_type or "http"
        existing.health_check_path = health_check_path or "/"
        existing.health_check_timeout = int(health_check_timeout)
        existing.auto_restart = bool(auto_restart)
        existing.auto_start_on_sandbox_start = bool(auto_start_on_sandbox_start)
        existing.sandbox_id = sandbox.id
        existing.desired_state = "running"
        existing.observed_state = "starting"
        existing.last_started_at = utcnow()
        service = existing
    else:
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
    metrics.inc_service_restart("manual")
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
                service.observed_state = "exited"
                changed += 1
                await emit_event(
                    db, "service.exited",
                    tenant_id=workspace.tenant_id,
                    project_id=workspace.project_id, workspace_id=workspace.id,
                    sandbox_id=sandbox.id, generation=sandbox.generation,
                    payload={"name": service.name, "port": service.port,
                             "reason": "process_missing"},
                )
            metrics.set_service_health(
                workspace.id, service.name, service.observed_state == "healthy")
            continue
        observed = str(state.get("observed_state") or "")
        if observed and observed != service.observed_state:
            previous = service.observed_state
            service.observed_state = observed
            changed += 1
            event_type = _OBSERVED_EVENT_TYPES.get(observed)
            if event_type:
                await emit_event(
                    db, event_type,
                    tenant_id=workspace.tenant_id,
                    project_id=workspace.project_id, workspace_id=workspace.id,
                    sandbox_id=sandbox.id, generation=sandbox.generation,
                    payload={"name": service.name, "port": service.port,
                             "previous": previous, "observed_state": observed},
                )
        new_pid = str(state.get("process_id") or "")
        if new_pid:
            if service.process_id and new_pid != service.process_id:
                # workspace-agent 自动重启：进程 ID 变化（任务书 §8.5）
                metrics.inc_service_restart("auto")
            service.process_id = new_pid
        if state.get("exit_code") is not None:
            service.exit_code = int(state["exit_code"])
        metrics.set_service_health(
            workspace.id, service.name, service.observed_state == "healthy")
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
