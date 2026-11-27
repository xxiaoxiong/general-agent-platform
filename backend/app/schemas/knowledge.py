"""知识库 API Schema（Knowledge Ingestion & RAG）。"""
from datetime import datetime
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field, model_validator


class KnowledgeBaseCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    description: Optional[str] = None
    embedding_model: Optional[str] = None
    # 旧字段（字符级，仅旧检索回退链路使用）
    chunk_size: int = 500
    chunk_overlap: int = 50
    # 新字段（Token 级，结构感知 Chunking）
    chunk_size_tokens: int = 512
    chunk_overlap_tokens: int = 64
    parser_profile: Literal["auto", "docling", "native"] = "auto"
    project_id: Optional[int] = None
    visibility: Literal["private", "project", "organization"] = "private"

    @model_validator(mode="after")
    def _validate_chunks(self):
        if self.chunk_size_tokens <= 0:
            raise ValueError("chunk_size_tokens 必须大于 0")
        if not 0 <= self.chunk_overlap_tokens < self.chunk_size_tokens:
            raise ValueError("chunk_overlap_tokens 必须 >= 0 且小于 chunk_size_tokens")
        if self.chunk_size <= 0 or not 0 <= self.chunk_overlap < self.chunk_size:
            raise ValueError("chunk_size / chunk_overlap 非法（overlap 必须小于 size）")
        if self.visibility == "project" and not self.project_id:
            raise ValueError("visibility=project 时必须提供 project_id")
        return self


class KnowledgeBaseUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    embedding_model: Optional[str] = None
    chunk_size: Optional[int] = None
    chunk_overlap: Optional[int] = None
    chunk_size_tokens: Optional[int] = None
    chunk_overlap_tokens: Optional[int] = None
    parser_profile: Optional[Literal["auto", "docling", "native"]] = None
    visibility: Optional[Literal["private", "project", "organization"]] = None
    project_id: Optional[int] = None
    is_active: Optional[bool] = None

    @model_validator(mode="after")
    def _validate_chunks(self):
        # 只在同时提供两项时做相对校验；单项更新由 API 结合库内现值复核
        if self.chunk_size_tokens is not None and self.chunk_overlap_tokens is not None:
            if self.chunk_size_tokens <= 0 or self.chunk_overlap_tokens < 0 \
                    or self.chunk_overlap_tokens >= self.chunk_size_tokens:
                raise ValueError("chunk_size_tokens / chunk_overlap_tokens 非法（overlap 必须小于 size）")
        if self.chunk_size is not None and self.chunk_overlap is not None:
            if self.chunk_size <= 0 or self.chunk_overlap < 0 or self.chunk_overlap >= self.chunk_size:
                raise ValueError("chunk_size / chunk_overlap 非法（overlap 必须小于 size）")
        return self


class KnowledgeBaseOut(BaseModel):
    id: int
    name: str
    description: Optional[str] = None
    embedding_model: Optional[str] = None
    chunk_size: int
    chunk_overlap: int
    is_active: bool
    owner_id: int
    doc_count: int
    vector_count: int
    indexed_chunk_count: int = 0
    failed_doc_count: int = 0
    project_id: Optional[int] = None
    visibility: str = "private"
    status: str = "active"
    active_index_generation_id: Optional[int] = None
    embedding_dimension: int = 0
    parser_profile: str = "auto"
    chunk_size_tokens: int = 512
    chunk_overlap_tokens: int = 64
    last_ingested_at: Optional[datetime] = None
    created_at: datetime
    model_config = {"from_attributes": True}


class IngestionJobOut(BaseModel):
    id: int
    knowledge_base_id: int
    document_id: Optional[int] = None
    document_version_id: Optional[int] = None
    job_type: str
    stage: str
    status: str
    progress: float
    attempt: int
    max_attempts: int
    error_code: Optional[str] = None
    error_message: Optional[str] = None
    created_at: Optional[datetime] = None
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    model_config = {"from_attributes": True}


