"""Conversation Memory：历史会话索引、索引 Job 与记忆设置表。

Revision ID: b6c7d8e9f0a1
Revises: f9a1b2c3d4e5
Create Date: 2026-11-27
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "b6c7d8e9f0a1"
down_revision = "f9a1b2c3d4e5"
branch_labels = None
depends_on = None


def _has_table(name: str) -> bool:
    return name in sa.inspect(op.get_bind()).get_table_names()


def _has_index(table: str, name: str) -> bool:
    try:
        return any(item.get("name") == name for item in sa.inspect(op.get_bind()).get_indexes(table))
    except Exception:  # noqa: BLE001 表不存在/方言差异
        return False


def upgrade() -> None:
    if not _has_table("conversation_index_entries"):
        op.create_table(
            "conversation_index_entries",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.Column("entry_key", sa.String(length=160), nullable=False),
            sa.Column("session_id", sa.Integer(), nullable=False),
            sa.Column("user_id", sa.Integer(), nullable=False),
            sa.Column("agent_id", sa.Integer(), nullable=True),
            sa.Column("project_id", sa.Integer(), nullable=True),
            sa.Column("entry_type", sa.String(length=16), nullable=False, server_default="turn"),
            sa.Column("message_ids", sa.JSON(), nullable=False),
            sa.Column("role", sa.String(length=16), nullable=False, server_default="turn"),
            sa.Column("content", sa.Text(), nullable=False),
            sa.Column("content_hash", sa.String(length=64), nullable=False),
            sa.Column("token_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("message_created_at", sa.DateTime(), nullable=True),
            sa.Column("index_version", sa.String(length=80), nullable=False, server_default=""),
            sa.Column("visibility_scope", sa.String(length=16), nullable=False, server_default="user"),
            sa.Column("is_deleted", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("is_excluded", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("embedding_status", sa.String(length=16), nullable=False, server_default="pending"),
            sa.Column("embedding_model", sa.String(length=160), nullable=False, server_default=""),
            sa.Column("embedding_dimension", sa.Integer(), nullable=False, server_default="0"),
            sa.UniqueConstraint("session_id", "entry_key", name="uq_conversation_index_entry"),
        )
        op.create_index("ix_conversation_index_entries_session_id", "conversation_index_entries",
                        ["session_id"])
        op.create_index("ix_conversation_index_entries_content_hash", "conversation_index_entries",
                        ["content_hash"])
        op.create_index("ix_conversation_index_owner", "conversation_index_entries",
                        ["user_id", "is_deleted", "is_excluded"])
        op.create_index("ix_conversation_index_session", "conversation_index_entries",
                        ["session_id", "is_deleted"])
        op.create_index("ix_conversation_index_project", "conversation_index_entries",
                        ["project_id", "visibility_scope", "is_deleted"])
        op.create_index("ix_conversation_index_created", "conversation_index_entries",
                        ["user_id", "message_created_at"])
        op.create_index("ix_conversation_index_version", "conversation_index_entries",
                        ["index_version", "embedding_status"])

    if not _has_table("conversation_index_jobs"):
        op.create_table(
            "conversation_index_jobs",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.Column("job_key", sa.String(length=200), nullable=False),
            sa.Column("job_type", sa.String(length=32), nullable=False, server_default="index"),
            sa.Column("session_id", sa.Integer(), nullable=True),
            sa.Column("user_id", sa.Integer(), nullable=True),
            sa.Column("message_ids", sa.JSON(), nullable=False),
            sa.Column("status", sa.String(length=16), nullable=False, server_default="pending"),
            sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("lease_expires_at", sa.DateTime(), nullable=True),
            sa.Column("last_error", sa.Text(), nullable=True),
            sa.Column("finished_at", sa.DateTime(), nullable=True),
            sa.UniqueConstraint("job_key", name="uq_conversation_index_job_key"),
        )
        op.create_index("ix_conversation_index_jobs_session_id", "conversation_index_jobs",
                        ["session_id"])
        op.create_index("ix_conversation_index_jobs_user_id", "conversation_index_jobs",
                        ["user_id"])
        op.create_index("ix_conversation_index_jobs_status_created", "conversation_index_jobs",
                        ["status", "created_at"])
        op.create_index("ix_conversation_index_jobs_session", "conversation_index_jobs",
                        ["session_id", "status"])
        op.create_index("ix_conversation_index_jobs_lease", "conversation_index_jobs",
                        ["status", "lease_expires_at"])

    if not _has_table("conversation_memory_settings"):
        op.create_table(
            "conversation_memory_settings",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.Column("user_id", sa.Integer(), nullable=False),
            sa.Column("history_enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("auto_retrieval_enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("project_memory_enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.UniqueConstraint("user_id", name="uq_conversation_memory_setting_user"),
        )
        op.create_index("ix_conversation_memory_settings_user_id", "conversation_memory_settings",
                        ["user_id"])

    if not _has_table("conversation_memory_exclusions"):
        op.create_table(
            "conversation_memory_exclusions",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.Column("user_id", sa.Integer(), nullable=False),
            sa.Column("conversation_id", sa.Integer(), nullable=False),
            sa.Column("reason", sa.String(length=200), nullable=True),
            sa.UniqueConstraint("user_id", "conversation_id", name="uq_conversation_memory_exclusion"),
        )
        op.create_index("ix_conversation_memory_exclusions_user_id", "conversation_memory_exclusions",
                        ["user_id"])
        op.create_index("ix_conversation_memory_exclusions_conversation_id",
                        "conversation_memory_exclusions", ["conversation_id"])

    if not _has_table("conversation_memory_shares"):
        op.create_table(
            "conversation_memory_shares",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.Column("conversation_id", sa.Integer(), nullable=False),
            sa.Column("project_id", sa.Integer(), nullable=False),
            sa.Column("shared_by", sa.Integer(), nullable=False),
            sa.Column("revoked_at", sa.DateTime(), nullable=True),
            sa.UniqueConstraint("conversation_id", name="uq_conversation_memory_share_conversation"),
        )
        op.create_index("ix_conversation_memory_shares_conversation_id", "conversation_memory_shares",
                        ["conversation_id"])
        op.create_index("ix_conversation_memory_share_project", "conversation_memory_shares",
                        ["project_id"])


def downgrade() -> None:
    for table in (
        "conversation_memory_shares",
        "conversation_memory_exclusions",
        "conversation_memory_settings",
        "conversation_index_jobs",
        "conversation_index_entries",
    ):
        if _has_table(table):
            op.drop_table(table)
