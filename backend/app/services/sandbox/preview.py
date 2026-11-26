"""Preview Grant（任务书 §5.7 / §10.2）。
- Token 只保存 SHA256；绑定 Workspace / Sandbox Generation / Service / Port；
- 支持过期与撤销；成员移除后撤销该成员签发的 Grant；
- Preview Token 不能用于命令执行或文件管理（只经 Preview Proxy 使用）。
"""
from __future__ import annotations

from datetime import timedelta
from typing import Optional
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.sandbox import (
    PreviewGrant, SandboxInstance, Workspace, WorkspaceService,
)
from app.models.user import User

from .common import (
    SandboxNotReadyError, emit_event, new_id, new_token, sha256_text, utcnow,
)


def build_preview_urls(token: str) -> dict:
    """生成浏览器可达地址：子域优先，路径兜底（子域不可用环境）。"""
    base = (settings.PREVIEW_PUBLIC_BASE_URL or "").strip()
    if base:
        parsed = urlsplit(base)
        scheme = parsed.scheme or "http"
        hostname = parsed.hostname or settings.PREVIEW_BASE_DOMAIN
        port_part = f":{parsed.port}" if parsed.port else ""
        subdomain_host = f"{token}.{hostname}"
        primary = f"{scheme}://{subdomain_host}{port_part}/"
        fallback = f"{scheme}://{hostname}{port_part}/p/{token}/"
        return {"url": primary, "fallback_url": fallback}
    scheme = "http"
    host_port = int(settings.PREVIEW_PROXY_HOST_PORT or settings.PREVIEW_PROXY_PORT)
    domain = settings.PREVIEW_BASE_DOMAIN or "localhost"
    primary = f"{scheme}://{token}.{domain}:{host_port}/"
    fallback = f"{scheme}://{domain}:{host_port}/p/{token}/"
    return {"url": primary, "fallback_url": fallback}


def create_preview_access_token(grant_id: str, user_id: int,
                                ttl_seconds: int = 3600) -> str:
    """登录态预览的短期凭证（只在 PREVIEW_REQUIRE_LOGIN=true 时下发）。

    绑定 Grant + User；Preview Proxy 校验签名、过期、Grant 匹配，并再次复核
    Project ACL。不得用于其他 API。
    """
    from datetime import datetime, timedelta, timezone

    from jose import jwt

    expire = datetime.now(timezone.utc) + timedelta(seconds=max(60, ttl_seconds))
    return jwt.encode(
        {"exp": expire, "sub": str(user_id), "grant": grant_id, "type": "preview"},
        settings.SECRET_KEY,
        algorithm="HS256",
    )


async def create_grant(
    db: AsyncSession, workspace: Workspace, service: WorkspaceService, user: User,
    *, ttl_seconds: Optional[int] = None, sandbox: Optional[SandboxInstance] = None,
) -> tuple[PreviewGrant, str]:
    if sandbox is None:
        sandbox = (await db.execute(
            select(SandboxInstance).where(SandboxInstance.id == service.sandbox_id)
        )).scalar_one_or_none()
    if sandbox is None or sandbox.observed_state != "ready":
        raise SandboxNotReadyError("服务所在 Sandbox 未运行，无法生成预览地址")
    token = new_token(24).lower()  # 子域 Host 会被浏览器小写化：Token 统一小写保证可路由
    ttl = int(ttl_seconds if ttl_seconds is not None else settings.PREVIEW_TOKEN_TTL)
    grant = PreviewGrant(
        id=new_id(),
        workspace_id=workspace.id,
        sandbox_id=sandbox.id,
        service_id=service.id,
        port=service.port,
        token_hash=sha256_text(token),
        expires_at=utcnow() + timedelta(seconds=ttl) if ttl > 0 else None,
        created_by_user_id=user.id,
    )
    db.add(grant)
    await emit_event(
        db, "preview.granted", user_id=user.id, tenant_id=workspace.tenant_id,
        project_id=workspace.project_id, workspace_id=workspace.id,
        sandbox_id=sandbox.id, generation=sandbox.generation,
        payload={"service": service.name, "port": service.port, "grant_id": grant.id,
                 "expires_at": grant.expires_at},
    )
    return grant, token


