"""Workspace 写租约（任务书 §3.2）。

- 同一 Workspace 默认只允许一个会修改文件系统的 Agent Run 持有写租约；
- 租约带过期时间 + Heartbeat + Fencing Token（token 单调递增）；
- 崩溃后租约过期方可被接管；旧持有者写操作必须校验 token。
"""
from __future__ import annotations

from datetime import timedelta
from typing import Optional

from sqlalchemy import and_, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.sandbox import Workspace

from .common import emit_event, utcnow


async def acquire_write_lease(
    db: AsyncSession, workspace_id: int, owner: str,
    *, ttl_seconds: Optional[int] = None, user_id: Optional[int] = None,
) -> Optional[int]:
    """CAS 获取写租约：空闲 / 已过期 / 自己持有 时可获取；否则返回 None。

    返回新的 Fencing Token；未获取到返回 None（调用方应等待或拒绝本次写执行）。
    """
    ttl = int(ttl_seconds or settings.SANDBOX_WRITE_LEASE_SECONDS)
    now = utcnow()
    result = await db.execute(
        update(Workspace)
        .where(
            Workspace.id == workspace_id,
            Workspace.deleted_at.is_(None),
            or_(
                Workspace.write_lease_owner.is_(None),
                Workspace.write_lease_owner == owner,
                Workspace.write_lease_expires_at.is_(None),
                Workspace.write_lease_expires_at < now,
            ),
        )
        .values(
            write_lease_owner=owner,
            write_lease_token=Workspace.write_lease_token + 1,
            write_lease_expires_at=now + timedelta(seconds=ttl),
            write_lease_heartbeat_at=now,
            updated_at=now,
        )
    )
    if result.rowcount == 0:
        return None
    token = (await db.execute(
        select(Workspace.write_lease_token).where(Workspace.id == workspace_id)
    )).scalar()
    await emit_event(
        db, "workspace.write_lease_acquired", user_id=user_id,
        workspace_id=workspace_id,
        payload={"owner": owner, "ttl_seconds": ttl, "token": token},
    )
    return int(token or 0)


async def renew_write_lease(
    db: AsyncSession, workspace_id: int, owner: str, token: int,
    *, ttl_seconds: Optional[int] = None,
) -> bool:
    ttl = int(ttl_seconds or settings.SANDBOX_WRITE_LEASE_SECONDS)
    now = utcnow()
    result = await db.execute(
        update(Workspace)
        .where(
            Workspace.id == workspace_id,
            Workspace.write_lease_owner == owner,
            Workspace.write_lease_token == int(token),
            Workspace.write_lease_expires_at.is_not(None),
            Workspace.write_lease_expires_at >= now,
        )
        .values(
            write_lease_expires_at=now + timedelta(seconds=ttl),
            write_lease_heartbeat_at=now,
            updated_at=now,
        )
    )
    return result.rowcount > 0


async def release_write_lease(
    db: AsyncSession, workspace_id: int, owner: str, token: int,
) -> bool:
    result = await db.execute(
        update(Workspace)
        .where(
            Workspace.id == workspace_id,
            Workspace.write_lease_owner == owner,
            Workspace.write_lease_token == int(token),
        )
        .values(
            write_lease_owner=None,
            write_lease_expires_at=None,
            write_lease_heartbeat_at=None,
            updated_at=utcnow(),
        )
    )
    return result.rowcount > 0


async def write_lease_valid(
    db: AsyncSession, workspace_id: int, owner: str, token: int,
) -> bool:
    now = utcnow()
    row = (await db.execute(
        select(Workspace.write_lease_owner).where(
            and_(
                Workspace.id == workspace_id,
                Workspace.write_lease_owner == owner,
                Workspace.write_lease_token == int(token),
                Workspace.write_lease_expires_at.is_not(None),
                Workspace.write_lease_expires_at >= now,
            )
        )
    )).scalar_one_or_none()
    return row is not None


async def current_write_lease(db: AsyncSession, workspace_id: int) -> Optional[dict]:
    workspace = (await db.execute(
        select(Workspace).where(Workspace.id == workspace_id)
    )).scalar_one_or_none()
    if workspace is None or workspace.write_lease_owner is None:
        return None
    return {
        "owner": workspace.write_lease_owner,
        "token": workspace.write_lease_token,
        "expires_at": workspace.write_lease_expires_at,
        "heartbeat_at": workspace.write_lease_heartbeat_at,
    }


async def release_owner(db: AsyncSession, workspace_id: int, owner: str) -> bool:
    """按持有者释放（Run 结束时调用；token 不再校验，owner 唯一即可）。"""
    result = await db.execute(
        update(Workspace)
        .where(Workspace.id == workspace_id, Workspace.write_lease_owner == owner)
        .values(
            write_lease_owner=None,
            write_lease_expires_at=None,
            write_lease_heartbeat_at=None,
            updated_at=utcnow(),
        )
    )
    return result.rowcount > 0


async def release_run_write_lease(project_id: int, run_id: str) -> bool:
    """Run 结束时释放其持有的项目 Workspace 写租约（独立会话，不做跨 Run 缓存）。"""
    from app.core.database import AsyncSessionLocal

    async with AsyncSessionLocal() as db:
        workspace = (await db.execute(
            select(Workspace).where(
                Workspace.project_id == project_id, Workspace.deleted_at.is_(None))
        )).scalar_one_or_none()
        if workspace is None:
            return False
        released = await release_owner(db, workspace.id, f"run:{run_id}")
        await db.commit()
        return released
