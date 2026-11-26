"""Project / Workspace ACL（任务书 §11.1）。

Project owner 隐式 admin；ProjectMember 提供 viewer / operator / admin 三级。
权限语义：
- viewer：查看 Workspace / Sandbox / Service / 预览（只读）；
- operator：执行命令、读写文件、启停 Service、签发 Preview；
- admin：管理项目成员、删除项目、删除 Volume。
平台 superuser 视为 admin（运维需要），但不得绕过 Project 归属读取其他用户文件以外的
资源管理操作——这里统一走同一入口，避免新增越权面。
"""
from __future__ import annotations

from typing import Optional, Tuple

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.sandbox import Project, ProjectMember, Workspace
from app.models.user import User

from .common import SandboxAccessDenied, SandboxNotFoundError

_ROLE_RANK = {"viewer": 1, "operator": 2, "admin": 3}


def _satisfies(role: Optional[str], min_role: str) -> bool:
    return bool(role) and _ROLE_RANK.get(role, 0) >= _ROLE_RANK.get(min_role, 1)


def role_satisfies(role: Optional[str], min_role: str = "viewer") -> bool:
    """公开的角色判定（Agent 工具 / 文件后端的每轮 ACL 复核使用）。"""
    return _satisfies(role, min_role)


async def require_project_role(
    db: AsyncSession, project_id: int, user_id: int, min_role: str = "viewer",
) -> Project:
    """按 user_id 复核项目角色（工具/文件后端每轮调用，成员移除立即失效）。"""
    project = (await db.execute(
        select(Project).where(Project.id == project_id, Project.deleted_at.is_(None))
    )).scalar_one_or_none()
    if project is None:
        raise SandboxNotFoundError("项目不存在")
    user = (await db.execute(select(User).where(User.id == int(user_id)))).scalar_one_or_none()
    if user is None:
        raise SandboxAccessDenied("用户不存在")
    role = await project_role(db, project, user)
    if not _satisfies(role, min_role):
        raise SandboxAccessDenied(f"当前项目角色 {role or '无'} 无 {min_role} 权限")
    return project


async def project_role(db: AsyncSession, project: Project, user: User) -> Optional[str]:
    if project.owner_id == user.id or bool(getattr(user, "is_superuser", False)):
        return "admin"
    member = (await db.execute(
        select(ProjectMember).where(
            ProjectMember.project_id == project.id,
            ProjectMember.user_id == user.id,
        )
    )).scalar_one_or_none()
    return member.role if member else None


async def require_project_access(
    db: AsyncSession, project_id: int, user: User, min_role: str = "viewer",
) -> Tuple[Project, str]:
    project = (await db.execute(
        select(Project).where(Project.id == project_id, Project.deleted_at.is_(None))
    )).scalar_one_or_none()
    if project is None:
        raise SandboxNotFoundError("项目不存在")
    from .common import sandbox_enabled_for

    if not sandbox_enabled_for(user.id, project.id):
        raise SandboxNotFoundError("项目不存在")
    role = await project_role(db, project, user)
    if role is None:
        # 不泄露项目是否存在：无权限与不存在统一按 404 处理（防 IDOR 枚举）
        raise SandboxNotFoundError("项目不存在")
    if not _satisfies(role, min_role):
        raise SandboxAccessDenied(f"当前项目角色 {role} 无 {min_role} 权限")
    return project, role


async def require_workspace_access(
    db: AsyncSession, workspace_id: int, user: User, min_role: str = "viewer",
) -> Tuple[Workspace, Project, str]:
    workspace = (await db.execute(
        select(Workspace).where(
            Workspace.id == workspace_id, Workspace.deleted_at.is_(None),
        )
    )).scalar_one_or_none()
    if workspace is None:
        raise SandboxNotFoundError("工作空间不存在")
    project = (await db.execute(
        select(Project).where(Project.id == workspace.project_id, Project.deleted_at.is_(None))
    )).scalar_one_or_none()
    if project is None:
        raise SandboxNotFoundError("项目不存在")
    from .common import sandbox_enabled_for

    if not sandbox_enabled_for(user.id, project.id):
        raise SandboxNotFoundError("工作空间不存在")
    role = await project_role(db, project, user)
    if role is None:
        raise SandboxNotFoundError("工作空间不存在")
    if not _satisfies(role, min_role):
        raise SandboxAccessDenied(f"当前项目角色 {role} 无 {min_role} 权限")
    return workspace, project, role


async def require_workspace_by_project(
    db: AsyncSession, project_id: int, user: User, min_role: str = "viewer",
) -> Tuple[Workspace, Project, str]:
    project, role = await require_project_access(db, project_id, user, min_role)
    workspace = (await db.execute(
        select(Workspace).where(
            Workspace.project_id == project.id, Workspace.deleted_at.is_(None),
        )
    )).scalar_one_or_none()
    if workspace is None:
        raise SandboxNotFoundError("项目工作空间尚未创建，请先初始化项目工作空间")
    if not _satisfies(role, min_role):
        raise SandboxAccessDenied(f"当前项目角色 {role} 无 {min_role} 权限")
    return workspace, project, role


def can_execute(role: str) -> bool:
    return _satisfies(role, "operator")


def can_manage(role: str) -> bool:
    return _satisfies(role, "admin")
