"""Knowledge Ingestion & RAG Runtime：版本 / Job / Generation / Chunk / 评测表。

Revision ID: c3d4e5f6a7b8
Revises: f0a1b2c3d4e5
Create Date: 2026-11-27

说明：
- 已有 `knowledge_bases` / `knowledge_documents` 只新增可空/带默认值列，
  存量数据不受影响；
- 新增 5 张表（版本 / Job / Generation / Chunk / 评测）；
- 旧 `status='done'` 归一化为 `ready`；`indexed_chunk_count` 由历史
  `chunk_count` 汇总，`vector_count` 置 0（旧 SQLite Chunk 不是真实向量，
  不能继续冒充向量数；重新索引后由 Generation 统计回填）。
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "c3d4e5f6a7b8"
down_revision: Union[str, None] = "f0a1b2c3d4e5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _tables() -> set:
    return set(sa.inspect(op.get_bind()).get_table_names())


def _columns(table: str) -> set:
    inspector = sa.inspect(op.get_bind())
    if table not in inspector.get_table_names():
        return set()
    return {c["name"] for c in inspector.get_columns(table)}


def _indexes(table: str) -> set:
    inspector = sa.inspect(op.get_bind())
    if table not in inspector.get_table_names():
        return set()
    return {i["name"] for i in inspector.get_indexes(table)}


def _add_column(table: str, column: sa.Column) -> None:
    if table in _tables() and column.name not in _columns(table):
        op.add_column(table, column)


def upgrade() -> None:
    tables = _tables()

    # ── knowledge_bases 扩展 ──────────────────────────────────────────
    _add_column("knowledge_bases", sa.Column("project_id", sa.Integer(), nullable=True))
    _add_column("knowledge_bases", sa.Column("visibility", sa.String(16), nullable=False,
                                             server_default="private"))
    _add_column("knowledge_bases", sa.Column("status", sa.String(24), nullable=False,
                                             server_default="active"))
    _add_column("knowledge_bases", sa.Column("active_index_generation_id", sa.Integer(), nullable=True))
    _add_column("knowledge_bases", sa.Column("embedding_dimension", sa.Integer(), nullable=False,
                                             server_default="0"))
    _add_column("knowledge_bases", sa.Column("indexed_chunk_count", sa.Integer(), nullable=False,
                                             server_default="0"))
    _add_column("knowledge_bases", sa.Column("failed_doc_count", sa.Integer(), nullable=False,
                                             server_default="0"))
    _add_column("knowledge_bases", sa.Column("parser_profile", sa.String(32), nullable=False,
                                             server_default="auto"))
    _add_column("knowledge_bases", sa.Column("chunk_size_tokens", sa.Integer(), nullable=False,
                                             server_default="512"))
    _add_column("knowledge_bases", sa.Column("chunk_overlap_tokens", sa.Integer(), nullable=False,
                                             server_default="64"))
    _add_column("knowledge_bases", sa.Column("last_ingested_at", sa.DateTime(), nullable=True))
    if "ix_knowledge_bases_project_id" not in _indexes("knowledge_bases"):
        op.create_index("ix_knowledge_bases_project_id", "knowledge_bases", ["project_id"])
    if "ix_knowledge_bases_status" not in _indexes("knowledge_bases"):
        op.create_index("ix_knowledge_bases_status", "knowledge_bases", ["status"])
    if "ix_knowledge_bases_owner_id" not in _indexes("knowledge_bases"):
        op.create_index("ix_knowledge_bases_owner_id", "knowledge_bases", ["owner_id"])

    # ── agents：多知识库授权绑定（兼容旧 knowledge_base_id）────────────
    _add_column("agents", sa.Column("knowledge_base_ids", sa.JSON(), nullable=True))

    # ── knowledge_documents 扩展 ──────────────────────────────────────
    _add_column("knowledge_documents", sa.Column("storage_key", sa.String(500), nullable=True))
    _add_column("knowledge_documents", sa.Column("mime_type", sa.String(120), nullable=True))
    _add_column("knowledge_documents", sa.Column("sha256", sa.String(64), nullable=True))
    _add_column("knowledge_documents", sa.Column("active_version_id", sa.Integer(), nullable=True))
    _add_column("knowledge_documents", sa.Column("page_count", sa.Integer(), nullable=False,
                                                 server_default="0"))
    _add_column("knowledge_documents", sa.Column("sheet_count", sa.Integer(), nullable=False,
                                                 server_default="0"))
    _add_column("knowledge_documents", sa.Column("slide_count", sa.Integer(), nullable=False,
                                                 server_default="0"))
    _add_column("knowledge_documents", sa.Column("element_count", sa.Integer(), nullable=False,
                                                 server_default="0"))
    _add_column("knowledge_documents", sa.Column("parse_stats_json", sa.JSON(), nullable=True))
    _add_column("knowledge_documents", sa.Column("warning_json", sa.JSON(), nullable=True))
    _add_column("knowledge_documents", sa.Column("error_code", sa.String(64), nullable=True))
    if "ix_knowledge_documents_sha256" not in _indexes("knowledge_documents"):
        op.create_index("ix_knowledge_documents_sha256", "knowledge_documents", ["sha256"])
    if "ix_knowledge_documents_status" not in _indexes("knowledge_documents"):
        op.create_index("ix_knowledge_documents_status", "knowledge_documents", ["status"])
    if "ix_knowledge_docs_kb_status" not in _indexes("knowledge_documents"):
        op.create_index("ix_knowledge_docs_kb_status", "knowledge_documents",
                        ["knowledge_base_id", "status"])

    # ── 新表 ───────────────────────────────────────────────────────────
    if "knowledge_document_versions" not in tables:
        op.create_table(
            "knowledge_document_versions",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("document_id", sa.Integer(), sa.ForeignKey("knowledge_documents.id"),
                      nullable=False),
            sa.Column("version_no", sa.Integer(), nullable=False),
            sa.Column("source_sha256", sa.String(64), nullable=False),
            sa.Column("parser_name", sa.String(64), nullable=False, server_default="native"),
            sa.Column("parser_version", sa.String(32), nullable=False, server_default="1"),
            sa.Column("pipeline_version", sa.String(32), nullable=False, server_default="1"),
            sa.Column("parsed_artifact_key", sa.String(500), nullable=True),
            sa.Column("index_generation_id", sa.Integer(), nullable=True),
            sa.Column("status", sa.String(24), nullable=False, server_default="building"),
            sa.Column("options_json", sa.JSON(), nullable=True),
            sa.Column("error_msg", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
            sa.UniqueConstraint("document_id", "version_no", name="uq_knowledge_doc_version"),
        )
        op.create_index("ix_knowledge_document_versions_document_id",
                        "knowledge_document_versions", ["document_id"])
        op.create_index("ix_knowledge_doc_versions_doc", "knowledge_document_versions",
                        ["document_id", "version_no"])

    if "knowledge_ingestion_jobs" not in tables:
        op.create_table(
            "knowledge_ingestion_jobs",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("knowledge_base_id", sa.Integer(), nullable=False),
            sa.Column("document_id", sa.Integer(), nullable=True),
            sa.Column("document_version_id", sa.Integer(), nullable=True),
            sa.Column("job_type", sa.String(32), nullable=False, server_default="ingest"),
            sa.Column("stage", sa.String(32), nullable=False, server_default="queued"),
            sa.Column("status", sa.String(24), nullable=False, server_default="queued"),
            sa.Column("progress", sa.Float(), nullable=False, server_default="0"),
            sa.Column("attempt", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="3"),
            sa.Column("idempotency_key", sa.String(200), nullable=False),
            sa.Column("lease_owner", sa.String(128), nullable=True),
            sa.Column("lease_token", sa.String(64), nullable=True),
            sa.Column("lease_expires_at", sa.DateTime(), nullable=True),
            sa.Column("heartbeat_at", sa.DateTime(), nullable=True),
            sa.Column("error_code", sa.String(64), nullable=True),
            sa.Column("error_message", sa.Text(), nullable=True),
            sa.Column("payload", sa.JSON(), nullable=True),
            sa.Column("started_at", sa.DateTime(), nullable=True),
            sa.Column("finished_at", sa.DateTime(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
            sa.UniqueConstraint("idempotency_key", name="uq_knowledge_job_idem"),
        )
        op.create_index("ix_knowledge_ingestion_jobs_knowledge_base_id",
                        "knowledge_ingestion_jobs", ["knowledge_base_id"])
        op.create_index("ix_knowledge_ingestion_jobs_document_id",
                        "knowledge_ingestion_jobs", ["document_id"])
        op.create_index("ix_knowledge_ingestion_jobs_status",
                        "knowledge_ingestion_jobs", ["status"])
        op.create_index("ix_knowledge_jobs_status_created", "knowledge_ingestion_jobs",
                        ["status", "created_at"])
        op.create_index("ix_knowledge_jobs_lease", "knowledge_ingestion_jobs",
                        ["status", "lease_expires_at"])

    if "knowledge_index_generations" not in tables:
        op.create_table(
            "knowledge_index_generations",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("knowledge_base_id", sa.Integer(), nullable=False),
            sa.Column("generation_no", sa.Integer(), nullable=False),
            sa.Column("embedding_model", sa.String(160), nullable=False),
            sa.Column("embedding_dimension", sa.Integer(), nullable=False),
            sa.Column("chunker_version", sa.String(32), nullable=False),
            sa.Column("parser_version", sa.String(32), nullable=False),
            sa.Column("collection_name", sa.String(200), nullable=False),
            sa.Column("status", sa.String(24), nullable=False, server_default="building"),
            sa.Column("point_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("activated_at", sa.DateTime(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
            sa.UniqueConstraint("knowledge_base_id", "generation_no",
                                name="uq_knowledge_generation_no"),
        )
        op.create_index("ix_knowledge_index_generations_knowledge_base_id",
                        "knowledge_index_generations", ["knowledge_base_id"])
        op.create_index("ix_knowledge_generations_kb_status", "knowledge_index_generations",
                        ["knowledge_base_id", "status"])

    if "knowledge_chunks" not in tables:
        op.create_table(
            "knowledge_chunks",
            sa.Column("id", sa.String(64), primary_key=True),
            sa.Column("knowledge_base_id", sa.Integer(), nullable=False),
            sa.Column("document_id", sa.Integer(), nullable=False),
            sa.Column("document_version_id", sa.Integer(), nullable=False),
            sa.Column("index_generation_id", sa.Integer(), nullable=False),
            sa.Column("parent_chunk_id", sa.String(64), nullable=True),
            sa.Column("chunk_index", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("content", sa.Text(), nullable=False),
            sa.Column("content_hash", sa.String(64), nullable=False),
            sa.Column("token_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("filename", sa.String(500), nullable=False),
            sa.Column("file_type", sa.String(50), nullable=False, server_default=""),
            sa.Column("title", sa.String(500), nullable=True),
            sa.Column("heading_path", sa.JSON(), nullable=True),
            sa.Column("element_type", sa.String(32), nullable=True),
            sa.Column("page_start", sa.Integer(), nullable=True),
            sa.Column("page_end", sa.Integer(), nullable=True),
            sa.Column("sheet_name", sa.String(200), nullable=True),
            sa.Column("row_start", sa.Integer(), nullable=True),
            sa.Column("row_end", sa.Integer(), nullable=True),
            sa.Column("slide_number", sa.Integer(), nullable=True),
            sa.Column("table_index", sa.Integer(), nullable=True),
            sa.Column("source_element_ids", sa.JSON(), nullable=True),
            sa.Column("parser_version", sa.String(32), nullable=False, server_default="1"),
            sa.Column("chunker_version", sa.String(32), nullable=False, server_default="1"),
            sa.Column("is_parent", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        )
        op.create_index("ix_knowledge_chunks_knowledge_base_id", "knowledge_chunks",
                        ["knowledge_base_id"])
        op.create_index("ix_knowledge_chunks_document_id", "knowledge_chunks", ["document_id"])
        op.create_index("ix_knowledge_chunks_parent_chunk_id", "knowledge_chunks",
                        ["parent_chunk_id"])
        op.create_index("ix_knowledge_chunks_kb_gen", "knowledge_chunks",
                        ["knowledge_base_id", "index_generation_id"])
        op.create_index("ix_knowledge_chunks_doc", "knowledge_chunks",
                        ["document_id", "index_generation_id"])
        op.create_index("ix_knowledge_chunks_hash", "knowledge_chunks",
                        ["knowledge_base_id", "content_hash"])

    if "knowledge_evaluations" not in tables:
        op.create_table(
            "knowledge_evaluations",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("knowledge_base_id", sa.Integer(), nullable=False),
            sa.Column("created_by", sa.Integer(), nullable=True),
            sa.Column("name", sa.String(200), nullable=False, server_default=""),
            sa.Column("status", sa.String(24), nullable=False, server_default="running"),
            sa.Column("retrieval_mode", sa.String(24), nullable=False, server_default="hybrid"),
            sa.Column("embedding_model", sa.String(160), nullable=True),
            sa.Column("dataset", sa.JSON(), nullable=True),
            sa.Column("metrics", sa.JSON(), nullable=True),
            sa.Column("details", sa.JSON(), nullable=True),
            sa.Column("error_msg", sa.Text(), nullable=True),
            sa.Column("finished_at", sa.DateTime(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        )
        op.create_index("ix_knowledge_evaluations_knowledge_base_id",
                        "knowledge_evaluations", ["knowledge_base_id"])

    # ── 存量数据归一化 ─────────────────────────────────────────────────
    bind = op.get_bind()
    if "knowledge_documents" in tables:
        op.execute(sa.text(
            "UPDATE knowledge_documents SET status='ready' WHERE status='done'"
        ))
        op.execute(sa.text(
            "UPDATE knowledge_bases SET indexed_chunk_count = ("
            "  SELECT COALESCE(SUM(chunk_count), 0) FROM knowledge_documents d"
            "  WHERE d.knowledge_base_id = knowledge_bases.id"
            ") WHERE indexed_chunk_count = 0"
        ))
        # 旧 vector_count 是 Chunk 数，不是真实向量数：置 0，重新索引后按 Generation 回填
        op.execute(sa.text("UPDATE knowledge_bases SET vector_count = 0"))


def downgrade() -> None:
    # SQLite 不允许删除被索引引用的列：先移除本迁移新增的索引
    for table, indexes in {
        "knowledge_documents": (
            "ix_knowledge_documents_sha256", "ix_knowledge_documents_status",
            "ix_knowledge_docs_kb_status",
        ),
        "knowledge_bases": (
            "ix_knowledge_bases_project_id", "ix_knowledge_bases_status",
            "ix_knowledge_bases_owner_id",
        ),
    }.items():
        existing = _indexes(table)
        for index in indexes:
            if index in existing:
                op.drop_index(index, table_name=table)
    for table in (
        "knowledge_evaluations", "knowledge_chunks", "knowledge_index_generations",
        "knowledge_ingestion_jobs", "knowledge_document_versions",
    ):
        if table in _tables():
            op.drop_table(table)
    for table, columns in {
        "knowledge_documents": (
            "storage_key", "mime_type", "sha256", "active_version_id", "page_count",
            "sheet_count", "slide_count", "element_count", "parse_stats_json",
            "warning_json", "error_code",
        ),
        "knowledge_bases": (
            "project_id", "visibility", "status", "active_index_generation_id",
            "embedding_dimension", "indexed_chunk_count", "failed_doc_count",
            "parser_profile", "chunk_size_tokens", "chunk_overlap_tokens",
            "last_ingested_at",
        ),
        "agents": ("knowledge_base_ids",),
    }.items():
        existing = _columns(table)
        for column in columns:
            if column in existing:
                op.drop_column(table, column)
