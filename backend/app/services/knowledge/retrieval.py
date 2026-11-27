"""Hybrid Retrieval（任务书 §7 / §10.3）。

Query -> Dense(Qdrant) + BM25(MySQL Chunk 表) -> RRF -> 去重
      -> 可选 Reranker -> Parent/Neighbor 上下文扩展 -> 引用
"""
from __future__ import annotations

import asyncio
import logging
import math
import re
import time
import uuid
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.knowledge import (
    KnowledgeBase,
    KnowledgeChunk,
    KnowledgeDocument,
    KnowledgeIndexGeneration,
)

from .embedding import EmbeddingError, embed_query, get_reranker
from .model import CHUNKER_VERSION, PIPELINE_VERSION
from .tokens import count_tokens
from .vector_store import build_filter, get_vector_store

logger = logging.getLogger(__name__)

_BM25_K1 = 1.5
_BM25_B = 0.75
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")
_TOKEN_RE = re.compile(r"[\u4e00-\u9fff]|[a-z0-9]+")


def tokenize(text: str) -> List[str]:
    lowered = (text or "").lower()
    tokens = _TOKEN_RE.findall(lowered)
    chars = _CJK_RE.findall(lowered)
    bigrams = [chars[i] + chars[i + 1] for i in range(len(chars) - 1)]
    return tokens + bigrams


@dataclass
class RetrievalOptions:
    top_k: int = 5
    mode: str = ""
    rerank: Optional[bool] = None
    expand_context: bool = True
    context_token_budget: int = 0
    dense_top_k: int = 0
    bm25_top_k: int = 0
    score_threshold: Optional[float] = None


@dataclass
class RetrievedItem:
    chunk_id: str
    content: str
    context: str
    score: float
    dense_score: Optional[float]
    bm25_score: Optional[float]
    rrf_score: float
    rerank_score: Optional[float]
    citation: Dict[str, Any]
    chunk: Dict[str, Any] = field(default_factory=dict)


@dataclass
class RetrievalResult:
    retrieval_id: str
    query: str
    mode: str
    items: List[RetrievedItem]
    timings: Dict[str, float]
    candidates: Dict[str, int]
    reranked: bool = False
    legacy_used: bool = False
    notes: List[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.items)

    def tool_payload(self) -> Dict[str, Any]:
        """Agent Tool / API 通用结构化结果（任务书 §10.3）。"""
        return {
            "status": "succeeded",
            "query": self.query,
            "results": [
                {
                    "content": item.context,
                    "matched_content": item.content,
                    "score": round(item.score, 6),
                    "signals": {
                        "dense": None if item.dense_score is None else round(item.dense_score, 6),
                        "bm25": None if item.bm25_score is None else round(item.bm25_score, 6),
                        "rrf": round(item.rrf_score, 6),
                        "rerank": None if item.rerank_score is None else round(item.rerank_score, 6),
                    },
                    "citation": item.citation,
                }
                for item in self.items
            ],
            "total": self.total,
            "retrieval_mode": self.mode,
            "retrieval_id": self.retrieval_id,
            "timings_ms": {k: round(v, 2) for k, v in self.timings.items()},
        }


