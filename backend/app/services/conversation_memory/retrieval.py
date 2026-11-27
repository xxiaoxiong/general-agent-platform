"""历史会话混合检索：Dense(Qdrant) + BM25(索引表) -> RRF -> 回查 ACL -> 预算。

- 权限由服务端 SQL / Qdrant Filter 强制执行；``scope_hint`` 不是授权；
- 结果必须回查原始会话与消息仍存在、仍可读，删除/撤销立即不可见；
- 向量或 Reranker 故障时降级为全文检索，不抛到 Run 主链路。
"""
from __future__ import annotations

import logging
import math
import re
import time
import uuid
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Sequence, Tuple

from qdrant_client.http import models as qm
from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.agent import AgentSession
from app.models.conversation_memory import (
    ConversationIndexEntry, ConversationMemoryShare,
)
from app.services.knowledge.embedding import EmbeddingError, embed_query, get_embedding_provider, get_reranker
from app.services.knowledge.retrieval import tokenize
from app.services.knowledge.tokens import count_tokens
from app.services.knowledge.vector_store import get_vector_store

from .indexer import current_index_version, memory_collection_name
from .repository import accessible_project_ids, load_settings

logger = logging.getLogger("ai_plat.conversation_memory")

_BM25_K1 = 1.5
_BM25_B = 0.75
_SOURCE_PREFIX = "history_src_"


@dataclass
class HistorySource:
    source_id: str
    conversation_id: int
    message_ids: List[str]
    conversation_title: str
    created_at: Optional[str]
    snippet: str
    relevance_score: float
    scope: str
    entry_type: str
    role: str
    project_id: Optional[int] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "source_id": self.source_id,
            "conversation_id": self.conversation_id,
            "message_ids": list(self.message_ids),
            "conversation_title": self.conversation_title,
            "created_at": self.created_at,
            "snippet": self.snippet,
            "relevance_score": round(float(self.relevance_score), 6),
            "scope": self.scope,
            "entry_type": self.entry_type,
            "role": self.role,
            "project_id": self.project_id,
        }


@dataclass
class HistoryItem:
    entry_id: int
    session_id: int
    user_id: int
    project_id: Optional[int]
    entry_type: str
    role: str
    message_ids: List[str]
    content: str
    created_at: Optional[datetime]
    score: float
    conversation_title: str = ""
    dense_score: Optional[float] = None
    lexical_score: Optional[float] = None
    rrf_score: float = 0.0
    rerank_score: Optional[float] = None
    scope: str = "user"


@dataclass
class HistoryRetrievalResult:
    retrieval_id: str
    query: str
    scope: str
    items: List[HistoryItem] = field(default_factory=list)
    timings: Dict[str, float] = field(default_factory=dict)
    candidates: Dict[str, int] = field(default_factory=dict)
    notes: List[str] = field(default_factory=list)
    reranked: bool = False

    def sources(self, *, snippet_chars: int = 320) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for item in self.items:
            snippet = re.sub(r"\s+", " ", item.content).strip()
            out.append(HistorySource(
                source_id=f"{_SOURCE_PREFIX}{item.entry_id}",
                conversation_id=item.session_id,
                message_ids=list(item.message_ids),
                conversation_title=item.conversation_title or "未命名会话",
                created_at=item.created_at.isoformat() if item.created_at else None,
                snippet=snippet[:snippet_chars],
                relevance_score=item.score,
                scope=item.scope,
                entry_type=item.entry_type,
                role=item.role,
                project_id=item.project_id,
            ).to_dict())
        return out


def _iso(value: Optional[datetime]) -> Optional[str]:
    return value.isoformat() if value else None


