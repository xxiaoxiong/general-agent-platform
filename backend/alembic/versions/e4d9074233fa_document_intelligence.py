"""新增文档智能资产、规范版本及 Run 补充请求。

Revision ID: e4d9074233fa
Revises: f3a4b5c6d7e8
"""

import sqlalchemy as sa
from alembic import op

revision = "e4d9074233fa"
down_revision = "f3a4b5c6d7e8"
branch_labels = None
depends_on = None


def _identity():
    return (
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )


def upgrade():
    op.create_table(
        "document_profiles", *_identity(),
        sa.Column("owner_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("name", sa.String(180), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("document_type", sa.String(80), nullable=False),
        sa.Column("visibility", sa.String(20), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("current_version", sa.Integer(), nullable=False),
        sa.Column("published_version", sa.Integer()),
        sa.Column("deleted_at", sa.DateTime()),
    )
    op.create_index("ix_document_profiles_owner_updated", "document_profiles", ["owner_id", "updated_at"])

    op.create_table(
        "document_profile_versions", *_identity(),
        sa.Column("profile_id", sa.Integer(), sa.ForeignKey("document_profiles.id"), nullable=False),
        sa.Column("version_no", sa.Integer(), nullable=False),
        sa.Column("definition", sa.JSON(), nullable=False),
        sa.Column("source_summary", sa.Text(), nullable=False),
        sa.Column("source_metadata", sa.JSON(), nullable=False),
        sa.Column("extraction_confidence", sa.Float()),
        sa.Column("change_summary", sa.String(500), nullable=False),
        sa.Column("created_by", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("created_by_run_id", sa.String(36), sa.ForeignKey("agent_runs.id", ondelete="SET NULL")),
        sa.UniqueConstraint("profile_id", "version_no", name="uq_document_profile_version"),
        sa.UniqueConstraint("created_by_run_id", name="uq_document_profile_run"),
    )
    op.create_table(
        "document_assets", *_identity(),
        sa.Column("owner_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("title", sa.String(220), nullable=False),
        sa.Column("document_type", sa.String(80), nullable=False),
        sa.Column("session_id", sa.Integer(), sa.ForeignKey("agent_sessions.id", ondelete="SET NULL")),
        sa.Column("profile_id", sa.Integer(), sa.ForeignKey("document_profiles.id")),
        sa.Column("current_version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("created_by_run_id", sa.String(36), sa.ForeignKey("agent_runs.id", ondelete="SET NULL")),
    )
    op.create_index("ix_document_assets_owner_updated", "document_assets", ["owner_id", "updated_at"])

    op.create_table(
        "document_versions", *_identity(),
        sa.Column("document_id", sa.Integer(), sa.ForeignKey("document_assets.id"), nullable=False),
        sa.Column("version_no", sa.Integer(), nullable=False),
        sa.Column("filename", sa.String(220), nullable=False),
        sa.Column("relative_path", sa.String(255), nullable=False),
        sa.Column("mime_type", sa.String(120), nullable=False),
        sa.Column("size", sa.Integer(), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("profile_id", sa.Integer(), sa.ForeignKey("document_profiles.id")),
        sa.Column("profile_version_no", sa.Integer()),
        sa.Column("created_by_run_id", sa.String(36), sa.ForeignKey("agent_runs.id", ondelete="SET NULL")),
        sa.Column("change_summary", sa.String(500), nullable=False),
        sa.Column("validation_report", sa.JSON(), nullable=False),
        sa.Column("version_diff", sa.JSON(), nullable=False),
        sa.Column("preview", sa.JSON(), nullable=False),
        sa.UniqueConstraint("document_id", "version_no", name="uq_document_version"),
        sa.UniqueConstraint("created_by_run_id", name="uq_document_version_run"),
    )
    op.create_table(
        "run_input_requests",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("run_id", sa.String(36), sa.ForeignKey("agent_runs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("prompt", sa.Text(), nullable=False),
        sa.Column("input_schema", sa.JSON(), nullable=False),
        sa.Column("required_fields", sa.JSON(), nullable=False),
        sa.Column("response", sa.JSON()),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("submitted_at", sa.DateTime()),
        sa.UniqueConstraint("run_id", "ordinal", name="uq_run_input_ordinal"),
    )


def downgrade():
    for table in ("run_input_requests", "document_versions", "document_assets",
                  "document_profile_versions", "document_profiles"):
        op.drop_table(table)
