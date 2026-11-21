"""在 API 边界记录业务请求，避免复制请求或响应正文。"""

import logging
import re
from time import monotonic
from uuid import uuid4

from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core import database
from app.models.audit import AuditLog

from .audit_context import AuditContext, current_audit

logger = logging.getLogger(__name__)
_PREFIXES = ("/api/v1/", "/api/v2/")
_METHODS = {"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"}
_MODULES = {
    "users": "user", "roles": "user", "models": "model", "workflows": "workflow",
    "conversations": "agent", "assistants": "agent", "runs": "agent", "agents": "agent",
    "documents": "document", "document-profiles": "document", "databases": "database",
    "prompts": "prompt", "skills": "skill", "evaluation": "evaluation", "mcp": "mcp",
    "apps": "app", "evolution": "evolution", "artifacts": "document",
}
_AUDIT_LIST_PATH = "/api/v1/audit"
_RESOURCE_IDS = re.compile(r"[a-zA-Z0-9_-]{1,100}\Z")


def _audit_candidate(scope: Scope) -> bool:
    if scope["type"] != "http" or scope.get("method") not in _METHODS:
        return False
    path = scope.get("path", "").rstrip("/")
    # SSE 重连属于传输流量，发起或变更任务使用各自的业务 API 记录。
    return path.startswith(_PREFIXES) and not (scope["method"] == "GET" and path.endswith("/events"))


def bind_audit_route(scope: Scope) -> None:
    context = current_audit.get()
    template = getattr(scope.get("route"), "path", "")
    if context is None or not template:
        return
    # FastAPI 版本间保留的可能是带前缀的路由，或 include_router 前的路由模板。
    prefix = next((prefix for prefix in _PREFIXES if scope.get("path", "").startswith(prefix)), "")
    if prefix:
        context.path = (template if template.startswith(_PREFIXES) else prefix[:-1] + template)[:300]


async def _write_http_event(scope: Scope, context: AuditContext, status: int, started: float) -> None:
    route = scope.get("route")
    if not context.path or not route:
        return
    # 打开审计列表不挤占正在查看的历史，但拒绝和失败仍需要记录。
    if context.path == _AUDIT_LIST_PATH and status < 400:
        return
    prefix = context.path.split("/", 4)[3]
    params = scope.get("path_params") or {}
    resource = next((str(value) for key, value in reversed(list(params.items()))
                     if (key == "id" or key.endswith("_id")) and _RESOURCE_IDS.fullmatch(str(value))), None)
    outcome = "success" if status < 400 else "denied" if status in (401, 403) else "failed"
    async with database.AsyncSessionLocal() as db:
        identity = dict(request_id=context.request_id, http_method=context.method,
                        request_path=context.path, status_code=status, user_id=context.user_id,
                        username=context.username, ip_address=context.ip_address, user_agent=context.user_agent)
        db.add(AuditLog(
            **identity, event_type="request", status=outcome, action=(context.action or route.name)[:100],
            module=(context.module or _MODULES.get(prefix, prefix))[:50],
            resource_type=(context.resource_type or prefix)[:50],
            resource_id=context.resource_id or resource,
            duration_ms=min(int((monotonic() - started) * 1000), 2_147_483_647),
        ))
        for change in context.external_changes:
            # 文件系统落盘独立于 HTTP 返回。即使后来出现错误，也保留已经完成的变更事实。
            db.add(AuditLog(**identity, **change, event_type="change", status="success"))
        await db.commit()


class AuditHTTPMiddleware:
    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if not _audit_candidate(scope):
            await self.app(scope, receive, send)
            return
        started = monotonic()
        client = scope.get("client")
        context = AuditContext(
            uuid4().hex, scope["method"], "",
            ip_address=str(client[0])[:50] if client else None,
            user_agent=Headers(scope=scope).get("user-agent", "")[:500] or None,
        )
        scope.setdefault("state", {})["request_id"] = context.request_id
        token = current_audit.set(context)
        status: int | None = None
        failed = False

        async def send_with_request_id(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                MutableHeaders(scope=message)["X-Request-ID"] = context.request_id
            await send(message)

        try:
            await self.app(scope, receive, send_with_request_id)
        except Exception:
            failed = True
            raise
        finally:
            bind_audit_route(scope)
            current_audit.reset(token)
            # read-only 操作也能追查；权限拒绝及异常与业务事务分开持久化。
            if status is not None or failed:
                try:
                    await _write_http_event(scope, context, 500 if failed else status, started)
                except Exception:
                    logger.exception("请求审计写入失败 request_id=%s", context.request_id)
