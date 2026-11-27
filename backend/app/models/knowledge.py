"""知识库领域模型（Knowledge Ingestion & RAG）。

保留旧字段以兼容存量数据与旧检索回滚链路：
- ``collection_name`` / ``chunk_size`` / ``chunk_overlap`` 仅供旧 SQLite 词法
  检索使用；
- ``vector_count`` 语义修正为"真实向量点数"（Qdrant Point 数），
  Chunk 数由 ``indexed_chunk_count`` 表达。

新增表：
- ``knowledge_document_versions``：文档版本（同名文件更新不覆盖旧版本）；
- ``knowledge_ingestion_jobs``：Durable Ingestion Job（Lease/Heartbeat/Retry）；
- ``knowledge_index_generations``：索引代际（Embedding 模型/维度/Chunker 版本）；
- ``knowledge_chunks``：结构化 Chunk（BM25 + Parent/Neighbor 扩展的权威来源）；
- ``knowledge_evaluations``：检索质量评测结果。
"""
from datetime import datetime
from typing import Optional

from sqlalchemy import (
    JSON, Boolean, DateTime, Float, ForeignKey, Index, Integer, String, Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import BaseModel


class KnowledgeBase(BaseModel):
    __tablename__ = "knowledge_bases"

    name: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=True)
    embedding_model: Mapped[str] = mapped_column(String(100), nullable=True)
    chunk_size: Mapped[int] = mapped_column(Integer, default=500)
    chunk_overlap: Mapped[int] = mapped_column(Integer, default=50)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    owner_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    doc_count: Mapped[int] = mapped_column(Integer, default=0)
    # 真实向量点数（Qdrant Point 数）；旧数据为历史 Chunk 数，迁移后重算
    vector_count: Mapped[int] = mapped_column(Integer, default=0)
    # 旧 SQLite 词法检索 collection（回滚链路使用）
    collection_name: Mapped[str] = mapped_column(String(200), nullable=True)

    # ── RAG 扩展 ──────────────────────────────────────────────────────
    project_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("projects.id"), nullable=True, index=True,
    )
    visibility: Mapped[str] = mapped_column(String(16), default="private", nullable=False)
    status: Mapped[str] = mapped_column(String(24), default="active", nullable=False, index=True)
    active_index_generation_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    embedding_dimension: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    indexed_chunk_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    failed_doc_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    parser_profile: Mapped[str] = mapped_column(String(32), default="auto", nullable=False)
    chunk_size_tokens: Mapped[int] = mapped_column(Integer, default=512, nullable=False)
    chunk_overlap_tokens: Mapped[int] = mapped_column(Integer, default=64, nullable=False)
    last_ingested_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    documents: Mapped[list["KnowledgeDocument"]] = relationship(
        "KnowledgeDocument", back_populates="knowledge_base",
        cascade="all, delete-orphan", passive_deletes=True,
    )


class KnowledgeDocument(BaseModel):
    __tablename__ = "knowledge_documents"

    __table_args__ = (
        Index("ix_knowledge_docs_kb_status", "knowledge_base_id", "status"),
    )

    knowledge_base_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("knowledge_bases.id"), nullable=False, index=True,
    )
    filename: Mapped[str] = mapped_column(String(500), nullable=False)
    file_path: Mapped[str] = mapped_column(String(1000), nullable=False)
    file_type: Mapped[str] = mapped_column(String(50), nullable=False)
    file_size: Mapped[int] = mapped_column(Integer, default=0)
    # 状态：pending / queued / processing / ready / partial / failed
    #      / deleting / deleted / migration_required
    status: Mapped[str] = mapped_column(String(24), default="pending", nullable=False, index=True)
    chunk_count: Mapped[int] = mapped_column(Integer, default=0)
    error_msg: Mapped[str] = mapped_column(Text, nullable=True)
    meta_info: Mapped[dict] = mapped_column(JSON, nullable=True)

    # ── RAG 扩展 ──────────────────────────────────────────────────────
    storage_key: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    mime_type: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    sha256: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    active_version_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    page_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    sheet_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    slide_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    element_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    parse_stats_json: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    warning_json: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    error_code: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)

    knowledge_base: Mapped["KnowledgeBase"] = relationship(
        "KnowledgeBase", back_populates="documents",
    )