class KnowledgeDocumentOut(BaseModel):
    id: int
    knowledge_base_id: int
    filename: str
    file_type: str
    file_size: int
    status: str
    chunk_count: int
    error_code: Optional[str] = None
    error_msg: Optional[str] = None
    mime_type: Optional[str] = None
    page_count: int = 0
    sheet_count: int = 0
    slide_count: int = 0
    element_count: int = 0
    parse_stats: Optional[Dict[str, Any]] = None
    warnings: Optional[List[str]] = None
    active_version_id: Optional[int] = None
    created_at: datetime
    updated_at: Optional[datetime] = None
    model_config = {"from_attributes": True}

    @classmethod
    def from_document(cls, document) -> "KnowledgeDocumentOut":
        warnings = (document.warning_json or {}).get("warnings") or []
        return cls(
            id=document.id,
            knowledge_base_id=document.knowledge_base_id,
            filename=document.filename,
            file_type=document.file_type,
            file_size=document.file_size,
            status=document.status,
            chunk_count=document.chunk_count,
            error_code=document.error_code,
            error_msg=document.error_msg,
            mime_type=document.mime_type,
            page_count=document.page_count or 0,
            sheet_count=document.sheet_count or 0,
            slide_count=document.slide_count or 0,
            element_count=document.element_count or 0,
            parse_stats=document.parse_stats_json,
            warnings=list(warnings),
            active_version_id=document.active_version_id,
            created_at=document.created_at,
            updated_at=document.updated_at,
        )


class DocumentUploadOut(KnowledgeDocumentOut):
    job: Optional[IngestionJobOut] = None


class ParsedElementOut(BaseModel):
    element_id: str
    type: str
    text: str
    heading_path: List[str] = []
    provenance: Dict[str, Any] = {}
    level: Optional[int] = None
    table: Optional[Dict[str, Any]] = None
    ocr_score: Optional[float] = None


class ParsedPreviewOut(BaseModel):
    document_id: int
    filename: str
    title: str
    source_type: str
    parser_name: str
    parser_version: str
    parse_stats: Dict[str, Any] = {}
    warnings: List[str] = []
    total_elements: int
    elements: List[ParsedElementOut]


class ChunkOut(BaseModel):
    chunk_id: str
    document_id: int
    document_version_id: int
    chunk_index: int
    content: str
    token_count: int
    is_parent: bool
    parent_chunk_id: Optional[str] = None
    heading_path: List[str] = []
    element_type: Optional[str] = None
    page_start: Optional[int] = None
    page_end: Optional[int] = None
    sheet_name: Optional[str] = None
    row_start: Optional[int] = None
    row_end: Optional[int] = None
    slide_number: Optional[int] = None
    table_index: Optional[int] = None
    content_hash: str


class ChunkPageOut(BaseModel):
    total: int
    items: List[ChunkOut]


class SearchRequest(BaseModel):
    query: str = Field(min_length=1)
    knowledge_base_ids: List[int] = []
    top_k: int = Field(default=5, ge=1, le=50)
    mode: Literal["dense", "bm25", "hybrid"] = "hybrid"
    rerank: Optional[bool] = None
    score_threshold: Optional[float] = None
    expand_context: bool = True


class SearchResult(BaseModel):
    # 兼容旧字段
    content: str
    score: float
    document_id: int
    filename: str
    chunk_index: int
    metadata: Optional[Dict[str, Any]] = None
    # 新字段
    context: Optional[str] = None
    chunk_id: Optional[str] = None
    signals: Optional[Dict[str, Any]] = None
    citation: Optional[Dict[str, Any]] = None


class SearchResponse(BaseModel):
    results: List[SearchResult]
    total: int
    query: str
    mode: str = "hybrid"
    retrieval_id: Optional[str] = None
    timings: Dict[str, float] = {}
    candidates: Dict[str, int] = {}
    notes: List[str] = []
    legacy_used: bool = False


class WebCrawlRequest(BaseModel):
    url: str
    title: Optional[str] = None


class ReindexRequest(BaseModel):
    new_generation: bool = False


class EvaluationCaseIn(BaseModel):
    question: str
    expected_document_ids: List[int] = []
    expected_pages: List[int] = []
    relevant_chunk_ids: List[str] = []
    expected_answer_points: List[str] = []
    no_answer: bool = False


class EvaluationCreate(BaseModel):
    name: str = ""
    cases: List[EvaluationCaseIn] = Field(min_length=1)
    mode: Literal["dense", "bm25", "hybrid"] = "hybrid"
    top_k: int = Field(default=5, ge=1, le=20)
    rerank: Optional[bool] = None


class EvaluationOut(BaseModel):
    id: int
    knowledge_base_id: int
    name: str
    status: str
    retrieval_mode: str
    embedding_model: Optional[str] = None
    metrics: Optional[Dict[str, Any]] = None
    details: Optional[Dict[str, Any]] = None
    error_msg: Optional[str] = None
    created_at: datetime
    finished_at: Optional[datetime] = None
    model_config = {"from_attributes": True}
