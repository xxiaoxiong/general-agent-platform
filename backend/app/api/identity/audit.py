"""管理员审计查询，列表和详情分开读取。"""

from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, field_serializer
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import defer

from app.api.deps import get_current_superuser
from app.core.database import get_db
from app.models.audit import AuditLog
from app.models.user import User
from app.schemas.common import PageResponse

router = APIRouter(prefix="/audit", tags=["审计日志"])


class AuditLogSchema(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    user_id: int | None = None
    username: str | None = None
    action: str
    module: str
    resource_type: str | None = None
    resource_id: str | None = None
    detail: dict | None = None
    ip_address: str | None = None
    user_agent: str | None = None
    status: str
    error_msg: str | None = None
    duration_ms: int | None = None
    event_type: str | None = None
    request_id: str | None = None
    http_method: str | None = None
    request_path: str | None = None
    status_code: int | None = None
    changed_fields: list[str] | None = None
    created_at: datetime

    @field_serializer("created_at")
    def serialize_time(self, value: datetime) -> str:
        return (value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value).isoformat()


class AuditLogDetail(AuditLogSchema):
    before_data: dict | None = None
    after_data: dict | None = None


def _utc_naive(value: datetime) -> datetime:
    return value.astimezone(timezone.utc).replace(tzinfo=None) if value.tzinfo is not None else value


@router.get("", response_model=PageResponse[AuditLogSchema])
async def list_audit_logs(
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    username: str | None = Query(None, max_length=50),
    action: str | None = Query(None, max_length=100),
    module: str | None = Query(None, max_length=50),
    status: str | None = Query(None, max_length=20),
    event_type: Literal["request", "change", "legacy"] | None = None,
    resource_type: str | None = Query(None, max_length=50),
    resource_id: str | None = Query(None, max_length=100),
    request_id: str | None = Query(None, max_length=64),
    start_time: datetime | None = None,
    end_time: datetime | None = None,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_superuser),
):
    filters = [column == value for column, value in (
        (AuditLog.action, action), (AuditLog.module, module), (AuditLog.status, status),
        (AuditLog.resource_type, resource_type), (AuditLog.resource_id, resource_id),
        (AuditLog.request_id, request_id),
    ) if value]
    if username:
        filters.append(AuditLog.username.contains(username, autoescape=True))
    if event_type:
        filters.append(AuditLog.event_type.is_(None) if event_type == "legacy" else AuditLog.event_type == event_type)
    if start_time:
        filters.append(AuditLog.created_at >= _utc_naive(start_time))
    if end_time:
        filters.append(AuditLog.created_at <= _utc_naive(end_time))

    total = await db.scalar(select(func.count(AuditLog.id)).where(*filters)) or 0
    # 先排序窄主键，再回表，避免较大的审计 JSON 参与 MySQL filesort。
    ids = (await db.scalars(select(AuditLog.id).where(*filters).order_by(AuditLog.id.desc())
                           .offset((page - 1) * page_size).limit(page_size))).all()
    logs = (await db.scalars(select(AuditLog).options(defer(AuditLog.before_data), defer(AuditLog.after_data))
                            .where(AuditLog.id.in_(ids)))).all() if ids else []
    return PageResponse(
        items=[AuditLogSchema.model_validate(log) for log in sorted(logs, key=lambda item: item.id, reverse=True)],
        total=total, page=page, page_size=page_size, total_pages=(total + page_size - 1) // page_size,
    )


@router.get("/{audit_id}", response_model=AuditLogDetail)
async def get_audit_log(
    audit_id: int,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_superuser),
):
    log = await db.get(AuditLog, audit_id)
    if log is None:
        raise HTTPException(404, "审计记录不存在")
    return AuditLogDetail.model_validate(log)