class KnowledgeDocumentVersion(BaseModel):
    __tablename__ = "knowledge_document_versions"

    __table_args__ = (
        UniqueConstraint("document_id", "version_no", name="uq_knowledge_doc_version"),
        Index("ix_knowledge_doc_versions_doc", "document_id", "version_no"),
    )

    document_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("knowledge_documents.id"), nullable=False, index=True,
    )
    version_no: Mapped[int] = mapped_column(Integer, nullable=False)
    source_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    parser_name: Mapped[str] = mapped_column(String(64), default="native", nullable=False)
    parser_version: Mapped[str] = mapped_column(String(32), default="1", nullable=False)
    pipeline_version: Mapped[str] = mapped_column(String(32), default="1", nullable=False)
    parsed_artifact_key: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    index_generation_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    # 状态：building / ready / partial / failed / deleted
    status: Mapped[str] = mapped_column(String(24), default="building", nullable=False)
    options_json: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    error_msg: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


class KnowledgeIngestionJob(BaseModel):
    __tablename__ = "knowledge_ingestion_jobs"

    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_knowledge_job_idem"),
        Index("ix_knowledge_jobs_status_created", "status", "created_at"),
        Index("ix_knowledge_jobs_lease", "status", "lease_expires_at"),
    )

    knowledge_base_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    document_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
    document_version_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    job_type: Mapped[str] = mapped_column(String(32), default="ingest", nullable=False)
    # 阶段：detecting / parsing / ocr / normalizing / chunking / embedding
    #       / indexing / validating / ready / failed / cancelled
    stage: Mapped[str] = mapped_column(String(32), default="queued", nullable=False)
    status: Mapped[str] = mapped_column(String(24), default="queued", nullable=False, index=True)
    progress: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    attempt: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)
    lease_owner: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    lease_token: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    lease_expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    heartbeat_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    error_code: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    payload: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)


class KnowledgeIndexGeneration(BaseModel):
    __tablename__ = "knowledge_index_generations"

    __table_args__ = (
        UniqueConstraint("knowledge_base_id", "generation_no", name="uq_knowledge_generation_no"),
        Index("ix_knowledge_generations_kb_status", "knowledge_base_id", "status"),
    )

    knowledge_base_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    generation_no: Mapped[int] = mapped_column(Integer, nullable=False)
    embedding_model: Mapped[str] = mapped_column(String(160), nullable=False)
    embedding_dimension: Mapped[int] = mapped_column(Integer, nullable=False)
    chunker_version: Mapped[str] = mapped_column(String(32), nullable=False)
    parser_version: Mapped[str] = mapped_column(String(32), nullable=False)
    collection_name: Mapped[str] = mapped_column(String(200), nullable=False)
    # building / active / retired / failed
    status: Mapped[str] = mapped_column(String(24), default="building", nullable=False)
    point_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    activated_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)


class KnowledgeChunk(BaseModel):
    """结构化 Chunk：BM25 检索、父子扩展与引用的权威来源。

    ``id`` 为稳定内容 ID（KB + Document + Version + Generation + 元素 + 序号的
    摘要），重复 Job 不会产生重复行。
    """

    __tablename__ = "knowledge_chunks"

    __table_args__ = (
        Index("ix_knowledge_chunks_kb_gen", "knowledge_base_id", "index_generation_id"),
        Index("ix_knowledge_chunks_doc", "document_id", "index_generation_id"),
        Index("ix_knowledge_chunks_hash", "knowledge_base_id", "content_hash"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    knowledge_base_id: Mapped[int] = mapped_column(Integer, nullable=False)
    document_id: Mapped[int] = mapped_column(Integer, nullable=False)
    document_version_id: Mapped[int] = mapped_column(Integer, nullable=False)
    index_generation_id: Mapped[int] = mapped_column(Integer, nullable=False)
    parent_chunk_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    chunk_index: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    token_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    filename: Mapped[str] = mapped_column(String(500), nullable=False)
    file_type: Mapped[str] = mapped_column(String(50), default="", nullable=False)
    title: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    heading_path: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)
    element_type: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    page_start: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    page_end: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    sheet_name: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    row_start: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    row_end: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    slide_number: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    table_index: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    source_element_ids: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)
    parser_version: Mapped[str] = mapped_column(String(32), default="1", nullable=False)
    chunker_version: Mapped[str] = mapped_column(String(32), default="1", nullable=False)
    is_parent: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


class KnowledgeEvaluation(BaseModel):
    __tablename__ = "knowledge_evaluations"

    knowledge_base_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    created_by: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    name: Mapped[str] = mapped_column(String(200), default="", nullable=False)
    status: Mapped[str] = mapped_column(String(24), default="running", nullable=False)
    retrieval_mode: Mapped[str] = mapped_column(String(24), default="hybrid", nullable=False)
    embedding_model: Mapped[Optional[str]] = mapped_column(String(160), nullable=True)
    dataset: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)
    metrics: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    details: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    error_msg: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
