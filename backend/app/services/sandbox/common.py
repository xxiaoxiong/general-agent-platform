"""Sandbox 控制面共享工具：ID、时间、事件、错误。

所有错误继承 ``SandboxError``，API 层统一映射为明确 HTTP 状态：
- ``SandboxDisabledError`` 503（总开关关闭）
- ``SandboxNotFoundError`` 404
- ``SandboxAccessDenied`` 403
- ``SandboxQuotaError`` 429（用户/租户配额）
- ``SandboxResourceUnavailable`` 429（节点资源不足，可稍后重试）
- ``SandboxNotReadyError`` 409（Sandbox 未启动 / 正在切换 Generation）
- ``SandboxOperationError`` 502（Runner / workspace-agent 故障）
"""
from __future__ import annotations

import hashlib
import json
import logging
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.sandbox import SandboxEvent

logger = logging.getLogger("ai_plat.sandbox")


class SandboxError(Exception):
    status_code = 500
    code = "sandbox_error"

    def __init__(self, message: str, *, code: Optional[str] = None):
        super().__init__(message)
        self.message = message
        if code:
            self.code = code


class SandboxDisabledError(SandboxError):
    status_code = 503
    code = "sandbox_disabled"


class SandboxNotFoundError(SandboxError):
    status_code = 404
    code = "sandbox_not_found"


class SandboxAccessDenied(SandboxError):
    status_code = 403
    code = "sandbox_forbidden"


class SandboxQuotaError(SandboxError):
    status_code = 429
    code = "sandbox_quota_exceeded"


class SandboxResourceUnavailable(SandboxError):
    status_code = 429
    code = "sandbox_resource_unavailable"


class SandboxNotReadyError(SandboxError):
    status_code = 409
    code = "sandbox_not_ready"


class SandboxOperationError(SandboxError):
    status_code = 502
    code = "sandbox_operation_failed"


class JobLeaseLost(SandboxError):
    status_code = 409
    code = "sandbox_job_lease_lost"


def utcnow() -> datetime:
    """统一使用 naive UTC（与现有模型一致，避免 MySQL/SQLite 时区混用）。"""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def new_id(prefix: str = "") -> str:
    value = uuid.uuid4().hex
    return f"{prefix}{value}" if prefix else value


def new_token(nbytes: int = 24) -> str:
    return secrets.token_urlsafe(nbytes)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def json_safe(value: Any) -> Any:
    """事件 payload 落库前转 JSON 安全结构（禁止 Token/密钥进入事件）。"""
    try:
        return json.loads(json.dumps(value, ensure_ascii=False, default=str))
    except (TypeError, ValueError):
        return str(value)


async def emit_event(
    db: AsyncSession,
    event_type: str,
    *,
    tenant_id: Optional[str] = None,
    user_id: Optional[int] = None,
    project_id: Optional[int] = None,
    workspace_id: Optional[int] = None,
    sandbox_id: Optional[int] = None,
    generation: Optional[int] = None,
    run_id: Optional[str] = None,
    job_id: Optional[str] = None,
    payload: Optional[dict] = None,
) -> None:
    """写审计事件（不提交事务，由调用方统一提交）。"""
    db.add(SandboxEvent(
        event_type=event_type,
        tenant_id=tenant_id,
        user_id=user_id,
        project_id=project_id,
        workspace_id=workspace_id,
        sandbox_id=sandbox_id,
        generation=generation,
        run_id=run_id,
        job_id=job_id,
        payload=json_safe(payload or {}),
    ))


def lease_expiry(seconds: Optional[int] = None) -> datetime:
    return utcnow() + timedelta(seconds=seconds or settings.SANDBOX_JOB_LEASE_SECONDS)


def ensure_enabled() -> None:
    if not settings.SANDBOX_ENABLED:
        raise SandboxDisabledError(
            "Workspace Sandbox 未启用（SANDBOX_ENABLED=false）。"
            "请先在部署配置中启用 Sandbox Runtime 并启动 sandbox-runner。"
        )


def redact_log_text(text: str, limit: int = 20_000) -> str:
    """服务日志/命令输出脱敏（复用评测模块的脱敏规则，避免凭据外泄）。"""
    try:
        from app.services.evaluation.artifacts import redact_text

        text = redact_text(text or "")
    except Exception:  # noqa: BLE001 脱敏失败不得阻断读取
        pass
    if len(text) > limit:
        return text[:limit] + f"\n...[已截断，共 {len(text)} 字符]"
    return text
