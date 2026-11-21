"""文档资产、规范及普通用户补充请求的持久化模型。"""

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import DateTime, Float, ForeignKey, Index, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, BaseModel


def _now() -> datetime:
    return datetime.now(timezone.utc)


class DocumentProfile(BaseModel):
    __tablename__ = "document_profiles"
    __table_args__ = (Index("ix_document_profiles_owner_updated", "owner_id", "updated_at"),)

    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    name: Mapped[str] = mapped_column(String(180))
    description: Mapped[str] = mapped_column(Text, default="")
    document_type: Mapped[str] = mapped_column(String(80))
    visibility: Mapped[str] = mapped_column(String(20), default="private")
    status: Mapped[str] = mapped_column(String(20), default="draft")
    current_version: Mapped[int] = mapped_column(Integer, default=1)
    published_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class DocumentProfileVersion(BaseModel):
    __tablename__ = "document_profile_versions"
    __table_args__ = (
        UniqueConstraint("profile_id", "version_no", name="uq_document_profile_version"),
        UniqueConstraint("created_by_run_id", name="uq_document_profile_run"),
    )

    profile_id: Mapped[int] = mapped_column(ForeignKey("document_profiles.id"))
    version_no: Mapped[int] = mapped_column(Integer)
    definition: Mapped[dict[str, Any]] = mapped_column(JSON)
    source_summary: Mapped[str] = mapped_column(Text, default="")
    source_metadata: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    extraction_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    change_summary: Mapped[str] = mapped_column(String(500), default="")
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_by_run_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("agent_runs.id", ondelete="SET NULL"),
    )


class DocumentAsset(BaseModel):
    __tablename__ = "document_assets"
    __table_args__ = (Index("ix_document_assets_owner_updated", "owner_id", "updated_at"),)

    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    title: Mapped[str] = mapped_column(String(220))
    document_type: Mapped[str] = mapped_column(String(80))
    session_id: Mapped[int | None] = mapped_column(ForeignKey("agent_sessions.id", ondelete="SET NULL"))
    profile_id: Mapped[int | None] = mapped_column(ForeignKey("document_profiles.id"))
    current_version: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(20), default="active")
    created_by_run_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("agent_runs.id", ondelete="SET NULL"),
    )


class DocumentVersion(BaseModel):
    __tablename__ = "document_versions"
    __table_args__ = (
        UniqueConstraint("document_id", "version_no", name="uq_document_version"),
        UniqueConstraint("created_by_run_id", name="uq_document_version_run"),
    )

    document_id: Mapped[int] = mapped_column(ForeignKey("document_assets.id"))
    version_no: Mapped[int] = mapped_column(Integer)
    filename: Mapped[str] = mapped_column(String(220))
    relative_path: Mapped[str] = mapped_column(String(255))
    mime_type: Mapped[str] = mapped_column(String(120))
    size: Mapped[int] = mapped_column(Integer)
    content_hash: Mapped[str] = mapped_column(String(64))
    profile_id: Mapped[int | None] = mapped_column(ForeignKey("document_profiles.id"))
    profile_version_no: Mapped[int | None] = mapped_column(Integer)
    created_by_run_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("agent_runs.id", ondelete="SET NULL"),
    )
    change_summary: Mapped[str] = mapped_column(String(500), default="")
    validation_report: Mapped[dict[str, Any]] = mapped_column(JSON)
    version_diff: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    preview: Mapped[dict[str, Any]] = mapped_column(JSON)


class RunInputRequest(Base):
    __tablename__ = "run_input_requests"
    __table_args__ = (UniqueConstraint("run_id", "ordinal", name="uq_run_input_ordinal"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(36), ForeignKey("agent_runs.id", ondelete="CASCADE"))
    ordinal: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(20), default="pending")
    prompt: Mapped[str] = mapped_column(Text)
    input_schema: Mapped[dict[str, Any]] = mapped_column(JSON)
    required_fields: Mapped[list[str]] = mapped_column(JSON)
    response: Mapped[dict[str, Any] | None] = mapped_column(JSON(none_as_null=True))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime)
