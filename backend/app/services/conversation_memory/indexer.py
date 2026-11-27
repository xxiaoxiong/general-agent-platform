"""会话索引器：两级索引构建、Embedding 同步与删除清理。

事实来源是 ``agent_sessions.messages``；本模块只生成派生索引。
- Turn：一轮用户提问 + 助手最终回答（精确定位）；
- Segment：最近若干轮组成的主题片段（保留讨论脉络）。
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional, Sequence

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.agent import AgentSession
from app.models.conversation_memory import ConversationIndexEntry
from app.services.agent_runtime.events import redact_secrets
from app.services.knowledge.embedding import EmbeddingError, get_embedding_provider
from app.services.knowledge.tokens import count_tokens
from app.services.knowledge.vector_store import get_vector_store

logger = logging.getLogger("ai_plat.conversation_memory")

_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_SPACE_RE = re.compile(r"[ \t]+")


def current_index_version() -> str:
    model = settings.KNOWLEDGE_EMBEDDING_MODEL or "unknown"
    dimension = int(settings.KNOWLEDGE_EMBEDDING_DIMENSION or 0)
    base = settings.CONVERSATION_MEMORY_INDEX_VERSION or "cvm1"
    return f"{base}:{model}:{dimension or 'auto'}"


def memory_collection_name(model: str, dimension: int) -> str:
    prefix = (settings.CONVERSATION_MEMORY_QDRANT_COLLECTION_PREFIX or "gap_memory").strip("_")
    slug = re.sub(r"[^a-z0-9]+", "_", (model or "").lower()).strip("_")[:48] or "model"
    return f"{prefix}_{slug}_{int(dimension)}"


def content_hash(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def sanitize_text(value: Any) -> str:
    """历史内容脱敏 + 去控制字符 + 折叠空白（脱敏不能替代 ACL）。"""
    text = redact_secrets(str(value or ""))
    if not isinstance(text, str):
        text = str(text)
    text = _CONTROL_RE.sub("", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _SPACE_RE.sub(" ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _message_content(message: Dict[str, Any]) -> str:
    content = message.get("content")
    if isinstance(content, list):
        parts: List[str] = []
        for item in content:
            if isinstance(item, dict):
                parts.append(str(item.get("text") or ""))
            else:
                parts.append(str(item))
        content = "".join(parts)
    return sanitize_text(content)


def _message_created_at(message: Dict[str, Any]) -> Optional[datetime]:
    raw = message.get("created_at")
    if isinstance(raw, datetime):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00")).replace(tzinfo=None)
        except ValueError:
            return None
    return None


def _message_id(message: Dict[str, Any]) -> str:
    return str(message.get("message_id") or "").strip()


def _is_indexable(message: Dict[str, Any]) -> bool:
    if message.get("steering_run_id"):
        return False
    return message.get("role") in ("user", "assistant")


@dataclass
class IndexedUnit:
    entry_key: str
    entry_type: str
    role: str
    message_ids: List[str]
    content: str
    message_created_at: Optional[datetime]
    token_count: int


@dataclass
class Turn:
    user_message: Optional[Dict[str, Any]]
    assistant_message: Dict[str, Any]
    content: str


def _build_turns(messages: Sequence[Dict[str, Any]]) -> List[Turn]:
    """按时间顺序构建轮次：每条 Assistant 回答配对其最近的前一条用户消息。"""
    turns: List[Turn] = []
    pending_user: Optional[Dict[str, Any]] = None
    for message in messages:
        if not isinstance(message, dict) or not _is_indexable(message):
            continue
        role = message.get("role")
        if role == "user":
            pending_user = message
            continue
        content = _message_content(message)
        if not content:
            pending_user = None
            continue
        user_text = _message_content(pending_user) if pending_user else ""
        if user_text:
            turn_text = f"用户：{user_text}\n助手：{content}"
        else:
            turn_text = f"助手：{content}"
        turns.append(Turn(user_message=pending_user, assistant_message=message, content=turn_text))
        pending_user = None
    return turns


def _truncate(text: str, max_chars: int) -> str:
    if max_chars > 0 and len(text) > max_chars:
        return text[:max_chars] + f"\n…（截断，原文共 {len(text)} 字）"
    return text


def _build_units(messages: Sequence[Dict[str, Any]], target_message_ids: Optional[set]) -> List[IndexedUnit]:
    turns = _build_turns(messages)
    max_turn_chars = int(getattr(settings, "CONVERSATION_MEMORY_TURN_MAX_CHARS", 4000) or 4000)
    segment_turns = max(1, int(getattr(settings, "CONVERSATION_MEMORY_SEGMENT_TURNS", 3) or 3))
    segment_chars = int(getattr(settings, "CONVERSATION_MEMORY_SEGMENT_MAX_CHARS", 3000) or 3000)
    units: List[IndexedUnit] = []
    for index, turn in enumerate(turns):
        assistant_id = _message_id(turn.assistant_message)
        if not assistant_id:
            continue
        if target_message_ids is not None and assistant_id not in target_message_ids:
            # 支持用户消息 ID：命中的用户消息会激活其后的助手回答
            user_id = _message_id(turn.user_message) if turn.user_message else ""
            if not user_id or user_id not in target_message_ids:
                continue
        message_ids = [
            mid for mid in (
                _message_id(turn.user_message) if turn.user_message else "",
                assistant_id,
            ) if mid
        ]
        turn_content = _truncate(turn.content, max_turn_chars)
        units.append(IndexedUnit(
            entry_key=f"turn:{assistant_id}",
            entry_type="turn",
            role="turn",
            message_ids=message_ids,
            content=turn_content,
            message_created_at=_message_created_at(turn.assistant_message),
            token_count=count_tokens(turn_content),
        ))
        # 主题片段：最近 N 轮（含当前轮），同一轮次集合生成稳定指纹
        start = max(0, index - segment_turns + 1)
        window = turns[start:index + 1]
        segment_message_ids: List[str] = []
        for item in window:
            if item.user_message:
                segment_message_ids.append(_message_id(item.user_message))
            segment_message_ids.append(_message_id(item.assistant_message))
        segment_message_ids = [mid for mid in segment_message_ids if mid]
        if len(window) < 2:
            continue
        fingerprint = hashlib.sha1("|".join(segment_message_ids).encode("utf-8")).hexdigest()[:20]
        segment_content = "\n\n".join(
            f"[轮次 {position + 1}]\n{item.content}" for position, item in enumerate(window)
        )
        segment_content = _truncate(segment_content, segment_chars)
        units.append(IndexedUnit(
            entry_key=f"seg:{fingerprint}",
            entry_type="segment",
            role="segment",
            message_ids=segment_message_ids,
            content=segment_content,
            message_created_at=_message_created_at(window[-1].assistant_message),
            token_count=count_tokens(segment_content),
        ))
    return units


async def _load_session(db: AsyncSession, session_id: int) -> Optional[AgentSession]:
    return (await db.execute(
        select(AgentSession)
        .where(AgentSession.id == int(session_id))
        .execution_options(populate_existing=True)
    )).scalar_one_or_none()


async def _upsert_entry(
    db: AsyncSession, session: AgentSession, unit: IndexedUnit, index_version: str,
) -> tuple[Optional[ConversationIndexEntry], bool, bool]:
    """幂等写入条目；返回 (条目, 是否需要重新 Embedding, 是否新建)。"""
    existing = (await db.execute(
        select(ConversationIndexEntry).where(
            ConversationIndexEntry.session_id == int(session.id),
            ConversationIndexEntry.entry_key == unit.entry_key,
        )
    )).scalar_one_or_none()
    digest = content_hash(unit.content)
    if existing is not None:
        unchanged = (
            existing.content_hash == digest
            and existing.index_version == index_version
            and str(existing.content or "") == unit.content
            and not bool(existing.is_deleted)
        )
        existing.is_deleted = False
        existing.content = unit.content
        existing.content_hash = digest
        existing.token_count = unit.token_count
        existing.message_ids = list(unit.message_ids)
        existing.message_created_at = unit.message_created_at
        existing.index_version = index_version
        existing.user_id = int(session.user_id or 0)
        existing.agent_id = int(session.agent_id or 0) or None
        # 项目归属只用于显式共享过滤；未共享时保持 user scope
        if existing.visibility_scope != "project":
            existing.project_id = int(session.project_id) if session.project_id else None
        if unchanged and existing.embedding_status == "indexed":
            return existing, False, False
        return existing, True, False
    entry = ConversationIndexEntry(
        entry_key=unit.entry_key,
        session_id=int(session.id),
        user_id=int(session.user_id or 0),
        agent_id=int(session.agent_id or 0) or None,
        project_id=int(session.project_id) if session.project_id else None,
        entry_type=unit.entry_type,
        message_ids=list(unit.message_ids),
        role=unit.role,
        content=unit.content,
        content_hash=digest,
        token_count=unit.token_count,
        message_created_at=unit.message_created_at,
        index_version=index_version,
        visibility_scope="user",
        is_deleted=False,
        is_excluded=False,
        embedding_status="pending",
    )
    db.add(entry)
    return entry, True, True


async def _embed_and_upsert(entries: List[ConversationIndexEntry]) -> Dict[str, Any]:
    """批量为条目生成向量并写入 Qdrant；失败时标记 degraded（全文仍可用）。"""
    if not entries:
        return {"embedded": 0, "degraded": 0}
    provider = get_embedding_provider()
    texts = [str(entry.content or "") for entry in entries]
    try:
        vectors = await provider.embed(texts)
    except EmbeddingError as exc:
        logger.warning("会话索引 Embedding 不可用，降级为全文索引：%s", exc)
        for entry in entries:
            entry.embedding_status = "degraded"
            entry.embedding_model = str(getattr(provider, "model", "") or "")
            entry.embedding_dimension = int(getattr(provider, "dimension", 0) or 0)
        return {"embedded": 0, "degraded": len(entries), "error": str(exc)}
    except Exception as exc:  # noqa: BLE001 Embedding 故障不得中断索引/对话
        logger.warning("会话索引 Embedding 异常，降级为全文索引：%s", exc)
        for entry in entries:
            entry.embedding_status = "degraded"
        return {"embedded": 0, "degraded": len(entries), "error": str(exc)}
    if len(vectors) != len(entries):
        for entry in entries:
            entry.embedding_status = "degraded"
        return {"embedded": 0, "degraded": len(entries), "error": "Embedding 返回数量不匹配"}

    dimension = len(vectors[0]) if vectors else 0
    if dimension <= 0:
        return {"embedded": 0, "degraded": len(entries), "error": "Embedding 维度为 0"}
    collection = memory_collection_name(provider.model, dimension)
    store = get_vector_store()
    try:
        await store.ensure_collection(collection, dimension)
    except Exception as exc:  # noqa: BLE001
        logger.warning("会话索引 Collection 创建失败：%s", exc)
        for entry in entries:
            entry.embedding_status = "degraded"
        return {"embedded": 0, "degraded": len(entries), "error": str(exc)}

    points = []
    for entry, vector in zip(entries, vectors):
        if entry.id is None:
            continue
        points.append({
            "chunk_id": str(entry.id),
            "vector": vector,
            "payload": {
                "entry_id": int(entry.id),
                "entry_key": entry.entry_key,
                "session_id": int(entry.session_id),
                "user_id": int(entry.user_id),
                "project_id": int(entry.project_id) if entry.project_id else None,
                "visibility_scope": entry.visibility_scope,
                "entry_type": entry.entry_type,
                "index_version": entry.index_version,
                "is_deleted": False,
                "is_excluded": False,
                "message_created_at": (
                    entry.message_created_at.isoformat() if entry.message_created_at else None
                ),
            },
        })
    try:
        await store.upsert(collection, points)
    except Exception as exc:  # noqa: BLE001
        logger.warning("会话索引向量写入失败：%s", exc)
        for entry in entries:
            entry.embedding_status = "degraded"
        return {"embedded": 0, "degraded": len(entries), "error": str(exc)}
    for entry in entries:
        entry.embedding_status = "indexed"
        entry.embedding_model = provider.model
        entry.embedding_dimension = dimension
    return {"embedded": len(entries), "degraded": 0}


@dataclass
class IndexStats:
    units: int = 0
    created: int = 0
    updated: int = 0
    embedded: int = 0
    degraded: int = 0
    deleted: int = 0
    errors: List[str] = field(default_factory=list)


async def index_session(
    db: AsyncSession,
    session: AgentSession,
    *,
    message_ids: Optional[Sequence[str]] = None,
    prune: bool = False,
) -> IndexStats:
    """索引会话中的目标消息（None=全部）；幂等、可重试。"""
    stats = IndexStats()
    messages = [m for m in (session.messages or []) if isinstance(m, dict)]
    targets = {str(mid) for mid in (message_ids or []) if mid} or None
    units = _build_units(messages, targets)
    stats.units = len(units)
    version = current_index_version()
    pending: List[ConversationIndexEntry] = []
    for unit in units:
        entry, needs_embedding, created = await _upsert_entry(db, session, unit, version)
        if entry is None:
            continue
        if entry.id is None:
            await db.flush()
        if created:
            stats.created += 1
        elif needs_embedding:
            stats.updated += 1
        if needs_embedding:
            pending.append(entry)
    if pending:
        await db.flush()
        result = await _embed_and_upsert(pending)
        stats.embedded = int(result.get("embedded") or 0)
        stats.degraded = int(result.get("degraded") or 0)
        if result.get("error"):
            stats.errors.append(str(result["error"]))
    if prune:
        valid_keys = {unit.entry_key for unit in units}
        rows = (await db.execute(
            select(ConversationIndexEntry).where(
                ConversationIndexEntry.session_id == int(session.id)
            )
        )).scalars().all()
        for row in rows:
            if valid_keys and row.entry_key not in valid_keys:
                await db.delete(row)
                stats.deleted += 1
        if not valid_keys and rows:
            # 会话已无任何可索引消息：清空派生条目
            for row in rows:
                await db.delete(row)
                stats.deleted += 1
    return stats


async def index_message(db: AsyncSession, session_id: int, message_id: str) -> IndexStats:
    session = await _load_session(db, session_id)
    if session is None:
        return IndexStats(errors=["会话不存在"])
    return await index_session(db, session, message_ids=[message_id])


async def reindex_user(db: AsyncSession, user_id: int, *, batch_size: int = 20) -> IndexStats:
    """用户全量重建：逐会话幂等索引 + 清理失效条目。"""
    total = IndexStats()
    offset = 0
    while True:
        rows = (await db.execute(
            select(AgentSession.id)
            .where(AgentSession.user_id == int(user_id))
            .order_by(AgentSession.id)
            .offset(offset).limit(batch_size)
        )).scalars().all()
        if not rows:
            break
        for session_id in rows:
            session = await _load_session(db, int(session_id))
            if session is None:
                continue
            stats = await index_session(db, session, prune=True)
            total.units += stats.units
            total.embedded += stats.embedded
            total.degraded += stats.degraded
            total.deleted += stats.deleted
            total.errors.extend(stats.errors)
        await db.flush()
        offset += batch_size
    return total


async def mark_session_deleted(db: AsyncSession, session_id: int) -> int:
    """会话删除第一步：置删除标记（检索立即不可见）。"""
    rows = (await db.execute(
        select(ConversationIndexEntry).where(
            ConversationIndexEntry.session_id == int(session_id),
            ConversationIndexEntry.is_deleted.is_(False),
        )
    )).scalars().all()
    for row in rows:
        row.is_deleted = True
    return len(rows)


async def delete_session_index(db: AsyncSession, session_id: int) -> int:
    """会话删除第二步：删除派生行 + Qdrant points（可重试）。"""
    entries = (await db.execute(
        select(ConversationIndexEntry).where(
            ConversationIndexEntry.session_id == int(session_id)
        )
    )).scalars().all()
    models = {
        (entry.embedding_model or "", int(entry.embedding_dimension or 0))
        for entry in entries if entry.embedding_dimension
    }
    await db.execute(
        delete(ConversationIndexEntry).where(
            ConversationIndexEntry.session_id == int(session_id)
        )
    )
    from qdrant_client.http import models as qm

    store = get_vector_store()
    flt = qm.Filter(must=[qm.FieldCondition(
        key="session_id", match=qm.MatchValue(value=int(session_id)),
    )])
    for model, dimension in models:
        collection = memory_collection_name(model, dimension)
        try:
            await store.delete_points(collection, flt)
        except Exception as exc:  # noqa: BLE001 删除失败上抛由 Job 重试
            raise RuntimeError(f"Qdrant 会话索引清理失败：{exc}") from exc
    return len(entries)


async def purge_stale_entries(db: AsyncSession, *, days: int = 30) -> int:
    """一致性扫描辅助：清理长时间处于 deleted 的残留条目（Qdrant 清理兜底）。"""
    cutoff = datetime.utcnow() - timedelta(days=max(1, days))
    rows = (await db.execute(
        select(ConversationIndexEntry).where(
            ConversationIndexEntry.is_deleted.is_(True),
            ConversationIndexEntry.updated_at < cutoff,
        )
    )).scalars().all()
    for row in rows:
        await db.delete(row)
    return len(rows)
