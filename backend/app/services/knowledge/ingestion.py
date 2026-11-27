"""Durable Ingestion：Job 队列 + Pipeline + Generation 原子切换 + 删除一致性。

数据库是任务权威来源（Redis 仅可做唤醒），Worker 通过 CAS 领取 Job 并持有
Lease；进程重启后过期 Lease 由其他 Worker 接管。幂等性：
- 同一文档版本 + Pipeline 版本 + Embedding 模型/Chunker 版本只产生一个 Job；
- Chunk ID 稳定，重跑先清理同代际旧数据再写入；
- Qdrant 使用稳定 Point ID（Upsert 幂等）。
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import time
import uuid
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from sqlalchemy import delete, func, insert, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import AsyncSessionLocal
from app.models.knowledge import (
    KnowledgeBase,
    KnowledgeChunk,
    KnowledgeDocument,
    KnowledgeDocumentVersion,
    KnowledgeIndexGeneration,
    KnowledgeIngestionJob,
)

from . import storage
from .chunking import CHUNKER_VERSION, ChunkingConfig, StructureAwareChunker
from .embedding import EmbeddingError, get_embedding_provider, embedding_signature
from .model import PIPELINE_VERSION, ParseError, ParsedDocument
from .parsers import get_parser
from .parsers.base import ParseOptions
from .vector_store import build_filter, collection_name_for, get_vector_store

logger = logging.getLogger(__name__)

_GENERATION_STATUSES_ACTIVE = {"active"}


def utcnow() -> datetime:
    return datetime.utcnow()


def _idem_key(kb_id: int, doc_id: int, version_id: int, signature: dict) -> str:
    raw = (
        f"ingest:{kb_id}:{doc_id}:{version_id}:{PIPELINE_VERSION}:"
        f"{signature['model']}:{signature['dimension']}:{CHUNKER_VERSION}"
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:64]


# ── Job 入队 ──────────────────────────────────────────────────────────────

async def queue_ingest(
    db: AsyncSession,
    *,
    knowledge_base: KnowledgeBase,
    document: KnowledgeDocument,
    version: KnowledgeDocumentVersion,
    generation_id: Optional[int] = None,
    activate: bool = False,
) -> KnowledgeIngestionJob:
    signature = embedding_signature()
    key = _idem_key(knowledge_base.id, document.id, version.id, signature)
    existing = (await db.execute(
        select(KnowledgeIngestionJob).where(
            KnowledgeIngestionJob.idempotency_key == key,
            KnowledgeIngestionJob.status.in_(("queued", "running")),
        )
    )).scalar_one_or_none()
    if existing is not None:
        return existing
    job = KnowledgeIngestionJob(
        knowledge_base_id=knowledge_base.id,
        document_id=document.id,
        document_version_id=version.id,
        job_type="ingest",
        stage="queued",
        status="queued",
        progress=0.0,
        attempt=0,
        max_attempts=max(1, int(settings.KNOWLEDGE_JOB_MAX_ATTEMPTS or 3)),
        idempotency_key=key,
        payload={
            "generation_id": generation_id,
            "activate": bool(activate),
            "pipeline_version": PIPELINE_VERSION,
            "embedding_model": signature["model"],
            "embedding_dimension": signature["dimension"],
            "chunker_version": CHUNKER_VERSION,
        },
    )
    db.add(job)
    await db.flush()
    return job


async def claim_job(
    db: AsyncSession, worker_id: str, *, lease_seconds: int = 120,
) -> Optional[KnowledgeIngestionJob]:
    """CAS 领取 Job；过期 Lease 先归还队列（attempt 未耗尽）。"""
    now = utcnow()
    # 1) 归还过期租约
    await db.execute(
        update(KnowledgeIngestionJob)
        .where(
            KnowledgeIngestionJob.status == "running",
            KnowledgeIngestionJob.lease_expires_at.is_not(None),
            KnowledgeIngestionJob.lease_expires_at < now,
        )
        .values(
            status="queued", stage="queued", lease_owner=None, lease_token=None,
            lease_expires_at=None, error_code="lease_expired",
            error_message="Worker 租约过期，任务已重新入队",
        )
    )
    await db.commit()

    # 2) 选最早入队的一条
    candidate = (await db.execute(
        select(KnowledgeIngestionJob.id).where(
            KnowledgeIngestionJob.status == "queued",
            KnowledgeIngestionJob.attempt < KnowledgeIngestionJob.max_attempts,
        ).order_by(KnowledgeIngestionJob.id.asc()).limit(1)
    )).scalar_one_or_none()
    if candidate is None:
        return None

    token = uuid.uuid4().hex[:32]
    result = await db.execute(
        update(KnowledgeIngestionJob)
        .where(
            KnowledgeIngestionJob.id == candidate,
            KnowledgeIngestionJob.status == "queued",
        )
        .values(
            status="running",
            stage="detecting",
            lease_owner=worker_id,
            lease_token=token,
            lease_expires_at=now + timedelta(seconds=max(10, lease_seconds)),
            heartbeat_at=now,
            attempt=KnowledgeIngestionJob.attempt + 1,
            started_at=func.coalesce(KnowledgeIngestionJob.started_at, now),
            error_code=None,
            error_message=None,
        )
    )
    await db.commit()
    if not result.rowcount:
        return None
    return await db.get(KnowledgeIngestionJob, candidate)


async def _heartbeat(job_id: int, worker_id: str, token: str, lease_seconds: int) -> bool:
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            update(KnowledgeIngestionJob)
            .where(
                KnowledgeIngestionJob.id == job_id,
                KnowledgeIngestionJob.lease_owner == worker_id,
                KnowledgeIngestionJob.lease_token == token,
                KnowledgeIngestionJob.status == "running",
            )
            .values(
                heartbeat_at=utcnow(),
                lease_expires_at=utcnow() + timedelta(seconds=max(10, lease_seconds)),
            )
        )
        await db.commit()
        return bool(result.rowcount)


async def _set_stage(db: AsyncSession, job: KnowledgeIngestionJob, stage: str, progress: float) -> None:
    job.stage = stage
    job.progress = max(0.0, min(1.0, float(progress)))
    db.add(job)
    await db.commit()


class _JobCancelled(Exception):
    """Job 已被删除流程取消 / 租约丢失：停止写入，不做失败重试。"""


class _HeartbeatLoop:
    def __init__(self, job_id: int, worker_id: str, token: str, lease_seconds: int) -> None:
        self.job_id = job_id
        self.worker_id = worker_id
        self.token = token
        self.lease_seconds = lease_seconds
        self.lost = False
        self._task: Optional[asyncio.Task] = None

    def ensure(self) -> None:
        if self.lost:
            raise _JobCancelled("Job 租约已丢失（可能已被删除流程取消）")

    async def __aenter__(self):
        interval = max(3.0, float(settings.KNOWLEDGE_WORKER_HEARTBEAT_INTERVAL or 15))

        async def _run():
            while True:
                await asyncio.sleep(interval)
                try:
                    ok = await _heartbeat(self.job_id, self.worker_id, self.token, self.lease_seconds)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("Ingestion Job %s 心跳失败：%s", self.job_id, exc)
                    continue
                if not ok:
                    self.lost = True
                    return

        self._task = asyncio.create_task(_run())
        return self

    async def __aexit__(self, *_exc):
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task


# ── Generation ────────────────────────────────────────────────────────────

async def ensure_active_generation(db: AsyncSession, kb: KnowledgeBase) -> KnowledgeIndexGeneration:
    if kb.active_index_generation_id:
        generation = await db.get(KnowledgeIndexGeneration, kb.active_index_generation_id)
        if generation is not None:
            return generation
    signature = embedding_signature()
    last_no = (await db.execute(
        select(func.max(KnowledgeIndexGeneration.generation_no)).where(
            KnowledgeIndexGeneration.knowledge_base_id == kb.id
        )
    )).scalar_one_or_none() or 0
    generation = KnowledgeIndexGeneration(
        knowledge_base_id=kb.id,
        generation_no=int(last_no) + 1,
        embedding_model=signature["model"],
        embedding_dimension=int(signature["dimension"] or 0),
        chunker_version=CHUNKER_VERSION,
        parser_version=PIPELINE_VERSION,
        collection_name=collection_name_for(signature["model"], int(signature["dimension"] or 512)),
        status="building",
    )
    db.add(generation)
    await db.flush()
    kb.active_index_generation_id = generation.id
    kb.embedding_model = signature["model"]
    kb.embedding_dimension = int(signature["dimension"] or 0)
    db.add(kb)
    await db.commit()
    return generation


async def create_rebuild_generation(db: AsyncSession, kb: KnowledgeBase) -> KnowledgeIndexGeneration:
    signature = embedding_signature()
    last_no = (await db.execute(
        select(func.max(KnowledgeIndexGeneration.generation_no)).where(
            KnowledgeIndexGeneration.knowledge_base_id == kb.id
        )
    )).scalar_one_or_none() or 0
    generation = KnowledgeIndexGeneration(
        knowledge_base_id=kb.id,
        generation_no=int(last_no) + 1,
        embedding_model=signature["model"],
        embedding_dimension=int(signature["dimension"] or 0),
        chunker_version=CHUNKER_VERSION,
        parser_version=PIPELINE_VERSION,
        collection_name=collection_name_for(signature["model"], int(signature["dimension"] or 512)),
        status="building",
    )
    db.add(generation)
    await db.commit()
    return generation


async def maybe_activate_generation(
    db: AsyncSession, kb_id: int, generation_id: int,
) -> bool:
    """同一 Generation 的所有 Job 结束后原子切换 Active（失败不破坏旧索引）。"""
    generation = await db.get(KnowledgeIndexGeneration, generation_id)
    if generation is None or generation.status != "building":
        return False
    pending_rows = (await db.execute(
        select(KnowledgeIngestionJob.payload).where(
            KnowledgeIngestionJob.knowledge_base_id == kb_id,
            KnowledgeIngestionJob.status.in_(("queued", "running")),
        )
    )).scalars().all()
    if any((payload or {}).get("generation_id") == generation_id for payload in pending_rows):
        return False
    point_count = (await db.execute(
        select(func.count()).select_from(KnowledgeChunk).where(
            KnowledgeChunk.knowledge_base_id == kb_id,
            KnowledgeChunk.index_generation_id == generation_id,
            KnowledgeChunk.is_parent.is_(False),
        )
    )).scalar() or 0
    if not point_count:
        generation.status = "failed"
        db.add(generation)
        await db.commit()
        return False
    kb = await db.get(KnowledgeBase, kb_id)
    if kb is None:
        return False
    previous_id = kb.active_index_generation_id
    generation.status = "active"
    generation.activated_at = utcnow()
    generation.point_count = int(point_count)
    kb.active_index_generation_id = generation.id
    kb.embedding_model = generation.embedding_model
    kb.embedding_dimension = generation.embedding_dimension
    kb.last_ingested_at = utcnow()
    db.add(generation)
    db.add(kb)
    if previous_id and previous_id != generation.id:
        previous = await db.get(KnowledgeIndexGeneration, previous_id)
        if previous is not None:
            previous.status = "retired"
            db.add(previous)
    await db.commit()
    await recount_kb(db, kb_id)
    return True


async def recount_kb(db: AsyncSession, kb_id: int) -> None:
    kb = await db.get(KnowledgeBase, kb_id)
    if kb is None:
        return
    doc_count = (await db.execute(
        select(func.count()).select_from(KnowledgeDocument).where(
            KnowledgeDocument.knowledge_base_id == kb_id,
            KnowledgeDocument.status.notin_(("deleted",)),
        )
    )).scalar() or 0
    failed = (await db.execute(
        select(func.count()).select_from(KnowledgeDocument).where(
            KnowledgeDocument.knowledge_base_id == kb_id,
            KnowledgeDocument.status == "failed",
        )
    )).scalar() or 0
    chunk_count = (await db.execute(
        select(func.count()).select_from(KnowledgeChunk).where(
            KnowledgeChunk.knowledge_base_id == kb_id,
            KnowledgeChunk.index_generation_id == (kb.active_index_generation_id or 0),
            KnowledgeChunk.is_parent.is_(False),
        )
    )).scalar() or 0
    kb.doc_count = int(doc_count)
    kb.failed_doc_count = int(failed)
    kb.indexed_chunk_count = int(chunk_count)
    generation = None
    if kb.active_index_generation_id:
        generation = await db.get(KnowledgeIndexGeneration, kb.active_index_generation_id)
    if generation is not None:
        vector_store = get_vector_store()
        try:
            flt = build_filter(
                knowledge_base_ids=[kb_id], generation_ids=[generation.id],
            )
            kb.vector_count = await vector_store.count(generation.collection_name, flt)
            generation.point_count = int(kb.vector_count)
            db.add(generation)
        except Exception as exc:  # noqa: BLE001
            logger.warning("统计 Qdrant Point 失败 kb=%s: %s", kb_id, exc)
            kb.vector_count = int(chunk_count)
    else:
        kb.vector_count = 0
    db.add(kb)
    await db.commit()
    try:
        from app.observability import metrics

        metrics.set_knowledge_counts(kb_id, kb.indexed_chunk_count, kb.vector_count)
    except Exception:  # noqa: BLE001
        pass


# ── Pipeline ──────────────────────────────────────────────────────────────

class IngestionRunner:
    def __init__(self, worker_id: str) -> None:
        self.worker_id = worker_id

    async def run(self, job_id: int) -> None:
        async with AsyncSessionLocal() as db:
            job = await db.get(KnowledgeIngestionJob, job_id)
            if job is None or job.status != "running" or job.lease_owner != self.worker_id:
                return
            job_payload = dict(job.payload or {})
            kb = await db.get(KnowledgeBase, job.knowledge_base_id)
            document = await db.get(KnowledgeDocument, job.document_id) if job.document_id else None
            version = (
                await db.get(KnowledgeDocumentVersion, job.document_version_id)
                if job.document_version_id else None
            )
            if kb is None or document is None or version is None:
                await self._fail(db, job, "资源缺失", "resource_missing", retryable=False)
                return

            lease_seconds = int(settings.KNOWLEDGE_JOB_LEASE_SECONDS or 120)
            try:
                async with _HeartbeatLoop(
                    job.id, self.worker_id, job.lease_token or "", lease_seconds
                ) as watch:
                    await self._pipeline(db, job, job_payload, kb, document, version, watch)
            except _JobCancelled as exc:
                logger.info("Ingestion Job %s 已取消：%s", job_id, exc)
                await self._cancel(db, job, version)
            except Exception as exc:  # noqa: BLE001
                logger.exception("Ingestion Job %s 失败", job_id)
                retryable = not (
                    isinstance(exc, ParseError)
                    and exc.code in {
                        "unsupported_format", "empty_extraction", "too_many_pages",
                        "legacy_conversion_unavailable", "docling_unavailable",
                        "parse_failed", "legacy_conversion_failed",
                    }
                )
                if isinstance(exc, EmbeddingError):
                    retryable = exc.retryable
                await self._fail(
                    db, job, str(exc)[:2000],
                    getattr(exc, "code", "ingest_failed"), retryable=retryable,
                )

    async def _pipeline(
        self,
        db: AsyncSession,
        job: KnowledgeIngestionJob,
        payload: Dict[str, Any],
        kb: KnowledgeBase,
        document: KnowledgeDocument,
        version: KnowledgeDocumentVersion,
        watch: Optional[_HeartbeatLoop] = None,
    ) -> None:
        await self._assert_active(db, document, watch)
        generation = None
        if payload.get("generation_id"):
            generation = await db.get(KnowledgeIndexGeneration, int(payload["generation_id"]))
        if generation is None:
            generation = await ensure_active_generation(db, kb)
        pipeline_started = time.monotonic()
        parse_ms = 0.0
        embed_ms = 0.0

        document.status = "processing"
        document.error_code = None
        document.error_msg = None
        version.status = "building"
        version.index_generation_id = generation.id
        db.add(document)
        db.add(version)
        await db.commit()

        # ── detect ─────────────────────────────────────────────
        await _set_stage(db, job, "detecting", 0.02)
        source_path = storage.resolve_storage_key(document.storage_key)
        if source_path is None or not source_path.is_file():
            # 兼容旧数据：file_path 为绝对路径
            from pathlib import Path

            legacy_path = Path(document.file_path) if document.file_path else None
            if legacy_path is not None and legacy_path.is_file():
                source_path = legacy_path
        if source_path is None or not source_path.is_file():
            raise ParseError("源文件缺失，无法重新解析（标记 migration_required）", code="source_missing")
        actual_sha = storage.sha256_file(source_path)
        if document.sha256 and actual_sha != document.sha256:
            raise ParseError("源文件 SHA256 与登记不一致，拒绝索引", code="source_changed")
        if not document.sha256:
            document.sha256 = actual_sha
            db.add(document)

        # ── parse ──────────────────────────────────────────────
        await _set_stage(db, job, "parsing", 0.08)
        artifact_path = storage.parsed_artifact_path(kb.id, document.id, version.version_no)
        parsed_data = storage.read_json_artifact(artifact_path)
        parser = get_parser(kb.parser_profile if kb.parser_profile in {"docling", "native"} else "")
        parsed: Optional[ParsedDocument] = None
        # 解析产物复用必须同时匹配 Parser 名称与版本（Docling / Native 结构不同）
        if (
            parsed_data
            and parsed_data.get("parser_name") == parser.name
            and parsed_data.get("parser_version") == parser.version
        ):
            try:
                parsed = ParsedDocument.from_dict(parsed_data)
            except Exception:  # noqa: BLE001
                parsed = None
        if parsed is not None:
            # 复用解析产物：补齐版本引用（上次可能在写库前崩溃）
            version.parsed_artifact_key = version.parsed_artifact_key or storage.relative_key(artifact_path)
            version.parser_name = parsed.parser_name
            version.parser_version = parsed.parser_version
        if parsed is None:
            parse_started = time.monotonic()
            options = ParseOptions(
                ocr_enabled=bool(settings.KNOWLEDGE_OCR_ENABLED),
                max_pages=int(settings.KNOWLEDGE_MAX_PAGES or 500),
                timeout_seconds=int(settings.KNOWLEDGE_PARSE_TIMEOUT_SECONDS or 300),
            )
            parsed = await parser.parse(
                source_path,
                file_type=document.file_type,
                document_id=document.id,
                source_name=document.filename,
                options=options,
            )
            if parsed.ocr_pages:
                await _set_stage(db, job, "ocr", 0.2)
            if parsed.ocr_pages:
                parsed.parse_stats["ocr_pages"] = sorted(set(parsed.ocr_pages))
            storage.write_json_artifact(artifact_path, parsed.to_dict())
            version.parsed_artifact_key = storage.relative_key(artifact_path)
            version.parser_name = parsed.parser_name
            version.parser_version = parsed.parser_version
            parse_ms = (time.monotonic() - parse_started) * 1000
            try:
                from app.observability import metrics

                metrics.observe_knowledge_parse(parse_ms / 1000.0)
            except Exception:  # noqa: BLE001
                pass
        await _set_stage(db, job, "normalizing", 0.3)

        stats = parsed.parse_stats or {}
        document.page_count = int(stats.get("page_count") or 0)
        document.sheet_count = int(stats.get("sheet_count") or 0)
        document.slide_count = int(stats.get("slide_count") or 0)
        document.element_count = int(stats.get("element_count") or 0)
        document.parse_stats_json = stats
        document.warning_json = {"warnings": parsed.warnings}
        db.add(document)
        await db.commit()

        # ── chunk ──────────────────────────────────────────────
        await _set_stage(db, job, "chunking", 0.4)
        chunker = StructureAwareChunker(ChunkingConfig(
            chunk_size_tokens=int(kb.chunk_size_tokens or settings.KNOWLEDGE_CHUNK_SIZE_TOKENS),
            chunk_overlap_tokens=int(kb.chunk_overlap_tokens or settings.KNOWLEDGE_CHUNK_OVERLAP_TOKENS),
            parent_max_tokens=max(
                int(kb.chunk_size_tokens or settings.KNOWLEDGE_CHUNK_SIZE_TOKENS),
                int(settings.KNOWLEDGE_PARENT_CHUNK_MAX_TOKENS or 1536),
            ),
        ))
        chunks = chunker.chunk(
            parsed,
            knowledge_base_id=kb.id,
            document_id=document.id,
            document_version_id=version.id,
            index_generation_id=generation.id,
        )
        if not chunks:
            raise ParseError("Chunking 未产生任何内容", code="empty_extraction")

        # 幂等重跑：清理本版本同代际旧 Chunk 与向量
        await self._assert_active(db, document, watch)
        await db.execute(
            delete(KnowledgeChunk).where(
                KnowledgeChunk.document_id == document.id,
                KnowledgeChunk.index_generation_id == generation.id,
            )
        )
        await db.commit()
        store = get_vector_store()
        try:
            await store.delete_points(
                generation.collection_name,
                build_filter(
                    knowledge_base_ids=[kb.id], generation_ids=[generation.id],
                    document_id=document.id,
                ),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("清理旧 Qdrant Point 失败（继续重建）：%s", exc)

        rows = [
            chunk.to_row(
                knowledge_base_id=kb.id, document_id=document.id,
                document_version_id=version.id, index_generation_id=generation.id,
                filename=document.filename, file_type=document.file_type,
                title=parsed.title or document.filename,
                parser_version=parsed.parser_version,
            )
            for chunk in chunks
        ]
        for start in range(0, len(rows), 500):
            await db.execute(insert(KnowledgeChunk), rows[start:start + 500])
        await db.commit()

        # ── embedding + indexing ───────────────────────────────
        await _set_step_commit(db, job, "embedding", 0.55)
        provider = get_embedding_provider()
        dimension = int(provider.dimension or generation.embedding_dimension or 0)
        if dimension <= 0:
            # 主动探测维度（本地模型首次加载后可知）
            probe = await provider.embed(["dimension probe"])
            dimension = len(probe[0]) if probe else 0
        if dimension <= 0:
            raise EmbeddingError("无法确定 Embedding 维度")
        generation.embedding_dimension = dimension
        generation.embedding_model = provider.model
        generation.collection_name = collection_name_for(provider.model, dimension)
        db.add(generation)
        await db.commit()
        await store.ensure_collection(generation.collection_name, dimension)

        batch_size = max(1, int(settings.KNOWLEDGE_EMBEDDING_BATCH_SIZE or 16) * 4)
        total = len(rows)
        indexed = 0
        embed_started = time.monotonic()
        for start in range(0, total, batch_size):
            await self._assert_active(db, document, watch)
            batch = rows[start:start + batch_size]
            texts = [
                (" / ".join(row["heading_path"] or []) + "\n" + row["content"]).strip()
                for row in batch
            ]
            vectors = await provider.embed(texts)
            points = []
            for row, vector in zip(batch, vectors):
                points.append({
                    "chunk_id": row["id"],
                    "vector": vector,
                    "payload": {
                        "chunk_id": row["id"],
                        "knowledge_base_id": kb.id,
                        "owner_id": kb.owner_id,
                        "project_id": kb.project_id,
                        "document_id": document.id,
                        "document_version_id": version.id,
                        "index_generation_id": generation.id,
                        "filename": document.filename,
                        "source_type": document.file_type,
                        "title": row["title"],
                        "page": row["page_start"],
                        "page_end": row["page_end"],
                        "sheet_name": row["sheet_name"],
                        "row_start": row["row_start"],
                        "row_end": row["row_end"],
                        "slide_number": row["slide_number"],
                        "table_index": row["table_index"],
                        "heading_path": row["heading_path"],
                        "element_type": row["element_type"],
                        "content_hash": row["content_hash"],
                        "text": row["content"][:4000],
                        "is_parent": row["is_parent"],
                    },
                })
            await store.upsert(generation.collection_name, points)
            indexed += len(points)
            elapsed_ratio = indexed / max(total, 1)
            job.progress = 0.55 + 0.35 * elapsed_ratio
            db.add(job)
            await db.commit()
        embed_ms = (time.monotonic() - embed_started) * 1000
        try:
            from app.observability import metrics

            metrics.observe_knowledge_embedding(embed_ms / 1000.0)
            metrics.observe_knowledge_index_write(embed_ms / 1000.0)
        except Exception:  # noqa: BLE001
            pass
        await _set_stage(db, job, "indexing", 0.92)

        # ── validate ───────────────────────────────────────────
        await _set_stage(db, job, "validating", 0.96)
        child_count = (await db.execute(
            select(func.count()).select_from(KnowledgeChunk).where(
                KnowledgeChunk.document_id == document.id,
                KnowledgeChunk.index_generation_id == generation.id,
                KnowledgeChunk.is_parent.is_(False),
            )
        )).scalar() or 0
        try:
            point_count = await store.count(
                generation.collection_name,
                build_filter(
                    knowledge_base_ids=[kb.id], generation_ids=[generation.id],
                    document_id=document.id,
                ),
            )
        except Exception as exc:  # noqa: BLE001
            raise EmbeddingError(f"Qdrant 校验失败：{exc}") from exc
        if point_count < child_count:
            raise EmbeddingError(
                f"向量写入不完整：Qdrant {point_count} < Chunk {child_count}，将重试"
            )

        # ── ready ──────────────────────────────────────────────
        partial = bool(parsed.warnings) or bool(parsed.ocr_pages and (
            (stats.get("ocr_mean_score") or 1.0) < float(settings.KNOWLEDGE_OCR_MIN_SCORE or 0.5)
        ))
        document.status = "partial" if partial else "ready"
        document.chunk_count = int(child_count)
        document.error_code = None
        document.error_msg = None
        document.active_version_id = version.id
        version.status = document.status
        version.error_msg = None
        db.add(document)
        db.add(version)
        job.status = "succeeded"
        job.stage = "ready"
        job.progress = 1.0
        job.finished_at = utcnow()
        job.error_code = None
        job.error_message = None
        job.lease_owner = None
        job.lease_token = None
        job.lease_expires_at = None
        db.add(job)
        await db.commit()

        await maybe_activate_generation(db, kb.id, generation.id)
        await recount_kb(db, kb.id)
        try:
            from app.observability import metrics

            metrics.observe_knowledge_job(
                "succeeded", "ready", (time.monotonic() - pipeline_started)
            )
        except Exception:  # noqa: BLE001
            pass

    async def _assert_active(
        self, db: AsyncSession, document: KnowledgeDocument,
        watch: Optional[_HeartbeatLoop],
    ) -> None:
        if watch is not None:
            watch.ensure()
        status = (await db.execute(
            select(KnowledgeDocument.status).where(KnowledgeDocument.id == document.id)
        )).scalar_one_or_none()
        if status in (None, "deleting", "deleted"):
            raise _JobCancelled("文档已被删除，停止写入")

    async def _cancel(
        self, db: AsyncSession, job: KnowledgeIngestionJob,
        version: KnowledgeDocumentVersion,
    ) -> None:
        """删除流程取消 Job：清理本代际已写入的 Chunk / Point，绝不标记 ready。"""
        job.status = "cancelled"
        job.stage = "cancelled"
        job.lease_owner = None
        job.lease_token = None
        job.lease_expires_at = None
        job.finished_at = utcnow()
        job.error_code = "cancelled"
        job.error_message = "Job 已被删除流程取消或租约丢失"
        version.status = "failed"
        version.error_msg = job.error_message
        db.add(job)
        db.add(version)
        await db.commit()
        generation_id = version.index_generation_id
        if not job.document_id or not generation_id:
            return
        await db.execute(
            delete(KnowledgeChunk).where(
                KnowledgeChunk.document_id == job.document_id,
                KnowledgeChunk.index_generation_id == generation_id,
            )
        )
        await db.commit()
        generation = await db.get(KnowledgeIndexGeneration, generation_id)
        if generation is None:
            return
        try:
            await get_vector_store().delete_points(
                generation.collection_name,
                build_filter(
                    knowledge_base_ids=[job.knowledge_base_id],
                    generation_ids=[generation_id],
                    document_id=job.document_id,
                ),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("取消 Job 清理 Qdrant Point 失败（删除流程会再次清理）：%s", exc)

    async def _fail(
        self, db: AsyncSession, job: KnowledgeIngestionJob, message: str,
        code: str, *, retryable: bool,
    ) -> None:
        job.attempt = int(job.attempt or 0)
        can_retry = retryable and job.attempt < int(job.max_attempts or 0)
        job.status = "queued" if can_retry else "failed"
        job.stage = "queued" if can_retry else "failed"
        job.error_code = code
        job.error_message = message
        job.lease_owner = None
        job.lease_token = None
        job.lease_expires_at = None
        if not can_retry:
            job.finished_at = utcnow()
        db.add(job)
        if job.document_id:
            document = await db.get(KnowledgeDocument, job.document_id)
            if document is not None and document.status not in ("deleting", "deleted"):
                document.status = "failed" if not can_retry else "processing"
                document.error_code = code
                document.error_msg = message
                db.add(document)
        if job.document_version_id:
            version = await db.get(KnowledgeDocumentVersion, job.document_version_id)
            if version is not None:
                version.status = "failed" if not can_retry else "building"
                version.error_msg = message
                db.add(version)
        await db.commit()
        if not can_retry:
            await recount_kb(db, job.knowledge_base_id)
        try:
            from app.observability import metrics

            metrics.observe_knowledge_job(
                "failed" if not can_retry else "queued", job.stage,
                None if not job.started_at else max(0.0, (utcnow() - job.started_at).total_seconds()),
            )
            if code in {"parse_failed", "unsupported_format", "legacy_conversion_unavailable",
                        "legacy_conversion_failed", "too_many_pages", "docling_unavailable"}:
                metrics.inc_knowledge_parse_failure(code)
            if code in {"empty_extraction",}:
                metrics.inc_knowledge_empty_extraction()
        except Exception:  # noqa: BLE001
            pass
        logger.warning(
            "Ingestion Job %s %s（code=%s, attempt=%s/%s）：%s",
            job.id, "重新入队" if can_retry else "失败",
            code, job.attempt, job.max_attempts, message,
        )


async def _set_step_commit(db: AsyncSession, job: KnowledgeIngestionJob, stage: str, progress: float) -> None:
    job.stage = stage
    job.progress = progress
    db.add(job)
    await db.commit()


# ── 删除一致性（任务书 §11.5）──────────────────────────────────────────────

class KnowledgeCleanupError(Exception):
    """清理失败：保留 deleting 状态与错误，可重复调用重试。"""


async def delete_document_data(db: AsyncSession, kb: KnowledgeBase, document: KnowledgeDocument) -> None:
    """DB 标记 deleting -> 删 Qdrant Points -> Chunk -> Parsed Artifact -> 源文件 -> 文档行。"""
    document.status = "deleting"
    db.add(document)
    # 取消未完成的构建 Job：Worker 下一次心跳失败后停止写入并自行清理
    await db.execute(
        update(KnowledgeIngestionJob)
        .where(
            KnowledgeIngestionJob.document_id == document.id,
            KnowledgeIngestionJob.status.in_(("queued", "running")),
        )
        .values(
            status="cancelled", stage="cancelled", lease_owner=None, lease_token=None,
            lease_expires_at=None, finished_at=utcnow(),
            error_code="cancelled", error_message="文档删除，构建任务已取消",
        )
    )
    await db.commit()
    try:
        generation_ids = (await db.execute(
            select(KnowledgeIndexGeneration.id, KnowledgeIndexGeneration.collection_name).where(
                KnowledgeIndexGeneration.knowledge_base_id == kb.id
            )
        )).all()
        store = get_vector_store()
        for _generation_id, collection in generation_ids:
            try:
                await store.delete_points(
                    collection,
                    build_filter(knowledge_base_ids=[kb.id], generation_ids=[], document_id=document.id),
                )
            except Exception as exc:  # noqa: BLE001
                raise KnowledgeCleanupError(f"删除 Qdrant Points 失败：{exc}") from exc
        await db.execute(
            delete(KnowledgeChunk).where(KnowledgeChunk.document_id == document.id)
        )
        await db.commit()
        try:
            storage.delete_document_files(kb.id, document.id)
        except OSError as exc:
            raise KnowledgeCleanupError(f"删除解析产物 / 源文件失败：{exc}") from exc
        # 旧版源文件（UPLOAD_DIR/{kb_id}/...）：仅新存储未接管时清理
        try:
            from pathlib import Path

            if not document.storage_key:
                legacy = Path(document.file_path) if document.file_path else None
                upload_root = Path(settings.UPLOAD_DIR).resolve()
                if legacy is not None and legacy.is_file():
                    resolved = legacy.resolve()
                    if resolved == upload_root or upload_root in resolved.parents:
                        resolved.unlink(missing_ok=True)
        except OSError as exc:
            raise KnowledgeCleanupError(f"删除旧版源文件失败：{exc}") from exc
        await db.execute(
            delete(KnowledgeDocumentVersion).where(KnowledgeDocumentVersion.document_id == document.id)
        )
        await db.execute(
            delete(KnowledgeIngestionJob).where(KnowledgeIngestionJob.document_id == document.id)
        )
        await db.delete(document)
        await db.commit()
        await recount_kb(db, kb.id)
    except KnowledgeCleanupError:
        raise
    except Exception as exc:  # noqa: BLE001
        await db.rollback()
        document = await db.get(KnowledgeDocument, document.id)
        if document is not None:
            document.status = "deleting"
            document.error_code = "delete_failed"
            document.error_msg = str(exc)[:2000]
            db.add(document)
            await db.commit()
        raise KnowledgeCleanupError(f"删除文档失败：{exc}") from exc


async def delete_knowledge_base_data(db: AsyncSession, kb: KnowledgeBase) -> None:
    """知识库删除：阻止检索 -> 逐文档清理 -> 统计/代际/Job -> 文件目录 -> KB 行。"""
    kb.status = "deleting"
    db.add(kb)
    await db.commit()
    try:
        documents = (await db.execute(
            select(KnowledgeDocument).where(KnowledgeDocument.knowledge_base_id == kb.id)
        )).scalars().all()
        for document in documents:
            await delete_document_data(db, kb, document)
        generations = (await db.execute(
            select(KnowledgeIndexGeneration).where(
                KnowledgeIndexGeneration.knowledge_base_id == kb.id
            )
        )).scalars().all()
        store = get_vector_store()
        for generation in generations:
            try:
                await store.delete_points(
                    generation.collection_name,
                    build_filter(knowledge_base_ids=[kb.id], generation_ids=[]),
                )
            except Exception as exc:  # noqa: BLE001
                raise KnowledgeCleanupError(f"删除知识库向量失败：{exc}") from exc
        await db.execute(delete(KnowledgeChunk).where(KnowledgeChunk.knowledge_base_id == kb.id))
        await db.execute(
            delete(KnowledgeIngestionJob).where(KnowledgeIngestionJob.knowledge_base_id == kb.id)
        )
        await db.execute(
            delete(KnowledgeIndexGeneration).where(
                KnowledgeIndexGeneration.knowledge_base_id == kb.id
            )
        )
        await db.commit()
        try:
            storage.delete_kb_files(kb.id)
        except OSError as exc:
            raise KnowledgeCleanupError(f"删除知识库文件目录失败：{exc}") from exc
        # 旧版 SQLite 检索索引（短期回滚链路）
        if kb.collection_name:
            from app.services.knowledge_service import KnowledgeService

            try:
                KnowledgeService.delete_collection(kb.collection_name)
            except Exception as exc:  # noqa: BLE001
                logger.warning("删除旧 SQLite 索引失败（忽略）：%s", exc)
        # 旧版上传目录
        try:
            from pathlib import Path
            import shutil

            legacy_dir = Path(settings.UPLOAD_DIR) / str(kb.id)
            if legacy_dir.exists():
                shutil.rmtree(legacy_dir)
        except OSError as exc:
            raise KnowledgeCleanupError(f"删除旧上传目录失败：{exc}") from exc
        kb.status = "deleted"
        db.add(kb)
        await db.delete(kb)
        await db.commit()
    except KnowledgeCleanupError:
        raise
    except Exception as exc:  # noqa: BLE001
        await db.rollback()
        kb = await db.get(KnowledgeBase, kb.id)
        if kb is not None:
            kb.status = "deleting"
            db.add(kb)
            await db.commit()
        raise KnowledgeCleanupError(f"删除知识库失败：{exc}") from exc
