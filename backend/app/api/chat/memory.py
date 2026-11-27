"""Conversation Memory 用户设置 / 状态 / 重建 / 排除 / 分享 API（/api/v2/memory）。

- 所有端点只作用于当前登录用户；会话必须属于当前用户（统一 404/403 语义）；
- 项目共享需要会话绑定该项目 + 当前用户具备项目成员资格；
- 重建是异步 Job，接口只负责入队，不阻塞请求。
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.core.config import settings
from app.core.database import get_db
from app.models.agent import AgentSession
from app.models.conversation_memory import ConversationIndexEntry, ConversationIndexJob
from app.models.user import User
from app.services.conversation_memory import repository
from app.services.conversation_memory.indexer import current_index_version

router = APIRouter(prefix="/memory", tags=["conversation-memory-v2"])


class MemorySettingsPatch(BaseModel):
    history_enabled: Optional[bool] = None
    auto_retrieval_enabled: Optional[bool] = None
    project_memory_enabled: Optional[bool] = None


class ConversationExclusionPatch(BaseModel):
    excluded: bool
    reason: Optional[str] = Field(None, max_length=200)


class ConversationShareRequest(BaseModel):
    project_id: Optional[int] = Field(None, gt=0)


async def _settings_payload(db: AsyncSession, user_id: int) -> Dict[str, Any]:
    values = await repository.load_settings(db, user_id)
    return {
        **values,
        "global_enabled": bool(getattr(settings, "CONVERSATION_MEMORY_ENABLED", True)),
        "planner_mode": str(getattr(settings, "CONVERSATION_MEMORY_PLANNER_MODE", "llm")),
        "index_version": current_index_version(),
    }


async def _index_counts(db: AsyncSession, user_id: int) -> Dict[str, Any]:
    version = current_index_version()
    entries = int(await db.scalar(
        select(func.count()).select_from(ConversationIndexEntry).where(
            ConversationIndexEntry.user_id == int(user_id),
            ConversationIndexEntry.is_deleted.is_(False),
            ConversationIndexEntry.is_excluded.is_(False),
            ConversationIndexEntry.index_version == version,
        )
    ) or 0)
    pending = int(await db.scalar(
        select(func.count()).select_from(ConversationIndexJob).where(
            ConversationIndexJob.user_id == int(user_id),
            ConversationIndexJob.status.in_(("pending", "processing")),
        )
    ) or 0)
    failed = int(await db.scalar(
        select(func.count()).select_from(ConversationIndexJob).where(
            ConversationIndexJob.user_id == int(user_id),
            ConversationIndexJob.status == "failed",
        )
    ) or 0)
    last = (await db.execute(
        select(ConversationIndexJob)
        .where(ConversationIndexJob.user_id == int(user_id))
        .order_by(ConversationIndexJob.id.desc())
        .limit(1)
    )).scalar_one_or_none()
    return {
        "indexed_entries": entries,
        "pending_jobs": pending,
        "failed_jobs": failed,
        "last_job": ({
            "id": int(last.id), "job_type": last.job_type, "status": last.status,
            "attempts": int(last.attempts or 0), "last_error": last.last_error,
            "updated_at": last.updated_at.isoformat() if last.updated_at else None,
        } if last else None),
    }


@router.get("/settings")
async def get_memory_settings(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return {
        "settings": await _settings_payload(db, current_user.id),
        "index": await _index_counts(db, current_user.id),
    }


@router.patch("/settings")
async def update_memory_settings(
    body: MemorySettingsPatch,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if not any(value is not None for value in body.model_dump().values()):
        raise HTTPException(status_code=422, detail="至少提供一个要修改的开关")
    values = await repository.update_settings(db, current_user.id, body.model_dump())
    await db.commit()
    return {
        "settings": {
            **values,
            "global_enabled": bool(getattr(settings, "CONVERSATION_MEMORY_ENABLED", True)),
            "planner_mode": str(getattr(settings, "CONVERSATION_MEMORY_PLANNER_MODE", "llm")),
            "index_version": current_index_version(),
        },
        "index": await _index_counts(db, current_user.id),
    }


@router.get("/status")
async def memory_status(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return {
        "settings": await _settings_payload(db, current_user.id),
        "index": await _index_counts(db, current_user.id),
    }


@router.post("/reindex")
async def reindex_memory(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await repository.enqueue_reindex_job(db, user_id=current_user.id)
    await db.commit()
    return {"status": result, "index": await _index_counts(db, current_user.id)}


@router.put("/conversations/{conversation_id}/exclusion")
async def set_conversation_exclusion(
    conversation_id: int,
    body: ConversationExclusionPatch,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    try:
        await repository.set_exclusion(
            db, user_id=current_user.id, conversation_id=conversation_id,
            excluded=bool(body.excluded), reason=body.reason,
        )
    except LookupError:
        raise HTTPException(status_code=404, detail="会话不存在") from None
    await db.commit()
    return {"conversation_id": conversation_id, "excluded": bool(body.excluded)}


@router.post("/conversations/{conversation_id}/share")
async def share_conversation(
    conversation_id: int,
    body: ConversationShareRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    session = (await db.execute(
        select(AgentSession).where(
            AgentSession.id == conversation_id,
            AgentSession.user_id == current_user.id,
        )
    )).scalar_one_or_none()
    if session is None:
        raise HTTPException(status_code=404, detail="会话不存在")
    project_id = int(body.project_id or session.project_id or 0)
    if project_id <= 0:
        raise HTTPException(status_code=422, detail="会话未绑定项目，不能共享到项目")
    try:
        share = await repository.share_to_project(
            db, user_id=current_user.id, conversation_id=conversation_id,
            project_id=project_id,
        )
    except LookupError:
        raise HTTPException(status_code=404, detail="会话不存在") from None
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    except Exception as exc:  # noqa: BLE001 项目 ACL 拒绝按 404（不泄露存在性）
        from app.services.sandbox.common import SandboxError

        if isinstance(exc, SandboxError):
            raise HTTPException(status_code=exc.status_code, detail=exc.message) from None
        raise
    await db.commit()
    return {
        "conversation_id": conversation_id,
        "project_id": int(share.project_id),
        "shared_by": int(share.shared_by),
    }


@router.delete("/conversations/{conversation_id}/share")
async def revoke_conversation_share(
    conversation_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    try:
        revoked = await repository.revoke_share(
            db, user_id=current_user.id, conversation_id=conversation_id,
        )
    except LookupError:
        raise HTTPException(status_code=404, detail="会话不存在") from None
    await db.commit()
    return {"conversation_id": conversation_id, "revoked": bool(revoked)}
