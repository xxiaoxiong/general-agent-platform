"""同一 HTTP 操作与数据库变更共享的审计身份。"""

from contextvars import ContextVar
from dataclasses import dataclass, field


@dataclass
class AuditContext:
    request_id: str
    method: str
    path: str
    ip_address: str | None = None
    user_agent: str | None = None
    user_id: int | None = None
    username: str | None = None
    action: str | None = None
    module: str | None = None
    resource_type: str | None = None
    resource_id: str | None = None
    external_changes: list[dict] = field(default_factory=list)


current_audit: ContextVar[AuditContext | None] = ContextVar("current_audit", default=None)


def bind_audit_actor(user_id: int, username: str) -> None:
    context = current_audit.get()
    if context is not None:
        context.user_id = user_id
        context.username = username


def record_attachment_change(session_id: int, *, before: dict | None = None, after: dict | None = None) -> None:
    """登记已落盘的文件元数据，审计不读取附件正文。"""
    context = current_audit.get()
    data = after if after is not None else before
    if context is not None and data is not None:
        context.external_changes.append({
            "action": "create" if before is None else "delete", "module": "agent",
            "resource_type": "workspace_files", "resource_id": str(session_id),
            "before_data": before, "after_data": after, "changed_fields": list(data),
        })
