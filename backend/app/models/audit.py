from sqlalchemy import Index, String, Text, Integer, JSON
from sqlalchemy.orm import Mapped, mapped_column
from app.models.base import BaseModel


class AuditLog(BaseModel):
    __tablename__ = "audit_logs"
    __table_args__ = (
        Index("ix_audit_logs_request_id", "request_id"),
        Index("ix_audit_logs_created_at", "created_at"),
        Index("ix_audit_logs_resource", "resource_type", "resource_id"),
    )

    user_id: Mapped[int] = mapped_column(Integer, nullable=True)
    username: Mapped[str] = mapped_column(String(50), nullable=True)
    action: Mapped[str] = mapped_column(String(100), nullable=False)
    module: Mapped[str] = mapped_column(String(50), nullable=False)
    resource_type: Mapped[str] = mapped_column(String(50), nullable=True)
    resource_id: Mapped[str] = mapped_column(String(100), nullable=True)
    detail: Mapped[dict] = mapped_column(JSON, nullable=True)
    ip_address: Mapped[str] = mapped_column(String(50), nullable=True)
    user_agent: Mapped[str] = mapped_column(String(500), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="success")  # success / failed
    error_msg: Mapped[str] = mapped_column(Text, nullable=True)
    duration_ms: Mapped[int] = mapped_column(Integer, nullable=True)
    event_type: Mapped[str | None] = mapped_column(String(20), nullable=True)
    request_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    http_method: Mapped[str | None] = mapped_column(String(10), nullable=True)
    request_path: Mapped[str | None] = mapped_column(String(300), nullable=True)
    status_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    before_data: Mapped[dict | None] = mapped_column(JSON(none_as_null=True), nullable=True)
    after_data: Mapped[dict | None] = mapped_column(JSON(none_as_null=True), nullable=True)
    changed_fields: Mapped[list[str] | None] = mapped_column(JSON(none_as_null=True), nullable=True)