class KnowledgeRetrievalService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def search(
        self,
        *,
        knowledge_base_ids: Sequence[int],
        query: str,
        options: RetrievalOptions = None,
    ) -> RetrievalResult:
        options = options or RetrievalOptions()
        started = time.monotonic()
        timings: Dict[str, float] = {}
        retrieval_id = f"kr_{uuid.uuid4().hex[:16]}"
        query = (query or "").strip()
        if not query:
            return RetrievalResult(
                retrieval_id=retrieval_id, query="", mode="hybrid", items=[],
                timings={"total": 0.0}, candidates={},
            )
        kb_ids = [int(x) for x in knowledge_base_ids]
        mode = (options.mode or settings.KNOWLEDGE_RETRIEVAL_MODE or "hybrid").lower()
        if mode not in {"dense", "bm25", "hybrid"}:
            mode = "hybrid"
        rerank_enabled = (
            bool(settings.KNOWLEDGE_RERANKER_ENABLED) if options.rerank is None else bool(options.rerank)
        )
        dense_top_k = int(options.dense_top_k or settings.KNOWLEDGE_DENSE_TOP_K)
        bm25_top_k = int(options.bm25_top_k or settings.KNOWLEDGE_BM25_TOP_K)
        top_k = max(1, min(int(options.top_k or 5), 50))

        # 1) 知识库 / Active Generation
        kbs = (await self.db.execute(
            select(KnowledgeBase).where(
                KnowledgeBase.id.in_(kb_ids),
                KnowledgeBase.status == "active",
                KnowledgeBase.is_active.is_(True),
            )
        )).scalars().all()
        active: List[Tuple[KnowledgeBase, KnowledgeIndexGeneration]] = []
        generation_ids: List[int] = []
        for kb in kbs:
            if not kb.active_index_generation_id:
                continue
            generation = (await self.db.execute(
                select(KnowledgeIndexGeneration).where(
                    KnowledgeIndexGeneration.id == kb.active_index_generation_id,
                )
            )).scalar_one_or_none()
            if generation is not None:
                active.append((kb, generation))
                generation_ids.append(int(generation.id))
        notes: List[str] = []
        if not active:
            if settings.KNOWLEDGE_LEGACY_FALLBACK:
                return await self._legacy_fallback(
                    kb_ids, query, top_k, mode, started, retrieval_id, options,
                )
            return RetrievalResult(
                retrieval_id=retrieval_id, query=query, mode=mode, items=[],
                timings={"total": (time.monotonic() - started) * 1000},
                candidates={}, notes=["知识库尚未完成新索引"],
            )
        if len(active) < len(kbs):
            notes.append("部分知识库尚未完成新索引，已跳过")

        dense_hits: List[Tuple[str, float]] = []
        # 2) Dense
        if mode in {"dense", "hybrid"}:
            t0 = time.monotonic()
            try:
                vector = await embed_query(query)
            except EmbeddingError as exc:
                if mode == "dense":
                    raise
                notes.append(f"Dense 检索不可用，已降级为 BM25：{exc}")
                vector = []
            if vector:
                by_collection: Dict[str, List[int]] = {}
                for kb, generation in active:
                    by_collection.setdefault(generation.collection_name, []).append(int(kb.id))
                store = get_vector_store()
                seen: Dict[str, float] = {}
                for collection, kb_ids_in_collection in by_collection.items():
                    flt = build_filter(
                        knowledge_base_ids=kb_ids_in_collection,
                        generation_ids=generation_ids,
                        exclude_parents=True,
                    )
                    try:
                        hits = await store.search(
                            collection, vector, limit=dense_top_k, flt=flt
                        )
                    except RuntimeError as exc:
                        notes.append(f"Dense 检索失败：{exc}")
                        continue
                    for chunk_id, score, _payload in hits:
                        seen[chunk_id] = max(seen.get(chunk_id, 0.0), float(score))
                dense_hits = sorted(seen.items(), key=lambda x: x[1], reverse=True)[:dense_top_k]
            timings["dense_ms"] = (time.monotonic() - t0) * 1000

        # 3) BM25
        candidates_meta: Dict[str, Dict[str, Any]] = {}
        bm25_hits: List[Tuple[str, float]] = []
        t0 = time.monotonic()
        if mode in {"bm25", "hybrid"}:
            rows = (await self.db.execute(
                select(
                    KnowledgeChunk.id, KnowledgeChunk.content, KnowledgeChunk.content_hash,
                    KnowledgeChunk.heading_path, KnowledgeChunk.document_id,
                    KnowledgeChunk.document_version_id, KnowledgeChunk.knowledge_base_id,
                    KnowledgeChunk.filename, KnowledgeChunk.file_type, KnowledgeChunk.title,
                    KnowledgeChunk.token_count, KnowledgeChunk.parent_chunk_id,
                    KnowledgeChunk.page_start, KnowledgeChunk.page_end,
                    KnowledgeChunk.sheet_name, KnowledgeChunk.row_start, KnowledgeChunk.row_end,
                    KnowledgeChunk.slide_number, KnowledgeChunk.table_index,
                    KnowledgeChunk.element_type, KnowledgeChunk.source_element_ids,
                    KnowledgeChunk.chunk_index, KnowledgeChunk.index_generation_id,
                ).where(
                    KnowledgeChunk.knowledge_base_id.in_([int(kb.id) for kb in kbs]),
                    KnowledgeChunk.index_generation_id.in_(generation_ids),
                    KnowledgeChunk.is_parent.is_(False),
                )
            )).all()
            query_tokens = tokenize(query)
            docs_tokens = [(row[0], tokenize(row[1])) for row in rows]
            total_docs = len(docs_tokens)
            doc_freq: Dict[str, int] = {}
            for _chunk_id, tokens in docs_tokens:
                for token in set(tokens):
                    doc_freq[token] = doc_freq.get(token, 0) + 1
            avg_len = (sum(len(t) for _i, t in docs_tokens) / max(total_docs, 1)) or 1.0
            # 查询词 IDF 权重：仅命中"的/是"等常见字的噪声在覆盖率判定中被压低
            query_idf: Dict[str, float] = {}
            for token in set(query_tokens):
                df = doc_freq.get(token, 0)
                query_idf[token] = math.log((total_docs - df + 0.5) / (df + 0.5) + 1)
            total_weight = sum(query_idf.values()) or 1.0
            min_coverage = float(settings.KNOWLEDGE_MIN_LEXICAL_COVERAGE or 0.0)
            scored: List[Tuple[float, str]] = []
            for (chunk_id, tokens), row in zip(docs_tokens, rows):
                if not tokens:
                    continue
                tf = Counter(tokens)
                score = 0.0
                matched_weight = 0.0
                matched_strong = False
                for token in query_tokens:
                    freq = tf.get(token)
                    if not freq:
                        continue
                    matched_weight += query_idf.get(token, 0.0)
                    # 强匹配信号：中文双字词 / 拉丁编号（长度 >= 2），
                    # 只命中"的/是"等单字噪声不足以进入候选
                    if len(token) >= 2:
                        matched_strong = True
                    df = doc_freq.get(token, 0)
                    idf = math.log((total_docs - df + 0.5) / (df + 0.5) + 1)
                    norm = freq * (_BM25_K1 + 1) / (
                        freq + _BM25_K1 * (1 - _BM25_B + _BM25_B * len(tokens) / avg_len)
                    )
                    score += idf * norm
                if score <= 0 or not matched_strong:
                    continue
                if min_coverage > 0 and matched_weight / total_weight < min_coverage:
                    continue
                scored.append((score, chunk_id))
                candidates_meta[chunk_id] = {
                    "id": row[0], "content": row[1], "content_hash": row[2],
                    "heading_path": row[3], "document_id": row[4], "document_version_id": row[5],
                    "knowledge_base_id": row[6], "filename": row[7], "file_type": row[8],
                    "title": row[9], "token_count": row[10], "parent_chunk_id": row[11],
                    "page_start": row[12], "page_end": row[13], "sheet_name": row[14],
                    "row_start": row[15], "row_end": row[16], "slide_number": row[17],
                    "table_index": row[18], "element_type": row[19],
                    "source_element_ids": row[20], "chunk_index": row[21],
                    "index_generation_id": row[22],
                }
            scored.sort(key=lambda x: x[0], reverse=True)
            bm25_hits = [(chunk_id, score) for score, chunk_id in scored[:bm25_top_k]]
        timings["bm25_ms"] = (time.monotonic() - t0) * 1000

        # 4) RRF 融合
        t0 = time.monotonic()
        rrf_scores: Dict[str, float] = {}
        dense_map = dict(dense_hits)
        bm25_map = dict(bm25_hits)
        rrf_k = max(1, int(settings.KNOWLEDGE_RRF_K or 60))
        for rank, (chunk_id, _score) in enumerate(dense_hits):
            rrf_scores[chunk_id] = rrf_scores.get(chunk_id, 0.0) + 1.0 / (rrf_k + rank + 1)
        for rank, (chunk_id, _score) in enumerate(bm25_hits):
            rrf_scores[chunk_id] = rrf_scores.get(chunk_id, 0.0) + 1.0 / (rrf_k + rank + 1)
        # Dense-only 命中需要补 DB 元数据（内容/来源）
        missing = [cid for cid in rrf_scores if cid not in candidates_meta]
        if missing:
            rows = (await self.db.execute(
                select(KnowledgeChunk).where(KnowledgeChunk.id.in_(missing))
            )).scalars().all()
            for chunk in rows:
                candidates_meta[chunk.id] = self._row_to_meta(chunk)

        # 低相关性处理：Dense-only 绝对阈值；BM25 命中共有词才进入候选
        min_dense = float(settings.KNOWLEDGE_MIN_DENSE_SCORE or 0.0)
        if options.score_threshold is not None:
            min_dense = float(options.score_threshold)
        filtered: List[Tuple[str, float]] = []
        for chunk_id, score in rrf_scores.items():
            dense_score = dense_map.get(chunk_id)
            bm25_score = bm25_map.get(chunk_id)
            if mode == "dense" and (dense_score is None or dense_score < min_dense):
                continue
            if mode == "hybrid" and dense_score is not None and dense_score < min_dense \
                    and bm25_score is None:
                continue
            filtered.append((chunk_id, score))
        filtered.sort(key=lambda x: x[1], reverse=True)

        # 5) 去重（内容 Hash）+ 截断
        seen_hashes: set = set()
        unique_ids: List[str] = []
        for chunk_id, _score in filtered:
            meta = candidates_meta.get(chunk_id) or {}
            digest = meta.get("content_hash") or chunk_id
            if digest in seen_hashes:
                continue
            seen_hashes.add(digest)
            unique_ids.append(chunk_id)
        candidates_count = len(unique_ids)
        candidate_limit = max(top_k, int(settings.KNOWLEDGE_RERANK_TOP_K or 20)) if rerank_enabled else top_k
        unique_ids = unique_ids[:candidate_limit]

        # 6) Reranker
        rerank_scores: Dict[str, float] = {}
        if rerank_enabled and unique_ids:
            t0 = time.monotonic()
            try:
                documents = [str((candidates_meta.get(cid) or {}).get("content") or "") for cid in unique_ids]
                scores = await get_reranker().rerank(query, documents)
                rerank_scores = dict(zip(unique_ids, scores))
                min_rerank = float(settings.KNOWLEDGE_MIN_RERANK_SCORE or 0.0)
                unique_ids = [cid for cid in unique_ids if rerank_scores.get(cid, 0.0) >= min_rerank]
                unique_ids.sort(key=lambda cid: rerank_scores.get(cid, 0.0), reverse=True)
            except EmbeddingError as exc:
                notes.append(f"Reranker 不可用，保留 RRF 排序：{exc}")
            timings["rerank_ms"] = (time.monotonic() - t0) * 1000
            try:
                from app.observability import metrics

                metrics.observe_knowledge_reranker(timings["rerank_ms"] / 1000.0)
            except Exception:  # noqa: BLE001
                pass

        unique_ids = unique_ids[:top_k]

        # 7) 上下文扩展 + 引用
        t0 = time.monotonic()
        budget = int(options.context_token_budget or settings.KNOWLEDGE_CONTEXT_TOKEN_BUDGET or 2400)
        items = await self._assemble(
            unique_ids, candidates_meta, dense_map, bm25_map, rrf_scores, rerank_scores,
            budget=budget, expand=options.expand_context,
        )
        timings["assemble_ms"] = (time.monotonic() - t0) * 1000
        timings["total_ms"] = (time.monotonic() - started) * 1000
        try:
            from app.observability import metrics

            metrics.observe_knowledge_retrieval(
                mode, timings["total_ms"] / 1000.0,
                candidates=candidates_count, empty=not items,
            )
        except Exception:  # noqa: BLE001
            pass
        return RetrievalResult(
            retrieval_id=retrieval_id, query=query, mode=mode, items=items,
            timings=timings,
            candidates={"dense": len(dense_hits), "bm25": len(bm25_hits),
                        "unique": candidates_count, "returned": len(items)},
            reranked=bool(rerank_scores), legacy_used=False, notes=notes,
        )

    # ── 组装（含 Parent / 邻居扩展）──────────────────────────────────────

    async def _assemble(
        self,
        chunk_ids: List[str],
        meta: Dict[str, Dict[str, Any]],
        dense_map: Dict[str, float],
        bm25_map: Dict[str, float],
        rrf_scores: Dict[str, float],
        rerank_scores: Dict[str, float],
        *,
        budget: int,
        expand: bool,
    ) -> List[RetrievedItem]:
        if not chunk_ids:
            return []
        kb_ids = {int((meta.get(cid) or {}).get("knowledge_base_id") or 0) for cid in chunk_ids}
        kb_names: Dict[int, str] = {}
        if kb_ids:
            rows = (await self.db.execute(
                select(KnowledgeBase.id, KnowledgeBase.name).where(KnowledgeBase.id.in_(kb_ids))
            )).all()
            kb_names = {int(row[0]): str(row[1]) for row in rows}
        doc_ids = {int((meta.get(cid) or {}).get("document_id") or 0) for cid in chunk_ids}
        source_types: Dict[int, str] = {}
        if doc_ids:
            rows = (await self.db.execute(
                select(KnowledgeDocument.id, KnowledgeDocument.file_type).where(
                    KnowledgeDocument.id.in_(doc_ids)
                )
            )).all()
            source_types = {int(row[0]): str(row[1] or "") for row in rows}

        items: List[RetrievedItem] = []
        remaining = max(200, budget)
        for chunk_id in chunk_ids:
            chunk = meta.get(chunk_id) or {}
            content = str(chunk.get("content") or "")
            if not content:
                continue
            expanded = content
            expanded_tokens = count_tokens(content)
            if expand and remaining > expanded_tokens:
                extra_parts: List[str] = []
                parent_added = False
                parent_id = chunk.get("parent_chunk_id")
                if parent_id:
                    parent = await self.db.get(KnowledgeChunk, str(parent_id))
                    if parent is not None and parent.content and parent.content not in content:
                        parent_tokens = count_tokens(parent.content)
                        if remaining >= parent_tokens:
                            extra_parts.append(parent.content)
                            remaining -= parent_tokens
                            parent_added = True
                # 已有 Parent 时不再拼接相邻 Child / 原文（Parent 已包含，避免重复占用预算）
                if remaining > 0 and not parent_added:
                    neighbors = (await self.db.execute(
                        select(KnowledgeChunk.chunk_index, KnowledgeChunk.content,
                               KnowledgeChunk.token_count)
                        .where(
                            KnowledgeChunk.document_id == int(chunk.get("document_id") or 0),
                            KnowledgeChunk.index_generation_id == int(chunk.get("index_generation_id") or 0),
                            KnowledgeChunk.chunk_index.in_([
                                int(chunk.get("chunk_index") or 0) - 1,
                                int(chunk.get("chunk_index") or 0) + 1,
                            ]),
                            KnowledgeChunk.is_parent.is_(False),
                        )
                        .order_by(KnowledgeChunk.chunk_index)
                    )).all()
                    for _idx, neighbor_text, neighbor_tokens in neighbors:
                        if int(neighbor_tokens or 0) <= remaining:
                            extra_parts.append(str(neighbor_text))
                            remaining -= int(neighbor_tokens or 0)
                if extra_parts:
                    if parent_added:
                        expanded = "\n\n".join(extra_parts)
                    else:
                        expanded = "\n\n".join(extra_parts + [content])
                    expanded_tokens = count_tokens(expanded)
            remaining -= expanded_tokens
            rank_score = rerank_scores.get(chunk_id, rrf_scores.get(chunk_id, 0.0))
            citation = {
                "knowledge_base_id": int(chunk.get("knowledge_base_id") or 0),
                "knowledge_base_name": kb_names.get(int(chunk.get("knowledge_base_id") or 0), ""),
                "document_id": int(chunk.get("document_id") or 0),
                "document_version_id": int(chunk.get("document_version_id") or 0),
                "filename": str(chunk.get("filename") or ""),
                "title": str(chunk.get("title") or ""),
                "source_type": source_types.get(int(chunk.get("document_id") or 0), ""),
                "page_start": chunk.get("page_start"),
                "page_end": chunk.get("page_end"),
                "heading_path": list(chunk.get("heading_path") or []),
                "sheet_name": chunk.get("sheet_name"),
                "row_start": chunk.get("row_start"),
                "row_end": chunk.get("row_end"),
                "slide_number": chunk.get("slide_number"),
                "table_index": chunk.get("table_index"),
                "chunk_id": chunk_id,
            }
            items.append(RetrievedItem(
                chunk_id=chunk_id,
                content=content,
                context=expanded,
                score=float(rank_score),
                dense_score=dense_map.get(chunk_id),
                bm25_score=bm25_map.get(chunk_id),
                rrf_score=float(rrf_scores.get(chunk_id, 0.0)),
                rerank_score=rerank_scores.get(chunk_id),
                citation=citation,
                chunk=chunk,
            ))
        return items

    @staticmethod
    def _row_to_meta(chunk: KnowledgeChunk) -> Dict[str, Any]:
        return {
            "id": chunk.id, "content": chunk.content, "content_hash": chunk.content_hash,
            "heading_path": chunk.heading_path, "document_id": chunk.document_id,
            "document_version_id": chunk.document_version_id,
            "knowledge_base_id": chunk.knowledge_base_id, "filename": chunk.filename,
            "file_type": chunk.file_type, "title": chunk.title,
            "token_count": chunk.token_count, "parent_chunk_id": chunk.parent_chunk_id,
            "page_start": chunk.page_start, "page_end": chunk.page_end,
            "sheet_name": chunk.sheet_name, "row_start": chunk.row_start,
            "row_end": chunk.row_end, "slide_number": chunk.slide_number,
            "table_index": chunk.table_index, "element_type": chunk.element_type,
            "source_element_ids": chunk.source_element_ids, "chunk_index": chunk.chunk_index,
            "index_generation_id": chunk.index_generation_id,
        }

    async def _legacy_fallback(
        self, kb_ids: List[int], query: str, top_k: int, mode: str,
        started: float, retrieval_id: str, options: RetrievalOptions,
    ) -> RetrievalResult:
        """新索引尚未建立时的短期回滚链路（旧 SQLite 词法检索）。"""
        from app.services.knowledge_service import KnowledgeService

        items: List[RetrievedItem] = []
        for kb_id in kb_ids:
            kb = await self.db.get(KnowledgeBase, kb_id)
            if kb is None or not kb.collection_name:
                continue
            try:
                rows = await KnowledgeService.search(
                    collection_name=kb.collection_name, query=query, top_k=top_k,
                    score_threshold=float(options.score_threshold or 0.0),
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("旧检索回退失败 kb=%s: %s", kb_id, exc)
                continue
            for row in rows:
                items.append(RetrievedItem(
                    chunk_id=f"legacy_{kb_id}_{row.get('document_id')}_{row.get('chunk_index')}",
                    content=str(row.get("content") or ""),
                    context=str(row.get("content") or ""),
                    score=float(row.get("score") or 0.0),
                    dense_score=None,
                    bm25_score=float((row.get("metadata") or {}).get("bm25_score") or 0.0),
                    rrf_score=0.0,
                    rerank_score=None,
                    citation={
                        "knowledge_base_id": kb_id, "knowledge_base_name": kb.name,
                        "document_id": row.get("document_id"),
                        "document_version_id": 0,
                        "filename": row.get("filename") or "",
                        "page_start": None, "page_end": None,
                        "heading_path": [], "chunk_id": f"legacy_{kb_id}_{row.get('chunk_index')}",
                        "legacy": True,
                    },
                    chunk={"legacy": True},
                ))
        items.sort(key=lambda item: item.score, reverse=True)
        items = items[:top_k]
        timings = {"total_ms": (time.monotonic() - started) * 1000}
        return RetrievalResult(
            retrieval_id=retrieval_id, query=query, mode="legacy", items=items,
            timings=timings, candidates={"dense": 0, "bm25": len(items), "unique": len(items),
                                         "returned": len(items)},
            legacy_used=True, notes=["知识库尚未完成新索引，使用旧词法检索回退"],
        )

    async def expand_query_terms(self, query: str) -> List[str]:
        """保留接口：查询扩展（同义词/改写）由上层决定，不在 Retrieval 内隐式调用 LLM。"""
        return [query]