class ConversationMemoryService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    # ── 检索入口 ──────────────────────────────────────────────────────────

    async def search(
        self,
        *,
        user_id: int,
        query: str,
        top_k: Optional[int] = None,
        project_id: Optional[int] = None,
        scope: str = "auto",
        mode: str = "hybrid",
    ) -> HistoryRetrievalResult:
        started = time.monotonic()
        retrieval_id = f"hm_{uuid.uuid4().hex[:16]}"
        query = (query or "").strip()
        top_k = max(1, min(int(top_k or settings.CONVERSATION_MEMORY_TOP_K or 5), 20))
        scope = scope if scope in {"auto", "current_project", "all"} else "auto"
        timings: Dict[str, float] = {}
        notes: List[str] = []
        if not query:
            return HistoryRetrievalResult(
                retrieval_id=retrieval_id, query="", scope=scope,
                timings={"total_ms": 0.0}, notes=["空查询"],
            )
        user_settings = await load_settings(self.db, int(user_id))
        if not bool(user_settings.get("history_enabled", True)):
            return HistoryRetrievalResult(
                retrieval_id=retrieval_id, query=query, scope=scope,
                timings={"total_ms": (time.monotonic() - started) * 1000},
                notes=["用户已关闭历史检索"],
            )
        project_ids = (
            await accessible_project_ids(self.db, int(user_id))
            if bool(user_settings.get("project_memory_enabled", True)) else []
        )
        if project_id is not None and int(project_id) not in project_ids:
            project_id = None
        if scope == "current_project" and project_id is None:
            return HistoryRetrievalResult(
                retrieval_id=retrieval_id, query=query, scope=scope,
                timings={"total_ms": (time.monotonic() - started) * 1000},
                notes=["当前会话未绑定项目，无法限定项目历史"],
            )

        index_version = current_index_version()
        lexical_rows = await self._lexical_rows(
            user_id=int(user_id), project_ids=project_ids, project_id=project_id,
            scope=scope, index_version=index_version,
        )
        t0 = time.monotonic()
        lexical_hits, lexical_meta = self._bm25(query, lexical_rows)
        timings["lexical_ms"] = (time.monotonic() - t0) * 1000

        dense_hits: List[Tuple[int, float]] = []
        t0 = time.monotonic()
        if mode in {"dense", "hybrid"}:
            dense_hits = await self._dense_search(
                query=query, user_id=int(user_id), project_ids=project_ids,
                project_id=project_id, scope=scope, index_version=index_version,
                notes=notes,
            )
        timings["dense_ms"] = (time.monotonic() - t0) * 1000

        t0 = time.monotonic()
        fused = self._fuse(dense_hits, lexical_hits, notes=notes)
        timings["fuse_ms"] = (time.monotonic() - t0) * 1000
        if not fused:
            self._record_metrics(
                mode, (time.monotonic() - started), 0, empty_reason="no_candidates",
            )
            return HistoryRetrievalResult(
                retrieval_id=retrieval_id, query=query, scope=scope, timings=timings,
                candidates={"dense": len(dense_hits), "lexical": len(lexical_hits), "unique": 0},
                notes=notes,
            )

        t0 = time.monotonic()
        items = await self._verify_and_assemble(
            fused, user_id=int(user_id), project_ids=project_ids,
            current_project_id=project_id, index_version=index_version,
            dense_scores=dict(dense_hits), lexical_scores=dict(lexical_hits),
            notes=notes,
        )
        timings["verify_ms"] = (time.monotonic() - t0) * 1000

        # 可选 Reranker（复用知识库 CrossEncoder；不可用保留 RRF 排序）
        reranked = False
        if items and bool(getattr(settings, "KNOWLEDGE_RERANKER_ENABLED", False)):
            t0 = time.monotonic()
            try:
                scores = await get_reranker().rerank(query, [item.content for item in items])
                for item, score in zip(items, scores):
                    item.rerank_score = float(score)
                items.sort(key=lambda item: item.rerank_score or 0.0, reverse=True)
                reranked = True
            except EmbeddingError as exc:
                notes.append(f"Reranker 不可用，保留融合排序：{exc}")
            except Exception as exc:  # noqa: BLE001
                notes.append(f"Reranker 异常，保留融合排序：{exc}")
            timings["rerank_ms"] = (time.monotonic() - t0) * 1000

        # 项目匹配度加成（只是排序辅助；不改变 ACL）
        def final_score(item: HistoryItem) -> float:
            base = item.rerank_score if reranked and item.rerank_score is not None else item.rrf_score
            boost = 0.01 if project_id and item.project_id == int(project_id) else 0.0
            return float(base) + boost

        items.sort(
            key=lambda item: (
                final_score(item),
                item.created_at.timestamp() if item.created_at else 0.0,
            ),
            reverse=True,
        )
        # 内容哈希去重（多查询/多级索引命中同一内容）
        seen_hashes: set = set()
        unique: List[HistoryItem] = []
        for item in items:
            digest = (item.content or "")[:2000]
            if digest in seen_hashes:
                continue
            seen_hashes.add(digest)
            item.score = final_score(item)
            unique.append(item)
        items = unique[:top_k]
        timings["total_ms"] = (time.monotonic() - started) * 1000
        self._record_metrics(
            mode, (time.monotonic() - started), len(items),
            empty_reason="" if items else "no_result",
        )
        return HistoryRetrievalResult(
            retrieval_id=retrieval_id, query=query, scope=scope, items=items,
            timings=timings,
            candidates={
                "dense": len(dense_hits), "lexical": len(lexical_hits),
                "unique": len(unique), "returned": len(items),
            },
            notes=notes, reranked=reranked,
        )

    async def get_source(
        self, *, user_id: int, source_id: str,
    ) -> Optional[Dict[str, Any]]:
        """按来源 ID 取原文片段；每次重新校验会话存在与权限。"""
        raw = str(source_id or "").strip()
        if raw.startswith(_SOURCE_PREFIX):
            raw = raw[len(_SOURCE_PREFIX):]
        try:
            entry_id = int(raw)
        except (TypeError, ValueError):
            return None
        entry = await self.db.get(ConversationIndexEntry, entry_id)
        if entry is None or entry.is_deleted or entry.is_excluded:
            return None
        user_settings = await load_settings(self.db, int(user_id))
        if not bool(user_settings.get("history_enabled", True)):
            return None
        project_ids = (
            await accessible_project_ids(self.db, int(user_id))
            if bool(user_settings.get("project_memory_enabled", True)) else []
        )
        if entry.visibility_scope == "project":
            if not entry.project_id or int(entry.project_id) not in project_ids:
                return None
            share = (await self.db.execute(
                select(ConversationMemoryShare).where(
                    ConversationMemoryShare.conversation_id == int(entry.session_id),
                    ConversationMemoryShare.revoked_at.is_(None),
                )
            )).scalar_one_or_none()
            if share is None:
                return None
        elif int(entry.user_id) != int(user_id):
            return None
        session = (await self.db.execute(
            select(AgentSession).where(AgentSession.id == int(entry.session_id))
        )).scalar_one_or_none()
        if session is None or not self._messages_exist(session, entry.message_ids):
            return None
        if entry.visibility_scope != "project" and int(session.user_id or 0) != int(user_id):
            return None
        return {
            "source_id": f"{_SOURCE_PREFIX}{entry.id}",
            "conversation_id": int(entry.session_id),
            "conversation_title": session.title or "未命名会话",
            "message_ids": list(entry.message_ids or []),
            "created_at": _iso(entry.message_created_at),
            "content": entry.content,
            "scope": entry.visibility_scope,
            "entry_type": entry.entry_type,
        }

    # ── 内部：候选与融合 ──────────────────────────────────────────────────

    def _base_conditions(
        self, *, user_id: int, project_ids: Sequence[int], index_version: str,
    ) -> List[Any]:
        lookback = int(getattr(settings, "CONVERSATION_MEMORY_LOOKBACK_DAYS", 730) or 730)
        cutoff = datetime.utcnow() - timedelta(days=max(1, lookback))
        scope_cond = ConversationIndexEntry.user_id == int(user_id)
        if project_ids:
            scope_cond = or_(
                scope_cond,
                and_(
                    ConversationIndexEntry.visibility_scope == "project",
                    ConversationIndexEntry.project_id.in_([int(x) for x in project_ids]),
                ),
            )
        return [
            ConversationIndexEntry.is_deleted.is_(False),
            ConversationIndexEntry.is_excluded.is_(False),
            ConversationIndexEntry.index_version == index_version,
            func.coalesce(
                ConversationIndexEntry.message_created_at, ConversationIndexEntry.created_at,
            ) >= cutoff,
            scope_cond,
        ]

    async def _lexical_rows(
        self, *, user_id: int, project_ids: Sequence[int], project_id: Optional[int],
        scope: str, index_version: str,
    ) -> List[Any]:
        conditions = self._base_conditions(
            user_id=user_id, project_ids=project_ids, index_version=index_version,
        )
        if scope == "current_project" and project_id is not None:
            conditions.append(ConversationIndexEntry.project_id == int(project_id))
        limit = max(100, int(getattr(settings, "CONVERSATION_MEMORY_MAX_SCAN_ENTRIES", 4000) or 4000))
        rows = (await self.db.execute(
            select(
                ConversationIndexEntry.id, ConversationIndexEntry.session_id,
                ConversationIndexEntry.user_id, ConversationIndexEntry.project_id,
                ConversationIndexEntry.entry_type, ConversationIndexEntry.role,
                ConversationIndexEntry.message_ids, ConversationIndexEntry.content,
                ConversationIndexEntry.content_hash, ConversationIndexEntry.message_created_at,
                ConversationIndexEntry.visibility_scope,
            )
            .where(*conditions)
            .order_by(ConversationIndexEntry.message_created_at.desc())
            .limit(limit)
        )).all()
        return list(rows)

    def _bm25(
        self, query: str, rows: Sequence[Any],
    ) -> Tuple[List[Tuple[int, float]], Dict[int, Dict[str, Any]]]:
        if not rows:
            return [], {}
        query_tokens = tokenize(query)
        if not query_tokens:
            return [], {}
        docs = [(int(row[0]), tokenize(row[7])) for row in rows]
        total_docs = len(docs)
        doc_freq: Dict[str, int] = {}
        for _entry_id, tokens in docs:
            for token in set(tokens):
                doc_freq[token] = doc_freq.get(token, 0) + 1
        avg_len = (sum(len(tokens) for _id, tokens in docs) / max(total_docs, 1)) or 1.0
        query_idf: Dict[str, float] = {}
        for token in set(query_tokens):
            df = doc_freq.get(token, 0)
            query_idf[token] = math.log((total_docs - df + 0.5) / (df + 0.5) + 1)
        total_weight = sum(query_idf.values()) or 1.0
        min_coverage = float(getattr(settings, "KNOWLEDGE_MIN_LEXICAL_COVERAGE", 0.2) or 0.0)
        scored: List[Tuple[float, int]] = []
        meta: Dict[int, Dict[str, Any]] = {}
        for (entry_id, tokens), row in zip(docs, rows):
            if not tokens:
                continue
            tf = Counter(tokens)
            score = 0.0
            matched_weight = 0.0
            strong = False
            for token in query_tokens:
                freq = tf.get(token)
                if not freq:
                    continue
                matched_weight += query_idf.get(token, 0.0)
                if len(token) >= 2:
                    strong = True
                df = doc_freq.get(token, 0)
                idf = math.log((total_docs - df + 0.5) / (df + 0.5) + 1)
                norm = freq * (_BM25_K1 + 1) / (
                    freq + _BM25_K1 * (1 - _BM25_B + _BM25_B * len(tokens) / avg_len)
                )
                score += idf * norm
            if score <= 0 or not strong:
                continue
            if min_coverage > 0 and matched_weight / total_weight < min_coverage:
                continue
            scored.append((score, entry_id))
            meta[entry_id] = {
                "id": int(row[0]), "session_id": int(row[1]), "user_id": int(row[2]),
                "project_id": row[3], "entry_type": str(row[4]), "role": str(row[5]),
                "message_ids": list(row[6] or []), "content": str(row[7] or ""),
                "content_hash": str(row[8] or ""), "created_at": row[9],
                "visibility_scope": str(row[10] or "user"),
            }
        scored.sort(key=lambda item: item[0], reverse=True)
        limit = max(1, int(getattr(settings, "CONVERSATION_MEMORY_LEXICAL_TOP_K", 24) or 24))
        hits = [(entry_id, score) for score, entry_id in scored[:limit]]
        return hits, {entry_id: meta[entry_id] for entry_id, _ in hits}

    async def _dense_search(
        self, *, query: str, user_id: int, project_ids: Sequence[int],
        project_id: Optional[int], scope: str, index_version: str,
        notes: List[str],
    ) -> List[Tuple[int, float]]:
        try:
            vector = await embed_query(query)
        except EmbeddingError as exc:
            notes.append(f"Dense 检索不可用，已降级为全文检索：{exc}")
            return []
        except Exception as exc:  # noqa: BLE001
            notes.append(f"Dense 检索异常，已降级为全文检索：{exc}")
            return []
        if not vector:
            return []
        provider = get_embedding_provider()
        dimension = int(getattr(provider, "dimension", 0) or 0) or len(vector)
        collection = memory_collection_name(provider.model, dimension)
        base_must = [
            qm.FieldCondition(key="is_deleted", match=qm.MatchValue(value=False)),
            qm.FieldCondition(key="is_excluded", match=qm.MatchValue(value=False)),
            qm.FieldCondition(key="index_version", match=qm.MatchValue(value=index_version)),
        ]
        if scope == "current_project" and project_id is not None:
            base_must.append(qm.FieldCondition(
                key="project_id", match=qm.MatchValue(value=int(project_id)),
            ))
        filters: List[qm.Filter] = [qm.Filter(must=[
            *base_must,
            qm.FieldCondition(key="user_id", match=qm.MatchValue(value=int(user_id))),
        ])]
        if project_ids:
            shared = qm.Filter(must=[
                *base_must,
                qm.FieldCondition(key="visibility_scope", match=qm.MatchValue(value="project")),
                qm.FieldCondition(
                    key="project_id",
                    match=qm.MatchAny(any=[int(x) for x in project_ids]),
                ),
            ])
            filters.append(shared)
        limit = max(1, int(getattr(settings, "CONVERSATION_MEMORY_DENSE_TOP_K", 24) or 24))
        store = get_vector_store()
        merged: Dict[int, float] = {}
        for flt in filters:
            try:
                hits = await store.search(collection, vector, limit=limit, flt=flt)
            except Exception as exc:  # noqa: BLE001 Collection 未创建/不可用
                notes.append(f"Dense 检索失败，已降级为全文检索：{exc}")
                continue
            for chunk_id, score, payload in hits:
                entry_id = payload.get("entry_id")
                if entry_id is None:
                    try:
                        entry_id = int(str(chunk_id))
                    except (TypeError, ValueError):
                        continue
                merged[int(entry_id)] = max(merged.get(int(entry_id), 0.0), float(score))
        min_dense = float(getattr(settings, "CONVERSATION_MEMORY_MIN_DENSE_SCORE", 0.35) or 0.0)
        return sorted(
            ((entry_id, score) for entry_id, score in merged.items() if score >= min_dense),
            key=lambda item: item[1], reverse=True,
        )[:limit]

    def _fuse(
        self, dense_hits: List[Tuple[int, float]], lexical_hits: List[Tuple[int, float]],
        *, notes: List[str],
    ) -> List[Tuple[int, float]]:
        """RRF 融合前先做 Dense-only 低相关拒答（绝对下限 + 强相关 + 相对优势）。

        短会话的无关语义相似度普遍偏高，单一绝对阈值无法区分“相关但表述不同”
        与“无关但同属技术语料”。规则：
        - 命中 BM25 的候选直接保留；
        - Dense-only 候选：>= 强相关阈值直接保留；否则仅保留领先第二名
          至少 margin 的最高候选（且不低于绝对下限）。
        """
        lexical_ids = {entry_id for entry_id, _score in lexical_hits}
        floor = float(getattr(settings, "CONVERSATION_MEMORY_MIN_DENSE_SCORE", 0.5) or 0.0)
        strong = float(
            getattr(settings, "CONVERSATION_MEMORY_DENSE_STRONG_SCORE", 0.62) or 0.0
        )
        margin = float(getattr(settings, "CONVERSATION_MEMORY_DENSE_MARGIN", 0.04) or 0.0)
        dense_only = [(entry_id, score) for entry_id, score in dense_hits
                      if entry_id not in lexical_ids]
        accepted: set = set()
        if dense_only:
            second = dense_only[1][1] if len(dense_only) > 1 else 0.0
            for rank, (entry_id, score) in enumerate(dense_only):
                if score >= strong:
                    accepted.add(entry_id)
                elif rank == 0 and score >= floor and (score - second) >= margin:
                    accepted.add(entry_id)
            if not accepted:
                notes.append("Dense 候选未达到相关性门槛，已拒答")
        kept = [
            (entry_id, score) for entry_id, score in dense_hits
            if entry_id in lexical_ids or entry_id in accepted
        ]
        if len(kept) < len(dense_hits):
            notes.append(f"低相关 Dense 候选已过滤 {len(dense_hits) - len(kept)} 条")
        rrf_k = max(1, int(getattr(settings, "KNOWLEDGE_RRF_K", 60) or 60))
        scores: Dict[int, float] = {}
        for rank, (entry_id, _score) in enumerate(kept):
            scores[entry_id] = scores.get(entry_id, 0.0) + 1.0 / (rrf_k + rank + 1)
        for rank, (entry_id, _score) in enumerate(lexical_hits):
            scores[entry_id] = scores.get(entry_id, 0.0) + 1.0 / (rrf_k + rank + 1)
        return sorted(scores.items(), key=lambda item: item[1], reverse=True)

    async def _verify_and_assemble(
        self, fused: List[Tuple[int, float]], *, user_id: int,
        project_ids: Sequence[int], current_project_id: Optional[int],
        index_version: str, dense_scores: Dict[int, float],
        lexical_scores: Dict[int, float], notes: List[str],
    ) -> List[HistoryItem]:
        """回查数据库：ACL、会话存在、消息仍存在、共享仍有效。"""
        candidate_ids = [entry_id for entry_id, _ in fused[:80]]
        conditions = self._base_conditions(
            user_id=user_id, project_ids=project_ids, index_version=index_version,
        )
        rows = (await self.db.execute(
            select(ConversationIndexEntry).where(
                ConversationIndexEntry.id.in_(candidate_ids), *conditions,
            )
        )).scalars().all()
        by_id = {int(row.id): row for row in rows}
        if not by_id:
            return []
        session_ids = {int(row.session_id) for row in rows}
        sessions = {
            int(row.id): row for row in (await self.db.execute(
                select(
                    AgentSession.id, AgentSession.user_id, AgentSession.project_id,
                    AgentSession.title, AgentSession.messages,
                )
                .where(AgentSession.id.in_(session_ids))
            )).all()
        }
        shared_ids: set = set()
        if any(row.visibility_scope == "project" for row in rows):
            shared_ids = {
                int(value) for value in (await self.db.execute(
                    select(ConversationMemoryShare.conversation_id).where(
                        ConversationMemoryShare.conversation_id.in_(session_ids),
                        ConversationMemoryShare.revoked_at.is_(None),
                    )
                )).scalars().all()
            }
        scores = dict(fused)
        items: List[HistoryItem] = []
        invalid: List[int] = []
        for entry_id, _rrf in fused[:80]:
            entry = by_id.get(int(entry_id))
            if entry is None:
                continue
            session = sessions.get(int(entry.session_id))
            if session is None or not self._messages_exist(session, entry.message_ids):
                invalid.append(int(entry.id))
                continue
            session_user_id = int(session.user_id or 0)
            if entry.visibility_scope == "project":
                if int(entry.session_id) not in shared_ids:
                    invalid.append(int(entry.id))
                    continue
                if not entry.project_id or int(entry.project_id) not in project_ids:
                    invalid.append(int(entry.id))
                    continue
                if session_user_id != int(user_id):
                    # 共享历史的会话所有者可以不是当前用户；共享关系已校验
                    pass
            elif session_user_id != int(user_id) or int(entry.user_id) != int(user_id):
                invalid.append(int(entry.id))
                continue
            items.append(HistoryItem(
                entry_id=int(entry.id),
                session_id=int(entry.session_id),
                user_id=int(entry.user_id),
                project_id=int(entry.project_id) if entry.project_id else None,
                entry_type=str(entry.entry_type or "turn"),
                role=str(entry.role or "turn"),
                message_ids=list(entry.message_ids or []),
                content=str(entry.content or ""),
                created_at=entry.message_created_at or entry.created_at,
                score=float(scores.get(int(entry.id), 0.0)),
                conversation_title=str(getattr(session, "title", "") or ""),
                dense_score=dense_scores.get(int(entry.id)),
                lexical_score=lexical_scores.get(int(entry.id)),
                rrf_score=float(scores.get(int(entry.id), 0.0)),
                scope=str(entry.visibility_scope or "user"),
            ))
        if invalid:
            # 失效条目置删除标记（后台清理；不阻断本次查询）
            try:
                async with self.db.begin_nested():
                    await self.db.execute(
                        update(ConversationIndexEntry)
                        .where(ConversationIndexEntry.id.in_(invalid))
                        .values(is_deleted=True, updated_at=datetime.utcnow()),
                        execution_options={"synchronize_session": False},
                    )
                notes.append(f"已跳过 {len(invalid)} 条失效历史引用")
                try:
                    from app.observability import metrics

                    metrics.inc_conversation_blocked_sources(len(invalid))
                except Exception:  # noqa: BLE001
                    pass
            except Exception:  # noqa: BLE001 标记失败不影响本次结果
                logger.warning("失效历史条目标记失败（忽略）", exc_info=True)
        if current_project_id is not None and not any(
            item.project_id == int(current_project_id) for item in items
        ):
            notes.append("当前项目暂无匹配历史，已扩展到个人历史")
        return items

    @staticmethod
    def _record_metrics(mode: str, seconds: float, results: int, *, empty_reason: str = "") -> None:
        try:
            from app.observability import metrics

            metrics.observe_conversation_retrieval(
                mode, seconds, results, empty_reason=empty_reason,
            )
        except Exception:  # noqa: BLE001
            pass

    @staticmethod
    def _messages_exist(session: Any, message_ids: Sequence[str]) -> bool:
        if not message_ids:
            return True
        if isinstance(session, dict):
            messages = session.get("messages") or []
        else:
            messages = getattr(session, "messages", None) or []
        existing = {
            str(item.get("message_id"))
            for item in messages if isinstance(item, dict) and item.get("message_id")
        }
        return all(str(mid) in existing for mid in message_ids)


def snippet_budget_chars(tokens: int) -> int:
    """粗略 Token→字符估算（中文约 1 token ≈ 1.5 字，保守取 1.2）。"""
    return max(80, int(tokens * 1.2))


def count_text_tokens(text: str) -> int:
    return count_tokens(text)
