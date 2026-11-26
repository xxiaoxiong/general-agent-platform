"""Project 管理与成员 ACL。

- Project 1:1 Workspace（懒创建）；
- 项目成员角色 viewer / operator / admin，owner 隐式 admin；
- 删除项目为软删除 + 显式 Volume 清理参数（默认保留 Volume，任务书 §5.1/§18）。
"""
from __future__ import annotations

from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.sandbox import (
    Project, ProjectMember, SandboxInstance, Workspace, WorkspaceService,
)
from app.models.user import User

from . import runtime, scheduler
from .acl import can_manage, require_project_access
from .common import SandboxAccessDenied, emit_event, utcnow


async def create_project(
    db: AsyncSession, user: User, name: str, description: str = "",
    tenant_id: Optional[str] = None,
) -> tuple[Project, Workspace]:
    project = Project(
        name=(name or "未命名项目").strip()[:120],
        description=description or "",
        owner_id=user.id,
        tenant_id=tenant_id,
        status="active",
    )
    db.add(project)
    await db.flush()
    workspace = await runtime.ensure_workspace(db, project.id, user.id, tenant_id)
    await emit_event(
        db, "workspace.created",
        tenant_id=tenant_id, user_id=user.id, project_id=project.id,
        workspace_id=workspace.id,
        payload={"name": project.name, "volume_id": workspace.volume_id},
    )
    return project, workspace


async def default_project(db: AsyncSession, user: User) -> Project:
    """会话未指定项目时的默认归属（每个用户一个「默认项目」）。

    存量会话（project_id 为空）不受影响；仅新会话在 Sandbox 启用时自动挂接，
    保证 Agent 工具与文件面板看到同一个持久化 Workspace。
    """
    project = (await db.execute(
        select(Project).where(
            Project.owner_id == user.id,
            Project.name == "默认项目",
            Project.deleted_at.is_(None),
        ).order_by(Project.id.asc()).limit(1)
    )).scalars().first()
    if project is None:
        project, _workspace = await create_project(
            db, user, "默认项目", "未显式选择项目时创建的默认持久化工作空间",
        )
    else:
        await runtime.ensure_workspace(db, project.id, project.owner_id, project.tenant_id)
    return project


async def list_projects(db: AsyncSession, user: User) -> list[dict]:
    owned = (await db.execute(
        select(Project).where(
            Project.deleted_at.is_(None),
            (Project.owner_id == user.id)
            | Project.id.in_(
                select(ProjectMember.project_id).where(ProjectMember.user_id == user.id)
            ),
        ).order_by(Project.updated_at.desc(), Project.id.desc())
    )).scalars().all()
    result = []
    for project in owned:
        workspace = (await db.execute(
            select(Workspace).where(
                Workspace.project_id == project.id, Workspace.deleted_at.is_(None))
        )).scalar_one_or_none()
        sandbox = await runtime.latest_sandbox(db, workspace.id) if workspace else None
        service_count = 0
        if workspace is not None:
            from sqlalchemy import func

            service_count = (await db.execute(
                select(func.count(WorkspaceService.id)).where(
                    WorkspaceService.workspace_id == workspace.id)
            )).scalar() or 0
        result.append({
            "id": project.id,
            "name": project.name,
            "description": project.description,
            "owner_id": project.owner_id,
            "status": project.status,
            "created_at": project.created_at,
            "updated_at": project.updated_at,
            "workspace": runtime.workspace_to_dict(workspace, sandbox),
            "service_count": service_count,
        })
    return result


async def project_detail(db: AsyncSession, project_id: int, user: User) -> dict:
    project, role = await require_project_access(db, project_id, user)
    workspace = await runtime.ensure_workspace(db, project.id, project.owner_id, project.tenant_id)
    sandbox = await runtime.latest_sandbox(db, workspace.id)
    return {
        "id": project.id,
        "name": project.name,
        "description": project.description,
        "owner_id": project.owner_id,
        "status": project.status,
        "role": role,
        "workspace": runtime.workspace_to_dict(workspace, sandbox),
        "created_at": project.created_at,
        "updated_at": project.updated_at,
    }


async def list_members(db: AsyncSession, project_id: int, user: User) -> list[dict]:
    project, _role = await require_project_access(db, project_id, user)
    rows = (await db.execute(
        select(ProjectMember, User)
        .join(User, User.id == ProjectMember.user_id)
        .where(ProjectMember.project_id == project.id)
    )).all()
    result = [{
        "user_id": project.owner_id,
        "username": "",
        "role": "admin",
        "is_owner": True,
    }]
    owner = (await db.execute(select(User).where(User.id == project.owner_id))).scalar_one_or_none()
    if owner is not None:
        result[0]["username"] = owner.username
    for member, member_user in rows:
        result.append({
            "user_id": member.user_id,
            "username": member_user.username,
            "role": member.role,
            "is_owner": False,
        })
    return result


