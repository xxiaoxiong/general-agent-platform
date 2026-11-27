"""内置浏览器（服务端 Chromium）会话服务。

预览面板不再要求客户机直接访问 Preview Proxy：由平台侧无头浏览器打开
Sandbox Service 页面，画面帧与键鼠输入经 Backend WebSocket 中转。

安全边界：
- 只有 Backend 能签发浏览器会话令牌（JWT 绑定用户 / Workspace / Service）；
- browser-service 只接受 Backend 的内部令牌与 Backend 生成的 Preview URL；
- 浏览器经 Preview Proxy（Grant 校验 + Runner Gateway）访问 Sandbox，不直连容器网络；
- 未配置 ``BROWSER_SERVICE_TOKEN`` 时 fail-closed（返回 browser_unavailable）。
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import quote

import httpx
from jose import JWTError, jwt

from app.core.config import settings

from .common import SandboxError

logger = logging.getLogger("ai_plat.sandbox.browser")


class BrowserUnavailableError(SandboxError):
    """内置浏览器未启用或服务不可达。"""

    status_code = 503
    code = "browser_unavailable"


class BrowserSessionError(SandboxError):
    """浏览器会话创建 / 页面打开失败。"""

    status_code = 502
    code = "browser_session_failed"


def browser_available() -> bool:
    """内置浏览器是否可用（配置了内部令牌且服务地址非空）。"""
    return bool((settings.BROWSER_SERVICE_TOKEN or "").strip()) and bool(
        (settings.BROWSER_SERVICE_URL or "").strip()
    )


def new_browser_session_id() -> str:
    return f"bs{uuid.uuid4().hex}"


def clamp_viewport(width: Optional[int], height: Optional[int]) -> tuple[int, int]:
    """限制视口尺寸，避免异常参数拖垮服务端浏览器。"""
    try:
        parsed_width = int(width or settings.BROWSER_VIEWPORT_WIDTH)
        parsed_height = int(height or settings.BROWSER_VIEWPORT_HEIGHT)
    except (TypeError, ValueError):
        parsed_width, parsed_height = (
            int(settings.BROWSER_VIEWPORT_WIDTH), int(settings.BROWSER_VIEWPORT_HEIGHT),
        )
    return (
        max(320, min(3840, parsed_width)),
        max(240, min(2160, parsed_height)),
    )


def build_internal_preview_url(token: str, access_token: str = "") -> str:
    """浏览器可达的内部 Preview 地址（Host 携带 Grant Token，仍由 Proxy 校验）。"""
    domain = (settings.PREVIEW_INTERNAL_BASE_DOMAIN or "preview.internal").strip()
    port = int(settings.PREVIEW_PROXY_PORT or 8081)
    url = f"http://{token}.{domain}:{port}/"
    if access_token:
        url += f"?access_token={quote(access_token)}"
    return url


def create_browser_session_token(
    session_id: str, user_id: int, workspace_id: int, service_id: int,
    ttl_seconds: int,
) -> str:
    """短期浏览器会话令牌：只用于 Backend WebSocket 鉴权。"""
    expire = datetime.now(timezone.utc) + timedelta(
        seconds=max(60, int(ttl_seconds or settings.BROWSER_SESSION_TTL))
    )
    return jwt.encode(
        {
            "exp": expire,
            "sub": str(user_id),
            "sid": session_id,
            "wid": int(workspace_id),
            "svc": int(service_id),
            "type": "browser",
        },
        settings.SECRET_KEY,
        algorithm="HS256",
    )


def decode_browser_session_token(raw: str) -> Optional[dict]:
    if not raw:
        return None
    try:
        payload = jwt.decode(raw, settings.SECRET_KEY, algorithms=["HS256"])
    except JWTError:
        return None
    if payload.get("type") != "browser":
        return None
    return payload


def browser_ws_path(session_id: str, token: str) -> str:
    return (
        f"/api/v2/workspaces/browser-sessions/{session_id}/stream"
        f"?token={quote(token)}"
    )


def _headers() -> dict:
    return {"Authorization": f"Bearer {(settings.BROWSER_SERVICE_TOKEN or '').strip()}"}


def _error_message(response: httpx.Response) -> str:
    try:
        data = response.json()
    except ValueError:
        return f"内置浏览器服务返回 HTTP {response.status_code}"
    if isinstance(data, dict):
        for key in ("message", "detail"):
            value = data.get(key)
            if isinstance(value, str) and value:
                return value
    return f"内置浏览器服务返回 HTTP {response.status_code}"


def _service_url(path: str) -> str:
    base = (settings.BROWSER_SERVICE_URL or "").strip().rstrip("/")
    return f"{base}{path}"


async def create_remote_session(
    session_id: str, url: str, *, width: int, height: int,
) -> None:
    """通知 browser-service 创建会话并打开页面（页面加载失败直接回传可读错误）。"""
    if not browser_available():
        raise BrowserUnavailableError(
            "内置浏览器未启用（BROWSER_SERVICE_TOKEN 为空）。"
            "请在部署配置中启用 browser-service 后重试。"
        )
    payload = {
        "session_id": session_id,
        "url": url,
        "viewport": {"width": int(width), "height": int(height)},
    }
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(connect=10.0, read=60.0, write=30.0, pool=10.0),
        ) as client:
            response = await client.post(
                _service_url("/sessions"), json=payload, headers=_headers(),
            )
    except httpx.HTTPError as exc:
        raise BrowserUnavailableError(
            "内置浏览器服务暂不可达，请确认 browser-service 已启动。"
        ) from exc
    if response.status_code >= 400:
        raise BrowserSessionError(_error_message(response))


async def delete_remote_session(session_id: str) -> None:
    """尽力关闭服务端会话（失败只记日志，不阻塞用户请求）。"""
    if not browser_available():
        return
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(5.0)) as client:
            await client.delete(
                _service_url(f"/sessions/{session_id}"), headers=_headers(),
            )
    except httpx.HTTPError as exc:
        logger.info("关闭内置浏览器会话失败 session=%s: %s", session_id, exc)
