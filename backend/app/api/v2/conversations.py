"""Conversations API v2（§7）：会话列表/创建/消息历史。

会话语义（UX-10）：
- 会话绑定助手（缺省 = 默认助手）；切换会话不串消息/状态/文件；
- 列表按用户隔离（A22）；消息历史只读展示 Run 写回的对话记录。
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import and_, delete, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import defer

from app.api.deps import get_current_user
from app.core.database import get_db
from app.models.agent import Agent, AgentRun, AgentSession, session_message_count_expr
from app.models.evolution import AgentEvaluation
from app.models.user import User
from app.services.agent_runtime.lifecycle import TERMINAL_STATUSES
from app.services.agent_runtime.repositories.run_repository import _sql_now
from app.services.agent_runtime.repositories.session_repository import SessionRepository

from .schemas import ConversationCreate, ConversationPatch
from .serializers import conversation_to_dict

router = APIRouter(prefix="/conversations", tags=["conversations-v2"])


async def _get_owned_session(db: AsyncSession, session_id: int, user: User) -> AgentSession:
    res = await db.execute(
        select(AgentSession).where(
            AgentSession.id == session_id,
            AgentSession.user_id == user.id,
        )
    )
    session = res.scalar_one_or_none()
    if session is None:
        raise HTTPException(status_code=404, detail="会话不存在")
    return session


@router.get("")
async def list_conversations(
    assistant_id: Optional[int] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """会话列表（仅当前用户；可按助手过滤）。

    列表裁掉 messages 大列（长会话 JSON 达 MB 级，参与 ORDER BY filesort
    会撑爆 MySQL sort buffer 报 1038），消息条数改由 JSON 函数库内计算。
    """
    cond = AgentSession.user_id == current_user.id
    if assistant_id is not None:
        cond = and_(cond, AgentSession.agent_id == assistant_id)
    total = (await db.execute(select(func.count(AgentSession.id)).where(cond))).scalar()
    dialect = db.get_bind().dialect.name
    msg_count = session_message_count_expr(dialect)
    pin_order = AgentSession.pinned_at.desc()
    if dialect == "postgresql":
        pin_order = pin_order.nulls_last()
    res = await db.execute(
        select(AgentSession, msg_count.label("message_count"))
        .options(defer(AgentSession.messages))
        .where(cond)
        .order_by(pin_order, AgentSession.updated_at.desc(), AgentSession.id.desc())
        .offset((page - 1) * page_size).limit(page_size)
    )
    sessions = res.all()
    return {
        "items": [conversation_to_dict(s, message_count=mc) for s, mc in sessions],
        "total": total, "page": page, "page_size": page_size,
    }


@router.post("")
async def create_conversation(
    body: ConversationCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """创建会话（缺省绑定默认助手，UX-01）。"""
    agent_id = body.assistant_id
    if agent_id is None:
        from app.assistant.default_assistant import ensure_default_assistant

        agent = await ensure_default_assistant(db, current_user.id, created_by=current_user.id)
        agent_id = agent.id
    else:
        res = await db.execute(
            select(Agent).where(Agent.id == agent_id, Agent.is_active == True)  # noqa: E712
        )
        agent = res.scalar_one_or_none()
        if agent is None or agent.owner_id not in (None, current_user.id):
            raise HTTPException(status_code=404, detail="助手不存在")

    session = AgentSession(
        agent_id=agent_id, user_id=current_user.id,
        title=body.title or "新对话", messages=[],
    )
    db.add(session)
    await db.commit()
    await db.refresh(session)
    return conversation_to_dict(session)


@router.get("/{session_id}")
async def get_conversation(
    session_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """按 ID 读取当前用户的会话，支持从文档历史打开原会话。"""
    return conversation_to_dict(await _get_owned_session(db, session_id, current_user))


@router.patch("/{session_id}")
async def update_conversation(
    session_id: int,
    body: ConversationPatch,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """在活动任务期间也可调整会话标题及置顶状态。"""
    updated = await SessionRepository(db).update_metadata(
        session_id, current_user.id, title=body.title, is_pinned=body.is_pinned,
    )
    if not updated:
        raise HTTPException(status_code=404, detail="会话不存在")
    msg_count = session_message_count_expr(db.get_bind().dialect.name)
    row = await db.execute(
        select(AgentSession, msg_count.label("message_count"))
        .options(defer(AgentSession.messages))
        .where(AgentSession.id == session_id)
    )
    session, count = row.one()
    result = conversation_to_dict(session, message_count=count)
    await db.commit()
    return result


@router.get("/{session_id}/messages")
async def get_conversation_messages(
    session_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    session = await _get_owned_session(db, session_id, current_user)
    return {"items": session.messages or []}


@router.delete("/{session_id}")
async def delete_conversation(
    session_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    # 锁住会话行，防止同一会话在拆除历史引用时被新的 Run 领取。
    session = (await db.execute(
        select(
            AgentSession.id,
            AgentSession.active_run_id,
            (AgentSession.active_run_lease_expires_at > _sql_now(db)).label("has_live_lease"),
        )
        .where(AgentSession.id == session_id, AgentSession.user_id == current_user.id)
        .with_for_update()
    )).one_or_none()
    if session is None:
        raise HTTPException(status_code=404, detail="会话不存在")

    # 没有执行权的旧 Run 仍可能在排队、等待审批或等待恢复。
    busy = await db.scalar(
        select(AgentRun.id)
        .where(
            AgentRun.session_id == session_id,
            or_(AgentRun.status.is_(None), AgentRun.status.not_in(TERMINAL_STATUSES)),
        )
        .limit(1)
    )
    if not busy and session.active_run_id and session.has_live_lease:
        # MySQL 快照可能尚未看到刚领取会话的 Run，租约可保护这段窗口。
        # 已进入终态的 Run 可以解除关联，即使历史数据残留了租约。
        claimed_status = await db.scalar(
            select(AgentRun.status).where(
                AgentRun.id == session.active_run_id, AgentRun.session_id == session_id,
            )
        )
        busy = claimed_status not in TERMINAL_STATUSES
    if busy:
        raise HTTPException(status_code=409, detail="会话仍有未完成的任务，请取消或等待任务结束后再删除")

    # Run、事件、Trace 和评测仍可按 Run 查询。仅解除已删除会话的可空外键引用。
    await db.execute(
        update(AgentEvaluation)
        .where(AgentEvaluation.session_id == session_id)
        .values(session_id=None, updated_at=AgentEvaluation.updated_at)
    )
    await db.execute(
        update(AgentRun)
        .where(AgentRun.session_id == session_id, AgentRun.status.in_(TERMINAL_STATUSES))
        .values(session_id=None, updated_at=AgentRun.updated_at)
    )
    await db.execute(delete(AgentSession).where(AgentSession.id == session_id))
    await db.commit()
    return {"message": "已删除"}
