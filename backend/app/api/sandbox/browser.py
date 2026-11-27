"""内置浏览器会话 API（/api/v2/workspaces）：创建 / 关闭 / WebSocket 帧与输入桥接。

客户端（控制台）只访问平台自身的 /api/v2 WebSocket；服务端 Chromium 在平台内部
经 Preview Proxy 打开 Sandbox Service 页面，因此客户机无需能解析 Preview 子域或
访问 Preview 端口。
"""
from __future__ import annotations

import asyncio
import logging

import websockets
from fastapi import APIRouter, Depends, Query, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.core.config import settings
from app.core.database import AsyncSessionLocal, get_db
from app.models.user import User
from app.services.sandbox import (
    acl, browser as browser_service, preview as preview_service, runtime,
    service_manager,
)
from app.services.sandbox.common import (
    SandboxError, SandboxNotReadyError, ensure_enabled,
)

logger = logging.getLogger("ai_plat.sandbox.browser")

router = APIRouter(prefix="/workspaces", tags=["workspace-browser-v2"])

_STOPPED_SERVICE_STATES = {"created", "stopped", "exited", "failed"}


class BrowserSessionCreate(BaseModel):
    viewport_width: int = Field(default=0, ge=0, le=4096)
    viewport_height: int = Field(default=0, ge=0, le=4096)
    ttl_seconds: int = Field(default=0, ge=0, le=86400)


@router.post("/{workspace_id}/services/{service_id}/browser-sessions")
async def create_browser_session(
    workspace_id: int,
    service_id: int,
    body: BrowserSessionCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    ensure_enabled()
    workspace, _project, _role = await acl.require_workspace_access(
        db, workspace_id, current_user, "operator",
    )
    if not browser_service.browser_available():
        raise browser_service.BrowserUnavailableError(
            "内置浏览器未启用：请在部署配置中设置 BROWSER_SERVICE_TOKEN 并启动 browser-service。"
        )
    service = await service_manager.get_service(db, workspace, service_id)
    if (
        service.desired_state != "running"
        or service.observed_state in _STOPPED_SERVICE_STATES
    ):
        raise SandboxNotReadyError("服务尚未运行，请先在「项目服务」中启动服务")

    ttl = int(body.ttl_seconds or settings.BROWSER_SESSION_TTL)
    grant, grant_token = await preview_service.create_grant(
        db, workspace, service, current_user, ttl_seconds=ttl,
    )
    access_token = ""
    if settings.PREVIEW_REQUIRE_LOGIN:
        access_token = preview_service.create_preview_access_token(
            grant.id, current_user.id, ttl,
        )
    await runtime.touch_workspace(db, workspace.id)
    await db.commit()

    width, height = browser_service.clamp_viewport(
        body.viewport_width, body.viewport_height,
    )
    session_id = browser_service.new_browser_session_id()
    internal_url = browser_service.build_internal_preview_url(
        grant_token, access_token,
    )
    await browser_service.create_remote_session(
        session_id, internal_url, width=width, height=height,
    )
    session_token = browser_service.create_browser_session_token(
        session_id, current_user.id, workspace.id, service.id, ttl,
    )
    grant_info = preview_service.grant_to_dict(grant, grant_token, access_token or None)
    return {
        "session_id": session_id,
        "token": session_token,
        "ws_url": browser_service.browser_ws_path(session_id, session_token),
        "expires_at": grant.expires_at,
        "ttl_seconds": ttl,
        "preview_url": grant_info.get("url"),
        "fallback_url": grant_info.get("fallback_url"),
        "access_token": access_token or None,
        "viewport": {"width": width, "height": height},
    }


@router.delete("/browser-sessions/{session_id}")
async def delete_browser_session(
    session_id: str,
    token: str = Query("", max_length=2048),
):
    payload = browser_service.decode_browser_session_token(token)
    if payload is None or str(payload.get("sid") or "") != session_id:
        raise browser_service.BrowserUnavailableError("浏览器会话令牌无效或已过期")
    await browser_service.delete_remote_session(session_id)
    return {"status": "closed", "session_id": session_id}


@router.websocket("/browser-sessions/{session_id}/stream")
async def stream_browser_session(websocket: WebSocket, session_id: str):
    """控制台 WebSocket ↔ browser-service：双向转发 JPEG 帧与输入 JSON。"""
    token = websocket.query_params.get("token") or ""
    payload = browser_service.decode_browser_session_token(token)
    if payload is None or str(payload.get("sid") or "") != session_id:
        await websocket.close(code=4403)
        return
    if not browser_service.browser_available():
        await websocket.close(code=1011)
        return
    try:
        user_id = int(payload.get("sub") or 0)
        workspace_id = int(payload.get("wid") or 0)
    except (TypeError, ValueError):
        await websocket.close(code=4403)
        return
    try:
        async with AsyncSessionLocal() as db:
            user = await db.get(User, user_id)
            if user is None or not bool(user.is_active):
                await websocket.close(code=4403)
                return
            await acl.require_workspace_access(db, workspace_id, user, "viewer")
    except SandboxError:
        await websocket.close(code=4403)
        return
    except Exception:  # noqa: BLE001 鉴权异常一律拒绝连接
        await websocket.close(code=4403)
        return

    upstream_base = (
        (settings.BROWSER_SERVICE_URL or "").strip().rstrip("/")
        .replace("http://", "ws://").replace("https://", "wss://")
    )
    try:
        upstream = await websockets.connect(
            f"{upstream_base}/sessions/{session_id}/stream",
            additional_headers={
                "Authorization": f"Bearer {(settings.BROWSER_SERVICE_TOKEN or '').strip()}",
            },
            open_timeout=15, ping_interval=20, max_size=None,
        )
    except Exception as exc:  # noqa: BLE001 浏览器服务不可达
        logger.info("内置浏览器上游连接失败 session=%s: %s", session_id, exc)
        await websocket.close(code=1011)
        return

    await websocket.accept()

    async def client_to_upstream() -> None:
        try:
            while True:
                message = await websocket.receive()
                if message.get("type") == "websocket.disconnect":
                    break
                if message.get("text") is not None:
                    await upstream.send(message["text"])
                elif message.get("bytes") is not None:
                    await upstream.send(message["bytes"])
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            try:
                await upstream.close()
            except Exception:  # noqa: BLE001 上游可能已关闭
                pass

    async def upstream_to_client() -> None:
        try:
            async for message in upstream:
                if isinstance(message, bytes):
                    await websocket.send_bytes(message)
                else:
                    await websocket.send_text(message)
        except Exception:  # noqa: BLE001 连接关闭属正常路径
            pass
        finally:
            try:
                await websocket.close()
            except Exception:  # noqa: BLE001 客户端可能已断开
                pass

    await asyncio.gather(client_to_upstream(), upstream_to_client(),
                         return_exceptions=True)
