"""Conversation Memory 数据访问：索引 Job 队列 / 设置 / 排除 / 分享。

所有查询都在服务端拼装 ACL 条件；模型/前端传入的 ID 只作为候选，不构成授权。
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Sequence

from sqlalchemy import and_, delete, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.agent import AgentSession
from app.models.conversation_memory import (
    ConversationIndexEntry, ConversationIndexJob, ConversationMemoryExclusion,
    ConversationMemorySetting, ConversationMemoryShare,
)
from app.models.sandbox import ProjectMember, Project

DEFAULT_SETTINGS = {
    "history_enabled": True,
    "auto_retrieval_enabled": True,
    "project_memory_enabled": True,
}


async def load_settings(db: AsyncSession, user_id: int) -> Dict[str, Any]:
    """读取用户记忆设置；缺省返回默认值（读取不隐式写库）。"""
    row = (await db.execute(
        select(ConversationMemorySetting).where(
            ConversationMemorySetting.user_id == int(user_id)
        )
    )).scalar_one_or_none()
    if row is None:
        return dict(DEFAULT_SETTINGS)
    return {
        "history_enabled": bool(row.history_enabled),
        "auto_retrieval_enabled": bool(row.auto_retrieval_enabled),
        "project_memory_enabled": bool(row.project_memory_enabled),
    }


async def update_settings(
    db: AsyncSession, user_id: int, values: Dict[str, Any],
) -> Dict[str, Any]:
    row = (await db.execute(
        select(ConversationMemorySetting).where(
            ConversationMemorySetting.user_id == int(user_id)
        )
    )).scalar_one_or_none()
    if row is None:
        row = ConversationMemorySetting(
            user_id=int(user_id),
            history_enabled=bool(values.get("history_enabled", True)),
            auto_retrieval_enabled=bool(values.get("auto_retrieval_enabled", True)),
            project_memory_enabled=bool(values.get("project_memory_enabled", True)),
        )
        db.add(row)
    else:
        for key in ("history_enabled", "auto_retrieval_enabled", "project_memory_enabled"):
            if key in values and values[key] is not None:
                setattr(row, key, bool(values[key]))
    await db.flush()
    return {
        "history_enabled": bool(row.history_enabled),
        "auto_retrieval_enabled": bool(row.auto_retrieval_enabled),
        "project_memory_enabled": bool(row.project_memory_enabled),
    }


def memory_enabled_for_run(agent_config: Optional[Dict[str, Any]], user_settings: Dict[str, Any]) -> bool:
    """Run 级开关（显式 false 优先）+ 用户级开关 + 全局开关。"""
    if not bool(getattr(settings, "CONVERSATION_MEMORY_ENABLED", True)):
        return False
    if agent_config is not None and agent_config.get("history_memory_enabled") is False:
        return False
    return bool(user_settings.get("history_enabled", True))


async def enqueue_index_job(
    db: AsyncSession,
    *,
    session_id: int,
    user_id: Optional[int],
    message_ids: Sequence[str],
) -> bool:
    """为单条消息入队索引任务（与消息写入同一事务）。"""
    target = [str(mid) for mid in message_ids if mid]
    if not target:
        return False
    key = f"index:{int(session_id)}:{target[0]}"
    exists = await db.scalar(
        select(ConversationIndexJob.id).where(ConversationIndexJob.job_key == key)
    )
    if exists:
        return False
    db.add(ConversationIndexJob(
        job_key=key, job_type="index", session_id=int(session_id),
        user_id=int(user_id) if user_id else None,
        message_ids=target, status="pending", attempts=0,
    ))
    return True


async def enqueue_delete_job(db: AsyncSession, *, session_id: int, user_id: Optional[int]) -> bool:
    """会话删除后入队 Qdrant 清理任务（与删除同一事务）。"""
    key = f"delete:{int(session_id)}"
    exists = await db.scalar(
        select(ConversationIndexJob.id).where(ConversationIndexJob.job_key == key)
    )
    if exists:
        return False
    db.add(ConversationIndexJob(
        job_key=key, job_type="delete_conversation", session_id=int(session_id),
        user_id=int(user_id) if user_id else None,
        message_ids=[], status="pending", attempts=0,
    ))
    return True


async def enqueue_reindex_job(db: AsyncSession, *, user_id: int) -> str:
    """用户全量重建（幂等：同一用户已有 pending/processing 任务时直接复用）。"""
    active = await db.scalar(
        select(ConversationIndexJob.id).where(
            ConversationIndexJob.user_id == int(user_id),
            ConversationIndexJob.job_type == "reindex_user",
            ConversationIndexJob.status.in_(("pending", "processing")),
        ).limit(1)
    )
    if active:
        return "existing"
    import uuid

    db.add(ConversationIndexJob(
        job_key=f"reindex:{int(user_id)}:{uuid.uuid4().hex[:12]}",
        job_type="reindex_user", session_id=None, user_id=int(user_id),
        message_ids=[], status="pending", attempts=0,
    ))
    return "queued"


async def claim_next_job(db: AsyncSession, *, lease_seconds: int = 120) -> Optional[ConversationIndexJob]:
    """CAS 领取一个待处理 Job（含 lease 过期回收）。"""
    now = datetime.utcnow()
    row = (await db.execute(
        select(ConversationIndexJob)
        .where(or_(
            ConversationIndexJob.status == "pending",
            and_(
                ConversationIndexJob.status == "processing",
                ConversationIndexJob.lease_expires_at.is_not(None),
                ConversationIndexJob.lease_expires_at < now,
            ),
        ))
        .order_by(ConversationIndexJob.id)
        .limit(1)
    )).scalar_one_or_none()
    if row is None:
        return None
    res = await db.execute(
        update(ConversationIndexJob)
        .where(
            ConversationIndexJob.id == row.id,
            ConversationIndexJob.status == row.status,
        )
        .values(
            status="processing",
            attempts=ConversationIndexJob.attempts + 1,
            lease_expires_at=now + timedelta(seconds=lease_seconds),
            updated_at=now,
        ),
        execution_options={"synchronize_session": False},
    )
    if res.rowcount != 1:
        return None
    await db.flush()
    await db.refresh(row)
    return row


async def finish_job(db: AsyncSession, job_id: int, *, status: str, error: Optional[str] = None) -> None:
    await db.execute(
        update(ConversationIndexJob)
        .where(ConversationIndexJob.id == int(job_id))
        .values(
            status=status,
            last_error=(error or "")[:2000] or None,
            lease_expires_at=None,
            finished_at=datetime.utcnow() if status in ("done", "failed") else None,
            updated_at=datetime.utcnow(),
        ),
        execution_options={"synchronize_session": False},
    )


async def retry_or_fail_job(
    db: AsyncSession, job: ConversationIndexJob, *, error: str,
) -> str:
    """未超过上限 → pending 重试；超过 → failed（保留错误，不静默成功）。"""
    max_attempts = int(getattr(settings, "CONVERSATION_MEMORY_INDEX_MAX_ATTEMPTS", 3) or 3)
    attempts = int(job.attempts or 0)
    status = "pending" if attempts < max_attempts else "failed"
    await db.execute(
        update(ConversationIndexJob)
        .where(ConversationIndexJob.id == job.id)
        .values(
            status=status,
            last_error=(error or "")[:2000],
            lease_expires_at=None,
            finished_at=datetime.utcnow() if status == "failed" else None,
            updated_at=datetime.utcnow(),
        ),
        execution_options={"synchronize_session": False},
    )
    return status


# ── 设置 / 排除 / 分享 ──────────────────────────────────────────────────────

async def set_exclusion(
    db: AsyncSession, *, user_id: int, conversation_id: int, excluded: bool,
    reason: Optional[str] = None,
) -> bool:
    """显式排除/恢复会话；同一事务内同步条目 is_excluded。"""
    session_id = await db.scalar(
        select(AgentSession.id).where(
            AgentSession.id == int(conversation_id),
            AgentSession.user_id == int(user_id),
        )
    )
    if session_id is None:
        raise LookupError("会话不存在")
    if excluded:
        exists = await db.scalar(
            select(ConversationMemoryExclusion.id).where(
                ConversationMemoryExclusion.user_id == int(user_id),
                ConversationMemoryExclusion.conversation_id == int(conversation_id),
            )
        )
        if not exists:
            db.add(ConversationMemoryExclusion(
                user_id=int(user_id), conversation_id=int(conversation_id),
                reason=(reason or "")[:200] or None,
            ))
    else:
        await db.execute(
            delete(ConversationMemoryExclusion).where(
                ConversationMemoryExclusion.user_id == int(user_id),
                ConversationMemoryExclusion.conversation_id == int(conversation_id),
            )
        )
    await db.execute(
        update(ConversationIndexEntry)
        .where(ConversationIndexEntry.session_id == int(conversation_id))
        .values(is_excluded=bool(excluded), updated_at=datetime.utcnow()),
        execution_options={"synchronize_session": False},
    )
    return True


async def get_share(db: AsyncSession, conversation_id: int) -> Optional[ConversationMemoryShare]:
    return (await db.execute(
        select(ConversationMemoryShare).where(
            ConversationMemoryShare.conversation_id == int(conversation_id),
            ConversationMemoryShare.revoked_at.is_(None),
        )
    )).scalar_one_or_none()


async def share_to_project(
    db: AsyncSession, *, user_id: int, conversation_id: int, project_id: int,
) -> ConversationMemoryShare:
    """会话所有者显式共享到其当前项目（可撤销，可审计 shared_by）。"""
    session = (await db.execute(
        select(AgentSession).where(
            AgentSession.id == int(conversation_id),
            AgentSession.user_id == int(user_id),
        )
    )).scalar_one_or_none()
    if session is None:
        raise LookupError("会话不存在")
    if not session.project_id or int(session.project_id) != int(project_id):
        raise ValueError("会话不属于该项目的当前工作区，不能共享到该项目")
    from app.models.user import User
    from app.services.sandbox.acl import project_role

    user = await db.get(User, int(user_id))
    if user is None:
        raise LookupError("用户不存在")
    project = (await db.execute(
        select(Project).where(
            Project.id == int(project_id), Project.deleted_at.is_(None),
        )
    )).scalar_one_or_none()
    if project is None or await project_role(db, project, user) is None:
        # 不泄露项目是否存在：无权限与不存在统一按 404 处理（防 IDOR 枚举）
        raise LookupError("项目不存在")
    existing = await get_share(db, conversation_id)
    if existing is not None:
        return existing
    row = ConversationMemoryShare(
        conversation_id=int(conversation_id),
        project_id=int(project_id),
        shared_by=int(user_id),
    )
    db.add(row)
    await db.flush()
    await db.execute(
        update(ConversationIndexEntry)
        .where(ConversationIndexEntry.session_id == int(conversation_id))
        .values(
            visibility_scope="project", project_id=int(project_id),
            updated_at=datetime.utcnow(),
        ),
        execution_options={"synchronize_session": False},
    )
    return row


async def revoke_share(
    db: AsyncSession, *, user_id: int, conversation_id: int,
) -> bool:
    session_id = await db.scalar(
        select(AgentSession.id).where(
            AgentSession.id == int(conversation_id),
            AgentSession.user_id == int(user_id),
        )
    )
    if session_id is None:
        raise LookupError("会话不存在")
    row = await get_share(db, conversation_id)
    if row is None:
        return False
    row.revoked_at = datetime.utcnow()
    await db.execute(
        update(ConversationIndexEntry)
        .where(ConversationIndexEntry.session_id == int(conversation_id))
        .values(visibility_scope="user", updated_at=datetime.utcnow()),
        execution_options={"synchronize_session": False},
    )
    return True


async def revoke_shares_for_session(db: AsyncSession, conversation_id: int) -> None:
    row = await get_share(db, conversation_id)
    if row is not None:
        row.revoked_at = datetime.utcnow()


async def accessible_project_ids(db: AsyncSession, user_id: int) -> List[int]:
    """当前用户可读的项目集合（owner + 显式成员）。"""
    owned = (await db.execute(
        select(Project.id).where(
            Project.owner_id == int(user_id), Project.deleted_at.is_(None),
        )
    )).scalars().all()
    member = (await db.execute(
        select(ProjectMember.project_id).where(ProjectMember.user_id == int(user_id))
    )).scalars().all()
    return sorted({int(x) for x in list(owned) + list(member)})


async def reindex_scope_rows(
    db: AsyncSession, *, user_id: int, limit: int, offset: int,
) -> List[Any]:
    """用户会话分页（重建用，窄列投影避免大 JSON 排序问题）。"""
    return list((await db.execute(
        select(AgentSession.id, AgentSession.agent_id, AgentSession.project_id)
        .where(AgentSession.user_id == int(user_id))
        .order_by(AgentSession.id)
        .offset(offset).limit(limit)
    )).all())
