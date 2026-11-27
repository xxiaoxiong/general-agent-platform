"""Knowledge API（/api/v1/knowledge）：知识库 / 文档 / 构建 Job / 检索 / 评测。

安全：所有端点统一走 ``require_base_access``（owner / 项目成员 / 组织共享），
Agent 绑定校验不能替代 API 自身校验；无权限与不存在统一 404。
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.core.config import settings
from app.core.database import get_db
from app.models.knowledge import (
    KnowledgeBase,
    KnowledgeChunk,
    KnowledgeDocument,
    KnowledgeDocumentVersion,
    KnowledgeEvaluation,
    KnowledgeIndexGeneration,
    KnowledgeIngestionJob,
)
from app.models.sandbox import Project, ProjectMember
from app.models.user import User
from app.schemas.common import PageResponse
from app.schemas.knowledge import (
    ChunkOut, ChunkPageOut, DocumentUploadOut, EvaluationCreate,
    EvaluationOut, IngestionJobOut, KnowledgeBaseCreate, KnowledgeBaseOut,
    KnowledgeBaseUpdate, KnowledgeDocumentOut, ParsedPreviewOut, ReindexRequest,
    SearchRequest, SearchResponse, SearchResult, WebCrawlRequest,
)
from app.services.audit import log_action
from app.services.knowledge import acl as knowledge_acl
from app.services.knowledge import storage
from app.services.knowledge.ingestion import (
    KnowledgeCleanupError, create_rebuild_generation, delete_document_data,
    delete_knowledge_base_data, queue_ingest,
)
from app.services.knowledge.retrieval import KnowledgeRetrievalService, RetrievalOptions
from app.services.knowledge.evaluation import EvaluationCase, evaluate
from app.services.knowledge.security import (
    KnowledgeSecurityError, fetch_webpage_safely, validate_extension, validate_magic,
    validate_chunk_config,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/knowledge", tags=["知识库"])


def _raise_acl(exc: Exception) -> None:
    if isinstance(exc, knowledge_acl.KnowledgeNotFoundError):
        raise HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, knowledge_acl.KnowledgeAccessDenied):
        raise HTTPException(status_code=403, detail=str(exc))
    raise exc


async def _get_base(db: AsyncSession, kb_id: int, user: User, action: str) -> KnowledgeBase:
    try:
        kb, _role = await knowledge_acl.require_base_access(db, kb_id, user, action)
        return kb
    except (knowledge_acl.KnowledgeNotFoundError, knowledge_acl.KnowledgeAccessDenied) as exc:
        _raise_acl(exc)


async def _get_document(db: AsyncSession, doc_id: int, user: User, action: str):
    document = await db.get(KnowledgeDocument, int(doc_id))
    if document is None or document.status == "deleted":
        raise HTTPException(status_code=404, detail="文档不存在")
    kb = await _get_base(db, document.knowledge_base_id, user, action)
    return kb, document


async def _validate_project_binding(db: AsyncSession, project_id: int, user: User) -> None:
    project = (await db.execute(
        select(Project).where(Project.id == int(project_id), Project.deleted_at.is_(None))
    )).scalar_one_or_none()
    if project is None:
        raise HTTPException(status_code=404, detail="项目不存在")
    if bool(getattr(user, "is_superuser", False)) or project.owner_id == user.id:
        return
    role = (await db.execute(
        select(ProjectMember.role).where(
            ProjectMember.project_id == int(project_id),
            ProjectMember.user_id == user.id,
        )
    )).scalar_one_or_none()
    if role not in {"operator", "admin"}:
        raise HTTPException(status_code=403, detail="需要项目 operator 及以上权限才能绑定知识库")


async def _latest_job(db: AsyncSession, document_id: int) -> Optional[KnowledgeIngestionJob]:
    return (await db.execute(
        select(KnowledgeIngestionJob)
        .where(KnowledgeIngestionJob.document_id == document_id)
        .order_by(KnowledgeIngestionJob.id.desc()).limit(1)
    )).scalar_one_or_none()


def _check_chunk_config(kb: KnowledgeBase) -> None:
    try:
        validate_chunk_config(kb.chunk_size_tokens, kb.chunk_overlap_tokens)
    except KnowledgeSecurityError as exc:
        raise HTTPException(status_code=422, detail=str(exc))


# ── Knowledge Base CRUD ──────────────────────────────────────────────────

@router.get("/bases", response_model=PageResponse[KnowledgeBaseOut])
async def list_knowledge_bases(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    keyword: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    query = select(KnowledgeBase).where(
        knowledge_acl.accessible_bases_filter(current_user),
        KnowledgeBase.is_active.is_(True),
    )
    if keyword:
        query = query.where(KnowledgeBase.name.contains(keyword))
    total = (await db.execute(select(func.count()).select_from(query.subquery()))).scalar() or 0
    query = query.order_by(KnowledgeBase.id.desc()).offset((page - 1) * page_size).limit(page_size)
    bases = (await db.execute(query)).scalars().all()
    return PageResponse(
        items=[KnowledgeBaseOut.model_validate(b) for b in bases],
        total=total, page=page, page_size=page_size,
        total_pages=(total + page_size - 1) // page_size,
    )


@router.post("/bases", response_model=KnowledgeBaseOut)
async def create_knowledge_base(
    body: KnowledgeBaseCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if body.project_id:
        await _validate_project_binding(db, body.project_id, current_user)
    if body.visibility == "project" and not body.project_id:
        raise HTTPException(status_code=422, detail="visibility=project 时必须提供 project_id")
    collection_name = f"kb_{current_user.id}_{uuid.uuid4().hex[:8]}"
    data = body.model_dump()
    data["embedding_model"] = body.embedding_model or settings.KNOWLEDGE_EMBEDDING_MODEL
    kb = KnowledgeBase(
        **data,
        owner_id=current_user.id,
        collection_name=collection_name,
        status="active",
        embedding_dimension=int(settings.KNOWLEDGE_EMBEDDING_DIMENSION or 0),
    )
    _check_chunk_config(kb)
    db.add(kb)
    await db.flush()
    await log_action(db, current_user.id, current_user.username, "create_kb", "knowledge",
                     resource_id=str(kb.id))
    return KnowledgeBaseOut.model_validate(kb)


@router.get("/bases/{kb_id}", response_model=KnowledgeBaseOut)
async def get_knowledge_base(
    kb_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    kb = await _get_base(db, kb_id, current_user, "read")
    return KnowledgeBaseOut.model_validate(kb)


@router.put("/bases/{kb_id}", response_model=KnowledgeBaseOut)
async def update_knowledge_base(
    kb_id: int,
    body: KnowledgeBaseUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    kb = await _get_base(db, kb_id, current_user, "manage")
    data = body.model_dump(exclude_none=True)
    if "project_id" in data and data["project_id"]:
        await _validate_project_binding(db, data["project_id"], current_user)
    for field, value in data.items():
        setattr(kb, field, value)
    if kb.visibility == "project" and not kb.project_id:
        raise HTTPException(status_code=422, detail="visibility=project 时必须提供 project_id")
    _check_chunk_config(kb)
    db.add(kb)
    await db.flush()
    return KnowledgeBaseOut.model_validate(kb)


@router.delete("/bases/{kb_id}")
async def delete_knowledge_base(
    kb_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    kb = await _get_base(db, kb_id, current_user, "delete")
    try:
        await delete_knowledge_base_data(db, kb)
    except KnowledgeCleanupError as exc:
        raise HTTPException(status_code=500, detail=f"知识库清理未完成，可重试删除：{exc}")
    await log_action(db, current_user.id, current_user.username, "delete_kb", "knowledge",
                     resource_id=str(kb_id))
    return {"message": "删除成功"}


# ── Documents ────────────────────────────────────────────────────────────

@router.get("/bases/{kb_id}/documents", response_model=PageResponse[KnowledgeDocumentOut])
async def list_documents(
    kb_id: int,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    status: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    await _get_base(db, kb_id, current_user, "read")
    query = select(KnowledgeDocument).where(
        KnowledgeDocument.knowledge_base_id == kb_id,
        KnowledgeDocument.status != "deleted",
    )
    if status:
        query = query.where(KnowledgeDocument.status == status)
    total = (await db.execute(select(func.count()).select_from(query.subquery()))).scalar() or 0
    query = query.order_by(KnowledgeDocument.id.desc()).offset((page - 1) * page_size).limit(page_size)
    docs = (await db.execute(query)).scalars().all()
    return PageResponse(
        items=[KnowledgeDocumentOut.from_document(d) for d in docs],
        total=total, page=page, page_size=page_size,
        total_pages=(total + page_size - 1) // page_size,
    )


@router.post("/bases/{kb_id}/documents", response_model=DocumentUploadOut)
async def upload_document(
    kb_id: int,
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    kb = await _get_base(db, kb_id, current_user, "upload")
    _check_chunk_config(kb)
    try:
        ext = validate_extension(file.filename or "")
    except KnowledgeSecurityError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    filename = storage.sanitize_filename(file.filename or f"upload.{ext}")
    document = KnowledgeDocument(
        knowledge_base_id=kb.id, filename=filename, file_path="",
        file_type=ext, file_size=0, status="queued",
        storage_key=None, mime_type=None, sha256=None,
    )
    db.add(document)
    await db.flush()
    version_no = 1
    version = KnowledgeDocumentVersion(
        document_id=document.id, version_no=version_no, source_sha256="",
        parser_name="", parser_version="", pipeline_version="",
        status="building",
    )
    db.add(version)
    await db.flush()
    document_id = document.id
    try:
        stored = await storage.save_upload_stream(
            file, kb_id=kb.id, doc_id=document_id, version_no=version_no, ext=ext,
            max_bytes=int(settings.KNOWLEDGE_MAX_UPLOAD_SIZE_MB) * 1024 * 1024,
        )
        mime = validate_magic(str(stored.path), ext)
    except (ValueError, KnowledgeSecurityError) as exc:
        await db.rollback()
        try:
            storage.delete_document_files(kb_id, document_id)
        except OSError:
            pass
        raise HTTPException(status_code=400 if isinstance(exc, KnowledgeSecurityError) else 413,
                            detail=str(exc))
    except Exception as exc:  # noqa: BLE001
        await db.rollback()
        try:
            storage.delete_document_files(kb_id, document_id)
        except OSError:
            pass
        raise HTTPException(status_code=500, detail=f"文件保存失败：{exc}")

    document.file_path = str(stored.path)
    document.storage_key = stored.storage_key
    document.file_size = stored.size
    document.sha256 = stored.sha256
    document.mime_type = mime
    version.source_sha256 = stored.sha256
    job = await queue_ingest(db, knowledge_base=kb, document=document, version=version)
    kb.doc_count = (kb.doc_count or 0) + 1
    db.add(kb)
    await db.commit()
    await db.refresh(document)
    await db.refresh(job)
    await log_action(db, current_user.id, current_user.username, "upload_kb_doc", "knowledge",
                     resource_id=str(document.id))
    payload = DocumentUploadOut(
        **KnowledgeDocumentOut.from_document(document).model_dump(),
        job=IngestionJobOut.model_validate(job),
    )
    return payload


@router.get("/bases/{kb_id}/documents/{doc_id}", response_model=KnowledgeDocumentOut)
async def get_document(
    kb_id: int,
    doc_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    await _get_base(db, kb_id, current_user, "read")
    document = await db.get(KnowledgeDocument, doc_id)
    if document is None or document.knowledge_base_id != kb_id or document.status == "deleted":
        raise HTTPException(status_code=404, detail="文档不存在")
    return KnowledgeDocumentOut.from_document(document)


@router.delete("/bases/{kb_id}/documents/{doc_id}")
async def delete_document(
    kb_id: int,
    doc_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    kb = await _get_base(db, kb_id, current_user, "delete_document")
    document = await db.get(KnowledgeDocument, doc_id)
    if document is None or document.knowledge_base_id != kb.id or document.status == "deleted":
        raise HTTPException(status_code=404, detail="文档不存在")
    try:
        await delete_document_data(db, kb, document)
    except KnowledgeCleanupError as exc:
        raise HTTPException(status_code=500, detail=f"文档清理未完成，可重试：{exc}")
    await log_action(db, current_user.id, current_user.username, "delete_kb_doc", "knowledge",
                     resource_id=str(doc_id))
    return {"message": "删除成功"}


@router.get("/documents/{doc_id}/ingestion", response_model=IngestionJobOut)
async def get_ingestion(
    doc_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _kb, document = await _get_document(db, doc_id, current_user, "read")
    job = await _latest_job(db, document.id)
    if job is None:
        raise HTTPException(status_code=404, detail="该文档暂无构建任务")
    return IngestionJobOut.model_validate(job)


@router.post("/documents/{doc_id}/reindex", response_model=DocumentUploadOut)
async def reindex_document(
    doc_id: int,
    body: ReindexRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    kb, document = await _get_document(db, doc_id, current_user, "reindex")
    _check_chunk_config(kb)
    if document.storage_key is None and not (document.file_path and Path(document.file_path).is_file()):
        raise HTTPException(status_code=409, detail="源文件缺失，无法重建索引（可在文档列表中删除后重新上传）")
    last_no = (await db.execute(
        select(func.max(KnowledgeDocumentVersion.version_no)).where(
            KnowledgeDocumentVersion.document_id == document.id
        )
    )).scalar_one_or_none() or 0
    generation_id = None
    if body.new_generation:
        generation = await create_rebuild_generation(db, kb)
        generation_id = generation.id
    version = KnowledgeDocumentVersion(
        document_id=document.id, version_no=int(last_no) + 1,
        source_sha256=document.sha256 or "",
        parser_name="", parser_version="", pipeline_version="", status="building",
    )
    db.add(version)
    document.status = "queued"
    document.error_code = None
    document.error_msg = None
    db.add(document)
    await db.flush()
    job = await queue_ingest(
        db, knowledge_base=kb, document=document, version=version,
        generation_id=generation_id, activate=bool(generation_id),
    )
    await db.commit()
    await db.refresh(document)
    await db.refresh(job)
    return DocumentUploadOut(
        **KnowledgeDocumentOut.from_document(document).model_dump(),
        job=IngestionJobOut.model_validate(job),
    )


@router.get("/documents/{doc_id}/parsed-preview", response_model=ParsedPreviewOut)
async def parsed_preview(
    doc_id: int,
    offset: int = Query(0, ge=0),
    limit: int = Query(200, ge=1, le=1000),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _kb, document = await _get_document(db, doc_id, current_user, "read")
    version = None
    if document.active_version_id:
        version = await db.get(KnowledgeDocumentVersion, document.active_version_id)
    if version is None or not version.parsed_artifact_key:
        version = (await db.execute(
            select(KnowledgeDocumentVersion).where(
                KnowledgeDocumentVersion.document_id == document.id,
                KnowledgeDocumentVersion.parsed_artifact_key.is_not(None),
            ).order_by(KnowledgeDocumentVersion.version_no.desc()).limit(1)
        )).scalar_one_or_none()
    if version is None or not version.parsed_artifact_key:
        raise HTTPException(status_code=404, detail="尚无解析结果（文档可能仍在构建或构建失败）")
    path = storage.resolve_storage_key(version.parsed_artifact_key)
    data = storage.read_json_artifact(path)
    if not data:
        raise HTTPException(status_code=404, detail="解析产物缺失，请重新索引")
    elements = data.get("elements") or []
    sliced = elements[offset:offset + limit]
    return ParsedPreviewOut(
        document_id=document.id,
        filename=document.filename,
        title=data.get("title") or document.filename,
        source_type=data.get("source_type") or document.file_type,
        parser_name=data.get("parser_name") or version.parser_name,
        parser_version=data.get("parser_version") or version.parser_version,
        parse_stats=data.get("parse_stats") or {},
        warnings=data.get("warnings") or [],
        total_elements=len(elements),
        elements=sliced,
    )


@router.get("/documents/{doc_id}/chunks", response_model=ChunkPageOut)
async def list_chunks(
    doc_id: int,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    kb, document = await _get_document(db, doc_id, current_user, "read")
    query = select(KnowledgeChunk).where(
        KnowledgeChunk.document_id == document.id,
        KnowledgeChunk.is_parent.is_(False),
    )
    if document.active_version_id:
        query = query.where(KnowledgeChunk.document_version_id == document.active_version_id)
    total = (await db.execute(select(func.count()).select_from(query.subquery()))).scalar() or 0
    query = query.order_by(KnowledgeChunk.chunk_index).offset((page - 1) * page_size).limit(page_size)
    rows = (await db.execute(query)).scalars().all()
    return ChunkPageOut(
        total=total,
        items=[
            ChunkOut(
                chunk_id=row.id, document_id=row.document_id,
                document_version_id=row.document_version_id,
                chunk_index=row.chunk_index, content=row.content,
                token_count=row.token_count, is_parent=row.is_parent,
                parent_chunk_id=row.parent_chunk_id,
                heading_path=list(row.heading_path or []),
                element_type=row.element_type, page_start=row.page_start,
                page_end=row.page_end, sheet_name=row.sheet_name,
                row_start=row.row_start, row_end=row.row_end,
                slide_number=row.slide_number, table_index=row.table_index,
                content_hash=row.content_hash,
            )
            for row in rows
        ],
    )


# ── Web Crawl ────────────────────────────────────────────────────────────

@router.post("/bases/{kb_id}/crawl", response_model=DocumentUploadOut)
async def crawl_webpage(
    kb_id: int,
    body: WebCrawlRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    kb = await _get_base(db, kb_id, current_user, "crawl")
    try:
        page = await fetch_webpage_safely(body.url)
    except KnowledgeSecurityError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if not page.text.strip():
        raise HTTPException(status_code=400, detail="网页未提取到有效正文")
    display_name = storage.sanitize_filename(body.title or page.title or body.url[:80])
    document = KnowledgeDocument(
        knowledge_base_id=kb.id, filename=display_name, file_path="",
        file_type="txt", file_size=0, status="queued", mime_type="text/plain",
    )
    db.add(document)
    await db.flush()
    version = KnowledgeDocumentVersion(
        document_id=document.id, version_no=1, source_sha256="",
        parser_name="", parser_version="", pipeline_version="", status="building",
    )
    db.add(version)
    await db.flush()
    content = f"来源: {page.final_url}\n\n{page.text}"
    try:
        stored = storage.save_text_source(
            kb_id=kb.id, doc_id=document.id, version_no=1, text=content, ext="txt",
        )
    except OSError as exc:
        await db.rollback()
        raise HTTPException(status_code=500, detail=f"网页内容保存失败：{exc}")
    document.file_path = str(stored.path)
    document.storage_key = stored.storage_key
    document.file_size = stored.size
    document.sha256 = stored.sha256
    version.source_sha256 = stored.sha256
    kb.doc_count = (kb.doc_count or 0) + 1
    job = await queue_ingest(db, knowledge_base=kb, document=document, version=version)
    await db.commit()
    await db.refresh(document)
    await db.refresh(job)
    return DocumentUploadOut(
        **KnowledgeDocumentOut.from_document(document).model_dump(),
        job=IngestionJobOut.model_validate(job),
    )


# ── Rebuild ──────────────────────────────────────────────────────────────

@router.post("/bases/{kb_id}/rebuild")
async def rebuild_knowledge_base(
    kb_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    kb = await _get_base(db, kb_id, current_user, "reindex")
    _check_chunk_config(kb)
    generation = await create_rebuild_generation(db, kb)
    documents = (await db.execute(
        select(KnowledgeDocument).where(
            KnowledgeDocument.knowledge_base_id == kb.id,
            KnowledgeDocument.status.in_(("ready", "partial", "failed", "queued", "processing")),
        )
    )).scalars().all()
    jobs = []
    for document in documents:
        last_no = (await db.execute(
            select(func.max(KnowledgeDocumentVersion.version_no)).where(
                KnowledgeDocumentVersion.document_id == document.id
            )
        )).scalar_one_or_none() or 0
        version = KnowledgeDocumentVersion(
            document_id=document.id, version_no=int(last_no) + 1,
            source_sha256=document.sha256 or "",
            parser_name="", parser_version="", pipeline_version="", status="building",
        )
        db.add(version)
        await db.flush()
        document.status = "queued"
        db.add(document)
        job = await queue_ingest(
            db, knowledge_base=kb, document=document, version=version,
            generation_id=generation.id, activate=True,
        )
        jobs.append(job.id)
    await db.commit()
    return {"message": "已创建新索引代际", "generation_id": generation.id,
            "generation_no": generation.generation_no, "jobs": jobs}


# ── Search ───────────────────────────────────────────────────────────────

@router.post("/search", response_model=SearchResponse)
async def search_knowledge(
    body: SearchRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    kb_ids = await knowledge_acl.filter_authorized_ids(db, current_user, body.knowledge_base_ids)
    if body.knowledge_base_ids and not kb_ids:
        raise HTTPException(status_code=404, detail="知识库不存在")
    if not kb_ids:
        kb_ids = (await db.execute(
            select(KnowledgeBase.id).where(
                knowledge_acl.accessible_bases_filter(current_user),
                KnowledgeBase.is_active.is_(True),
            )
        )).scalars().all()
    service = KnowledgeRetrievalService(db)
    try:
        result = await service.search(
            knowledge_base_ids=[int(x) for x in kb_ids],
            query=body.query,
            options=RetrievalOptions(
                top_k=body.top_k, mode=body.mode, rerank=body.rerank,
                expand_context=body.expand_context, score_threshold=body.score_threshold,
            ),
        )
    except Exception as exc:  # noqa: BLE001
        from app.services.knowledge.embedding import EmbeddingError

        if isinstance(exc, EmbeddingError):
            raise HTTPException(status_code=503, detail=f"检索向量化不可用：{exc}")
        raise
    results = []
    for item in result.items:
        results.append(SearchResult(
            content=item.content,
            score=round(item.score, 6),
            document_id=int((item.citation or {}).get("document_id") or 0),
            filename=str((item.citation or {}).get("filename") or ""),
            chunk_index=int((item.chunk or {}).get("chunk_index") or 0),
            metadata={
                "heading_path": item.citation.get("heading_path"),
                "page_start": item.citation.get("page_start"),
                "page_end": item.citation.get("page_end"),
                "sheet_name": item.citation.get("sheet_name"),
                "slide_number": item.citation.get("slide_number"),
                "signals": {
                    "dense": item.dense_score, "bm25": item.bm25_score,
                    "rrf": item.rrf_score, "rerank": item.rerank_score,
                },
            },
            context=item.context,
            chunk_id=item.chunk_id,
            signals={
                "dense": item.dense_score, "bm25": item.bm25_score,
                "rrf": item.rrf_score, "rerank": item.rerank_score,
            },
            citation=item.citation,
        ))
    return SearchResponse(
        results=results, total=len(results), query=body.query, mode=result.mode,
        retrieval_id=result.retrieval_id, timings=result.timings,
        candidates=result.candidates, notes=result.notes, legacy_used=result.legacy_used,
    )


# ── Evaluation ───────────────────────────────────────────────────────────

@router.post("/bases/{kb_id}/evaluations", response_model=EvaluationOut)
async def create_evaluation(
    kb_id: int,
    body: EvaluationCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    kb = await _get_base(db, kb_id, current_user, "write")
    row = KnowledgeEvaluation(
        knowledge_base_id=kb.id, created_by=current_user.id, name=body.name,
        status="running", retrieval_mode=body.mode,
        embedding_model=kb.embedding_model,
        dataset=[case.model_dump() for case in body.cases],
    )
    db.add(row)
    await db.flush()
    cases = [EvaluationCase.from_dict(case.model_dump()) for case in body.cases]
    try:
        metrics = await evaluate(
            db, knowledge_base_ids=[kb.id], cases=cases,
            mode=body.mode, top_k=body.top_k, rerank=body.rerank,
        )
        row.status = "completed"
        row.metrics = {k: v for k, v in metrics.items() if k != "details"}
        row.details = {"cases": metrics.get("details") or []}
        row.finished_at = datetime.utcnow()
    except Exception as exc:  # noqa: BLE001
        logger.exception("知识库评测失败 kb=%s", kb_id)
        row.status = "failed"
        row.error_msg = str(exc)[:2000]
        row.finished_at = datetime.utcnow()
    db.add(row)
    await db.commit()
    await db.refresh(row)
    if row.status == "failed":
        return EvaluationOut.model_validate(row)
    return EvaluationOut.model_validate(row)


@router.get("/bases/{kb_id}/evaluations", response_model=list[EvaluationOut])
async def list_evaluations(
    kb_id: int,
    limit: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    await _get_base(db, kb_id, current_user, "read")
    rows = (await db.execute(
        select(KnowledgeEvaluation)
        .where(KnowledgeEvaluation.knowledge_base_id == kb_id)
        .order_by(KnowledgeEvaluation.id.desc()).limit(limit)
    )).scalars().all()
    return [EvaluationOut.model_validate(row) for row in rows]


@router.get("/evaluations/{evaluation_id}", response_model=EvaluationOut)
async def get_evaluation(
    evaluation_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    row = await db.get(KnowledgeEvaluation, evaluation_id)
    if row is None:
        raise HTTPException(status_code=404, detail="评测不存在")
    await _get_base(db, row.knowledge_base_id, current_user, "read")
    return EvaluationOut.model_validate(row)
