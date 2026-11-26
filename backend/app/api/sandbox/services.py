"""Workspace Service 与 Preview Grant API（/api/v2/workspaces）。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.core.config import settings
from app.core.database import get_db
from app.models.user import User
from app.services.sandbox import (
    acl, agent_client, preview as preview_service, runtime, service_manager,
)
from app.services.sandbox.common import ensure_enabled

router = APIRouter(prefix="/workspaces", tags=["workspace-services-v2"])


class ServiceCreate(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    command: str = Field(min_length=1, max_length=8000)
    port: int = Field(ge=1, le=65535)
    working_directory: str = Field(default="/workspace", max_length=255)
    protocol: str = Field(default="http", pattern="^(http|https)$")
    health_check_type: str = Field(default="http", pattern="^(http|tcp|none)$")
    health_check_path: str = Field(default="/", max_length=255)
    health_check_timeout: int = Field(default=60, ge=5, le=600)
    auto_restart: bool = True
    auto_start_on_sandbox_start: bool = True


class PreviewGrantCreate(BaseModel):
    ttl_seconds: int = Field(default=0, ge=0, le=86400)


@router.post("/{workspace_id}/services")
async def create_service(
    workspace_id: int,
    body: ServiceCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    ensure_enabled()
    workspace, project, role = await acl.require_workspace_access(
        db, workspace_id, current_user, "operator",
    )
    sandbox = await runtime.require_ready_sandbox(db, workspace)
    service = await service_manager.create_service(
        db, workspace, sandbox, current_user,
        name=body.name.strip(),
        command=body.command,
        port=body.port,
        working_directory=body.working_directory,
        protocol=body.protocol,
        health_check_type=body.health_check_type,
        health_check_path=body.health_check_path,
        health_check_timeout=body.health_check_timeout,
        auto_restart=body.auto_restart,
        auto_start_on_sandbox_start=body.auto_start_on_sandbox_start,
    )
    await db.commit()
    return service_manager.service_to_dict(service)


@router.get("/{workspace_id}/services")
async def list_services(
    workspace_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    ensure_enabled()
    workspace, _project, _role = await acl.require_workspace_access(
        db, workspace_id, current_user, "viewer",
    )
    sandbox = await runtime.latest_sandbox(db, workspace.id)
    if sandbox is not None and sandbox.observed_state == "ready":
        try:
            await service_manager.sync_service_states(db, workspace, sandbox)
        except Exception:  # noqa: BLE001 同步失败不阻断列表
            pass
    services = await service_manager.list_services(db, workspace)
    await db.commit()
    return {"items": [service_manager.service_to_dict(svc) for svc in services]}


@router.get("/{workspace_id}/services/{service_id}")
async def get_service(
    workspace_id: int,
    service_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    ensure_enabled()
    workspace, _project, _role = await acl.require_workspace_access(
        db, workspace_id, current_user, "viewer",
    )
    service = await service_manager.get_service(db, workspace, service_id)
    if service.sandbox_id:
        try:
            sandbox = await runtime.require_ready_sandbox(db, workspace)
            await service_manager.sync_service_states(db, workspace, sandbox)
            await db.refresh(service)
        except Exception:  # noqa: BLE001 Sandbox 未运行时返回数据库状态
            pass
    await db.commit()
    return service_manager.service_to_dict(service)


@router.post("/{workspace_id}/services/{service_id}/stop")
async def stop_service(
    workspace_id: int,
    service_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    ensure_enabled()
    workspace, _project, _role = await acl.require_workspace_access(
        db, workspace_id, current_user, "operator",
    )
    service = await service_manager.get_service(db, workspace, service_id)
    await service_manager.stop_service(db, workspace, service, current_user)
    await db.commit()
    return service_manager.service_to_dict(service)


@router.post("/{workspace_id}/services/{service_id}/restart")
async def restart_service(
    workspace_id: int,
    service_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    ensure_enabled()
    workspace, _project, _role = await acl.require_workspace_access(
        db, workspace_id, current_user, "operator",
    )
    service = await service_manager.get_service(db, workspace, service_id)
    await service_manager.restart_service(db, workspace, service, current_user)
    await db.commit()
    return service_manager.service_to_dict(service)


@router.get("/{workspace_id}/services/{service_id}/logs")
async def service_logs(
    workspace_id: int,
    service_id: int,
    tail: int = Query(200, ge=1, le=2000),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    ensure_enabled()
    workspace, _project, _role = await acl.require_workspace_access(
        db, workspace_id, current_user, "viewer",
    )
    service = await service_manager.get_service(db, workspace, service_id)
    data = await service_manager.fetch_logs(service, tail=tail)
    await runtime.touch_workspace(db, workspace.id)
    await db.commit()
    return data


@router.post("/{workspace_id}/services/{service_id}/preview-grants")
async def create_preview_grant(
    workspace_id: int,
    service_id: int,
    body: PreviewGrantCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    ensure_enabled()
    workspace, _project, _role = await acl.require_workspace_access(
        db, workspace_id, current_user, "operator",
    )
    service = await service_manager.get_service(db, workspace, service_id)
    grant, token = await preview_service.create_grant(
        db, workspace, service, current_user,
        ttl_seconds=(body.ttl_seconds or None),
    )
    access_token = None
    if settings.PREVIEW_REQUIRE_LOGIN:
        from app.services.sandbox.preview import create_preview_access_token

        access_token = create_preview_access_token(
            grant.id, current_user.id,
            int(body.ttl_seconds or settings.PREVIEW_TOKEN_TTL),
        )
    await runtime.touch_workspace(db, workspace.id)
    await db.commit()
    return preview_service.grant_to_dict(grant, token, access_token)


@router.get("/{workspace_id}/preview-grants")
async def list_preview_grants(
    workspace_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    ensure_enabled()
    workspace, _project, _role = await acl.require_workspace_access(
        db, workspace_id, current_user, "viewer",
    )
    return {"items": await preview_service.list_grants(db, workspace)}


@router.delete("/{workspace_id}/preview-grants/{grant_id}")
async def revoke_preview_grant(
    workspace_id: int,
    grant_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    ensure_enabled()
    workspace, _project, _role = await acl.require_workspace_access(
        db, workspace_id, current_user, "operator",
    )
    await preview_service.revoke_grant(db, workspace, grant_id, current_user)
    await db.commit()
    return {"status": "revoked"}
