"""用户侧 Project / Workspace API（/api/v2/projects）。"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.core.database import get_db
from app.models.user import User
from app.services.sandbox import acl, preview as preview_service, projects as project_service, runtime, service_manager
from app.services.sandbox.common import ensure_enabled

router = APIRouter(prefix="/projects", tags=["projects-v2"])


class ProjectCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=2000)
    profile: Optional[str] = Field(default=None, max_length=48)


class ProjectPatch(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=120)
    description: Optional[str] = Field(default=None, max_length=2000)


class MemberAdd(BaseModel):
    user_id: int
    role: str = Field(default="operator", pattern="^(viewer|operator|admin)$")


@router.get("")
async def list_projects(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    ensure_enabled()
    return {"items": await project_service.list_projects(db, current_user)}


@router.post("")
async def create_project(
    body: ProjectCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    ensure_enabled()
    from app.services.sandbox.common import ensure_enabled_for

    ensure_enabled_for(current_user.id)
    project, workspace = await project_service.create_project(
        db, current_user, body.name, body.description,
    )
    if body.profile:
        from app.services.sandbox.profiles import get_profile

        profile = await get_profile(db, body.profile)
        workspace.resource_profile_id = profile.id
        workspace.storage_quota_bytes = int(profile.disk_quota_bytes)
    await db.commit()
    return await project_service.project_detail(db, project.id, current_user)


@router.get("/{project_id}")
async def get_project(
    project_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    ensure_enabled()
    detail = await project_service.project_detail(db, project_id, current_user)
    await db.commit()
    return detail


@router.patch("/{project_id}")
async def patch_project(
    project_id: int,
    body: ProjectPatch,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    ensure_enabled()
    project, role = await acl.require_project_access(db, project_id, current_user, "admin")
    if not acl.can_manage(role):
        raise HTTPException(status_code=403, detail="仅项目管理员可修改项目")
    if body.name is not None:
        project.name = body.name.strip()[:120]
    if body.description is not None:
        project.description = body.description
    await db.commit()
    return await project_service.project_detail(db, project.id, current_user)


@router.delete("/{project_id}")
async def delete_project(
    project_id: int,
    purge_volume: bool = Query(False, description="是否同时清理 Workspace Volume（不可恢复）"),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    ensure_enabled()
    await project_service.delete_project(
        db, project_id, current_user, purge_volume=bool(purge_volume),
    )
    await db.commit()
    return {"status": "deleting", "purge_volume": bool(purge_volume)}


@router.get("/{project_id}/workspace")
async def get_project_workspace(
    project_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    ensure_enabled()
    workspace, project, role = await acl.require_workspace_by_project(
        db, project_id, current_user, "viewer",
    )
    sandbox = await runtime.latest_sandbox(db, workspace.id)
    services = await service_manager.list_services(db, workspace)
    if sandbox is not None and sandbox.observed_state == "ready":
        await service_manager.sync_service_states(db, workspace, sandbox)
    grants = await preview_service.list_grants(db, workspace)
    await runtime.touch_workspace(db, workspace.id)
    await db.commit()
    return {
        "project": {
            "id": project.id, "name": project.name, "owner_id": project.owner_id,
            "status": project.status,
        },
        "role": role,
        "workspace": runtime.workspace_to_dict(workspace, sandbox),
        "services": [service_manager.service_to_dict(svc) for svc in services],
        "preview_grants": grants,
    }


class ProvisionRequest(BaseModel):
    profile: Optional[str] = Field(default=None, max_length=48)


@router.post("/{project_id}/workspace/provision")
async def provision_project_workspace(
    project_id: int,
    body: ProvisionRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    ensure_enabled()
    workspace, project, role = await acl.require_workspace_by_project(
        db, project_id, current_user, "operator",
    )
    sandbox = await runtime.provision_workspace(
        db, workspace, profile_name=body.profile, requested_by=current_user.id,
    )
    await db.commit()
    return {
        "workspace": runtime.workspace_to_dict(workspace, sandbox),
        "sandbox": runtime.sandbox_to_dict(sandbox),
    }


@router.get("/{project_id}/members")
async def list_project_members(
    project_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    ensure_enabled()
    return {"items": await project_service.list_members(db, project_id, current_user)}


@router.post("/{project_id}/members")
async def add_project_member(
    project_id: int,
    body: MemberAdd,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    ensure_enabled()
    result = await project_service.add_member(
        db, project_id, current_user, body.user_id, body.role,
    )
    await db.commit()
    return result


@router.delete("/{project_id}/members/{user_id}")
async def remove_project_member(
    project_id: int,
    user_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    ensure_enabled()
    await project_service.remove_member(db, project_id, current_user, user_id)
    await db.commit()
    return {"status": "removed"}