async def add_member(db: AsyncSession, project_id: int, actor: User,
                     target_user_id: int, role: str) -> dict:
    project, actor_role = await require_project_access(db, project_id, actor, "admin")
    if not can_manage(actor_role):
        raise SandboxAccessDenied("仅项目管理员可管理成员")
    if role not in ("viewer", "operator", "admin"):
        raise SandboxAccessDenied("非法成员角色")
    member = (await db.execute(
        select(ProjectMember).where(
            ProjectMember.project_id == project.id,
            ProjectMember.user_id == target_user_id,
        )
    )).scalar_one_or_none()
    if member is None:
        member = ProjectMember(project_id=project.id, user_id=target_user_id, role=role)
        db.add(member)
    else:
        member.role = role
    await emit_event(
        db, "workspace.acl_changed", user_id=actor.id, project_id=project.id,
        payload={"target_user_id": target_user_id, "role": role, "action": "grant"},
    )
    return {"project_id": project.id, "user_id": target_user_id, "role": role}


async def remove_member(db: AsyncSession, project_id: int, actor: User,
                        target_user_id: int) -> None:
    project, actor_role = await require_project_access(db, project_id, actor, "admin")
    if not can_manage(actor_role):
        raise SandboxAccessDenied("仅项目管理员可管理成员")
    member = (await db.execute(
        select(ProjectMember).where(
            ProjectMember.project_id == project.id,
            ProjectMember.user_id == target_user_id,
        )
    )).scalar_one_or_none()
    if member is not None:
        await db.delete(member)
    # 成员移除后已签发 Grant 立即失效（任务书 §16.3-5 / §11.1）
    from .preview import revoke_grants_for_user

    await revoke_grants_for_user(db, project.id, target_user_id)
    await emit_event(
        db, "workspace.acl_changed", user_id=actor.id, project_id=project.id,
        payload={"target_user_id": target_user_id, "action": "revoke"},
    )


async def delete_project(
    db: AsyncSession, project_id: int, user: User, *, purge_volume: bool = False,
) -> None:
    """软删除项目 + 停止并删除 Sandbox；Volume 默认保留（显式 purge 才清理）。"""
    project, role = await require_project_access(db, project_id, user, "admin")
    if not can_manage(role):
        raise SandboxAccessDenied("仅项目管理员可删除项目")
    workspace = (await db.execute(
        select(Workspace).where(
            Workspace.project_id == project.id, Workspace.deleted_at.is_(None))
    )).scalar_one_or_none()
    if workspace is not None:
        sandbox = await runtime.active_sandbox(db, workspace.id)
        if sandbox is not None and sandbox.desired_state != "deleted":
            sandbox.desired_state = "deleted"
            sandbox.lifecycle_version = int(sandbox.lifecycle_version or 0) + 1
            from . import jobs
            from .profiles import get_profile

            profile = await get_profile(db)
            payload = runtime._job_payload(  # noqa: SLF001 同域复用白名单载荷
                workspace, sandbox, profile,
                await runtime._workspace_services(db, workspace.id),  # noqa: SLF001
                purge_volume=purge_volume,
            )
            await jobs.enqueue_job(
                db, job_type="delete", workspace_id=workspace.id,
                sandbox_id=sandbox.id, runner_id=sandbox.runner_id,
                payload=payload,
                idempotency_key=f"delete:{sandbox.id}:{sandbox.generation}:{sandbox.lifecycle_version}",
                priority=60,
            )
            await emit_event(
                db, "sandbox.delete_requested", user_id=user.id,
                tenant_id=workspace.tenant_id, project_id=project.id,
                workspace_id=workspace.id, sandbox_id=sandbox.id,
                generation=sandbox.generation,
                payload={"purge_volume": purge_volume},
            )
        workspace.status = "deleting"
        workspace.deleted_at = utcnow()
        if purge_volume:
            await scheduler.release_reservations_for_workspace(db, workspace.id)
    project.status = "deleted"
    project.deleted_at = utcnow()
    await emit_event(
        db, "workspace.deleting", user_id=user.id, tenant_id=project.tenant_id,
        project_id=project.id,
        payload={"purge_volume": purge_volume},
    )
