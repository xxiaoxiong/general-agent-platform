"""Conversations API v2（§7）：会话列表/创建/消息历史。

会话语义（UX-10）：
- 会话绑定助手（缺省 = 默认助手）；切换会话不串消息/状态/文件；
- 列表按用户隔离（A22）；消息历史只读展示 Run 写回的对话记录。
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import defer

from app.api.deps import get_current_user
from app.core.database import get_db
from app.models.agent import Agent, AgentSession, session_message_count_expr
from app.models.user import User

from .schemas import ConversationCreate
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
    msg_count = session_message_count_expr(db.get_bind().dialect.name)
    res = await db.execute(
        select(AgentSession, msg_count.label("message_count"))
        .options(defer(AgentSession.messages))
        .where(cond)
        .order_by(AgentSession.updated_at.desc())
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
    session = await _get_owned_session(db, session_id, current_user)
    await db.delete(session)
    await db.commit()
    return {"message": "已删除"}
