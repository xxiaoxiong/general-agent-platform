"""Knowledge ACL（任务书 §11.1）。

所有权 / 显式共享（项目 / 组织）统一入口；Agent 绑定校验不能替代 API 校验。
- owner 或平台超管：admin；
- ``visibility=project``：复用 ProjectMember 角色（viewer 只读 / operator 写入 / admin 管理）；
- ``visibility=organization``：登录用户只读，写操作仍要求 owner/admin；
- 无权限与不存在统一 404，避免 IDOR 枚举。
"""
from __future__ import annotations

from typing import Optional, Sequence, Tuple

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.knowledge import KnowledgeBase
from app.models.sandbox import ProjectMember
from app.models.user import User

from app.services.sandbox.acl import project_role

_WRITE_ACTIONS = {"write", "upload", "reindex", "delete_document", "crawl"}
_ADMIN_ACTIONS = {"manage", "delete"}


class KnowledgeNotFoundError(Exception):
    """知识库不存在或当前用户不可见（统一 404）。"""


class KnowledgeAccessDenied(Exception):
    """已确认存在但权限不足（403）。"""


def _read_roles() -> set:
    return {"viewer", "operator", "admin"}


async def require_base_access(
    db: AsyncSession,
    kb_id: int,
    user: User,
    action: str = "read",
) -> Tuple[KnowledgeBase, str]:
    kb = (await db.execute(
        select(KnowledgeBase).where(
            KnowledgeBase.id == int(kb_id),
            KnowledgeBase.status != "deleted",
        )
    )).scalar_one_or_none()
    if kb is None:
        raise KnowledgeNotFoundError("知识库不存在")

    if bool(getattr(user, "is_superuser", False)) or kb.owner_id == user.id:
        return kb, "admin"

    role: Optional[str] = None
    if kb.visibility == "organization":
        role = "viewer"
    elif kb.visibility == "project" and kb.project_id:
        from app.models.sandbox import Project

        project = (await db.execute(
            select(Project).where(Project.id == int(kb.project_id), Project.deleted_at.is_(None))
        )).scalar_one_or_none()
        if project is not None:
            role = await project_role(db, project, user)

    if role is None:
        raise KnowledgeNotFoundError("知识库不存在")

    if action in _ADMIN_ACTIONS:
        if role != "admin":
            raise KnowledgeAccessDenied("仅知识库所有者或项目管理员可以执行该操作")
        return kb, role
    if action in _WRITE_ACTIONS:
        if role not in {"operator", "admin"}:
            raise KnowledgeAccessDenied("当前项目角色只读，不能修改知识库")
        return kb, role
    if role not in _read_roles():
        raise KnowledgeAccessDenied("无权访问该知识库")
    return kb, role


def accessible_bases_filter(user: User):
    """列表查询过滤条件（owner / 组织共享 / 项目成员）。"""
    if bool(getattr(user, "is_superuser", False)):
        return KnowledgeBase.status != "deleted"
    member_projects = select(ProjectMember.project_id).where(ProjectMember.user_id == user.id)
    return and_(
        KnowledgeBase.status != "deleted",
        or_(
            KnowledgeBase.owner_id == user.id,
            KnowledgeBase.visibility == "organization",
            and_(
                KnowledgeBase.visibility == "project",
                KnowledgeBase.project_id.in_(member_projects),
            ),
        ),
    )


async def filter_authorized_ids(
    db: AsyncSession, user: User, kb_ids: Sequence[int],
) -> list:
    """把请求中的知识库 ID 收窄为当前用户可读集合（不泄露哪些 ID 无效）。"""
    requested = [int(x) for x in kb_ids if x is not None]
    if not requested:
        return []
    rows = (await db.execute(
        select(KnowledgeBase.id).where(
            KnowledgeBase.id.in_(requested),
            accessible_bases_filter(user),
            KnowledgeBase.is_active.is_(True),
        )
    )).scalars().all()
    return [int(x) for x in rows]