async def revoke_grant(db: AsyncSession, workspace: Workspace, grant_id: str,
                       user: User) -> None:
    grant = (await db.execute(
        select(PreviewGrant).where(
            PreviewGrant.id == grant_id,
            PreviewGrant.workspace_id == workspace.id,
        )
    )).scalar_one_or_none()
    if grant is None:
        raise SandboxNotReadyError("预览授权不存在")
    if grant.revoked_at is None:
        grant.revoked_at = utcnow()
        await emit_event(
            db, "preview.revoked", user_id=user.id, tenant_id=workspace.tenant_id,
            project_id=workspace.project_id, workspace_id=workspace.id,
            sandbox_id=grant.sandbox_id,
            payload={"grant_id": grant.id, "service_id": grant.service_id},
        )


async def revoke_grants_for_user(db: AsyncSession, project_id: int, user_id: int) -> int:
    from sqlalchemy import update

    workspace_ids = (await db.execute(
        select(Workspace.id).where(Workspace.project_id == project_id)
    )).scalars().all()
    if not workspace_ids:
        return 0
    result = await db.execute(
        update(PreviewGrant)
        .where(
            PreviewGrant.workspace_id.in_(workspace_ids),
            PreviewGrant.created_by_user_id == user_id,
            PreviewGrant.revoked_at.is_(None),
        )
        .values(revoked_at=utcnow())
    )
    return result.rowcount or 0


async def list_grants(db: AsyncSession, workspace: Workspace) -> list[dict]:
    grants = (await db.execute(
        select(PreviewGrant)
        .where(PreviewGrant.workspace_id == workspace.id)
        .order_by(PreviewGrant.created_at.desc())
    )).scalars().all()
    return [
        {
            "id": grant.id,
            "service_id": grant.service_id,
            "sandbox_id": grant.sandbox_id,
            "port": grant.port,
            "expires_at": grant.expires_at,
            "revoked_at": grant.revoked_at,
            "created_by_user_id": grant.created_by_user_id,
            "created_at": grant.created_at,
            "last_accessed_at": grant.last_accessed_at,
        }
        for grant in grants
    ]


class PreviewDenied(Exception):
    def __init__(self, reason: str, status_code: int = 403):
        super().__init__(reason)
        self.reason = reason
        self.status_code = status_code


async def resolve_grant(db: AsyncSession, token: str) -> tuple[PreviewGrant, SandboxInstance, WorkspaceService]:
    """Preview Proxy 校验入口：Token -> Sandbox + Port（只允许已登记目标）。"""
    if not token or len(token) < 8:
        raise PreviewDenied("invalid_token", 401)
    grant = (await db.execute(
        select(PreviewGrant).where(PreviewGrant.token_hash == sha256_text(token))
    )).scalar_one_or_none()
    if grant is None:
        raise PreviewDenied("invalid_token", 401)
    if grant.revoked_at is not None:
        raise PreviewDenied("grant_revoked", 403)
    if grant.expires_at is not None and grant.expires_at < utcnow():
        raise PreviewDenied("grant_expired", 403)
    sandbox = (await db.execute(
        select(SandboxInstance).where(SandboxInstance.id == grant.sandbox_id)
    )).scalar_one_or_none()
    if sandbox is None or sandbox.observed_state != "ready":
        raise PreviewDenied("sandbox_not_ready", 410)
    service = (await db.execute(
        select(WorkspaceService).where(WorkspaceService.id == grant.service_id)
    )).scalar_one_or_none()
    if service is None or service.desired_state != "running":
        raise PreviewDenied("service_stopped", 410)
    if service.observed_state in ("stopped", "exited", "failed"):
        raise PreviewDenied("service_unavailable", 410)
    grant.last_accessed_at = utcnow()
    workspace = (await db.execute(
        select(Workspace).where(Workspace.id == grant.workspace_id)
    )).scalar_one_or_none()
    if workspace is not None:
        workspace.last_active_at = utcnow()
    return grant, sandbox, service


def grant_to_dict(grant: PreviewGrant, token: Optional[str] = None,
                  access_token: Optional[str] = None) -> dict:
    data = {
        "id": grant.id,
        "service_id": grant.service_id,
        "sandbox_id": grant.sandbox_id,
        "port": grant.port,
        "expires_at": grant.expires_at,
        "revoked_at": grant.revoked_at,
        "created_at": grant.created_at,
    }
    if token:
        data.update(build_preview_urls(token))
        data["token"] = token
    if access_token:
        data["access_token"] = access_token
    return data
