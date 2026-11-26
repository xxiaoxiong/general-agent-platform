"""Preview Proxy：Preview Grant 校验 + 子域路由 + HTTP/WebSocket 转发。

安全边界（任务书 §10.2 / §7.4）：
- 只代理 Preview Grant 中登记的 Sandbox + Port，不接受任意 upstream URL；
- Token 只做 Hash 匹配，不写入日志；
- 不挂载 Docker Socket；通过 Runner Gateway 访问 Sandbox；
- Preview 与控制台不同 Origin（本地 {token}.localhost，生产独立域名 + TLS）。
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Optional

import httpx
import websockets
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, Response, StreamingResponse

from app.core.config import settings
from app.core.database import AsyncSessionLocal
from app.observability import metrics
from app.services.sandbox.common import SandboxError
from app.services.sandbox.preview import PreviewDenied, resolve_grant

logger = logging.getLogger("ai_plat.preview_proxy")

app = FastAPI(title="Preview Proxy")

_HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "host", "content-length",
}
_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{16,128}$")
_PATH_TOKEN_RE = re.compile(r"^/p/([A-Za-z0-9_-]{16,128})(/.*)?$")

_connection_lock = asyncio.Lock()
_connections: dict[str, int] = {}
_rate_lock = asyncio.Lock()
_rate_windows: dict[str, list[float]] = {}


async def _rate_allowed(grant_id: str) -> bool:
    """单 Grant 每分钟请求上限（滑动窗口，进程内；多副本部署按副本计）。"""
    limit = int(settings.PREVIEW_RATE_LIMIT_PER_MINUTE or 0)
    if limit <= 0:
        return True
    now = time.monotonic()
    async with _rate_lock:
        window = [stamp for stamp in _rate_windows.get(grant_id, []) if now - stamp < 60.0]
        if len(window) >= limit:
            _rate_windows[grant_id] = window
            return False
        window.append(now)
        _rate_windows[grant_id] = window
        if len(_rate_windows) > 2000:
            empty = [key for key, stamps in _rate_windows.items() if not stamps]
            for key in empty[:1000]:
                _rate_windows.pop(key, None)
        return True


def _runner_base() -> str:
    return (settings.SANDBOX_RUNNER_CONTROL_URL or "").rstrip("/")


def _extract_token(request_host: str, path: str) -> tuple[Optional[str], str]:
    match = _PATH_TOKEN_RE.match(path or "/")
    if match:
        return match.group(1), match.group(2) or "/"
    raw_hostname = (request_host or "").split(":")[0]
    hostname = raw_hostname.lower()
    base = (settings.PREVIEW_BASE_DOMAIN or "").lower()
    if base and hostname.endswith("." + base):
        # Host 域名大小写不敏感，但 Token 大小写敏感：保留原始大小写
        prefix = raw_hostname[: -(len(base) + 1)]
        if prefix and "." not in prefix and _TOKEN_RE.match(prefix):
            return prefix, path or "/"
    return None, path or "/"


async def _grant_target(token: str) -> dict:
    async with AsyncSessionLocal() as db:
        grant, sandbox, service = await resolve_grant(db, token)
        target = {
            "grant_id": grant.id,
            "sandbox_id": sandbox.id,
            "port": service.port,
            "service_name": service.name,
            "workspace_id": sandbox.workspace_id,
        }
        await db.commit()
    return target


async def _acquire(grant_id: str) -> bool:
    limit = int(settings.PREVIEW_MAX_CONNECTIONS or 0)
    async with _connection_lock:
        current = _connections.get(grant_id, 0)
        if limit > 0 and current >= limit:
            return False
        _connections[grant_id] = current + 1
        return True


async def _release(grant_id: str) -> None:
    async with _connection_lock:
        current = _connections.get(grant_id, 0)
        if current <= 1:
            _connections.pop(grant_id, None)
        else:
            _connections[grant_id] = current - 1


def _security_headers(content_type: str) -> dict:
    headers = {
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "no-referrer",
        "X-Robots-Tag": "noindex, nofollow",
    }
    if "text/html" in (content_type or "").lower():
        # Preview 页面需要被控制台 iframe 嵌入；生产可将 frame-ancestors 收窄到控制台域名
        headers["Content-Security-Policy"] = "frame-ancestors *"
    return headers


def _denied_response(exc: PreviewDenied) -> JSONResponse:
    metrics.inc_preview_request(denied=True)
    return JSONResponse(
        status_code=exc.status_code,
        content={"code": exc.reason, "message": "预览授权无效或已过期，请刷新页面重新获取预览地址"},
        headers={"Cache-Control": "no-store"},
    )


@app.get("/health")
async def health():
    return {"status": "ok", "service": "preview-proxy"}


@app.get("/metrics")
async def prometheus_metrics():
    body, content_type = metrics.render()
    return Response(content=body, media_type=content_type)


@app.api_route(
    "/{path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"],
)
async def proxy_http(request: Request, path: str):
    token, rest = _extract_token(request.headers.get("host") or "", request.url.path)
    if not token:
        metrics.inc_preview_request(denied=True)
        return JSONResponse(
            status_code=404,
            content={"code": "preview_token_missing", "message": "缺少预览令牌"},
        )
    try:
        target = await _grant_target(token)
    except PreviewDenied as exc:
        return _denied_response(exc)
    except SandboxError as exc:
        metrics.inc_preview_request(denied=True)
        return JSONResponse(status_code=exc.status_code,
                            content={"code": exc.code, "message": exc.message})
    if not await _rate_allowed(target["grant_id"]):
        metrics.inc_preview_request(denied=True)
        return JSONResponse(status_code=429,
                            content={"code": "preview_rate_limited",
                                     "message": "预览请求过于频繁，请稍后重试"})
    if not await _acquire(target["grant_id"]):
        metrics.inc_preview_request(denied=True)
        return JSONResponse(status_code=429,
                            content={"code": "preview_too_many_connections",
                                     "message": "预览并发连接超限"})
    try:
        return await _forward_http(request, target, rest)
    finally:
        await _release(target["grant_id"])


async def _forward_http(request: Request, target: dict, path: str):
    upstream_path = path.lstrip("/")
    url = (f"{_runner_base()}/gateway/sandboxes/{target['sandbox_id']}"
           f"/ports/{target['port']}/{upstream_path}")
    headers = {}
    for key, value in request.headers.items():
        if key.lower() in _HOP_BY_HOP or key.lower() == "x-runner-token":
            continue
        headers[key] = value
    headers["X-Runner-Token"] = settings.SANDBOX_RUNNER_TOKEN
    headers["X-Forwarded-Host"] = request.headers.get("host") or ""
    headers["X-Forwarded-Proto"] = request.headers.get("x-forwarded-proto") or "http"
    if request.client:
        prior = request.headers.get("x-forwarded-for")
        client_ip = request.client.host
        headers["X-Forwarded-For"] = f"{prior}, {client_ip}" if prior else client_ip
    client = httpx.AsyncClient(
        timeout=httpx.Timeout(connect=10.0, read=600.0, write=600.0, pool=10.0),
        follow_redirects=False,
    )
    try:
        upstream_request = client.build_request(
            request.method, url, headers=headers,
            content=request.stream(),
            params=list(request.query_params.multi_items()),
        )
        upstream = await client.send(upstream_request, stream=True)
    except httpx.HTTPError as exc:
        await client.aclose()
        metrics.inc_preview_request(denied=False)
        logger.info("Preview 上游不可达 grant=%s: %s", target["grant_id"], exc)
        return JSONResponse(status_code=502, content={
            "code": "preview_upstream_unreachable",
            "message": "预览服务暂不可达，请确认服务已启动",
        })
    response_headers = {
        key: value for key, value in upstream.headers.items()
        if key.lower() not in _HOP_BY_HOP
    }
    content_type = upstream.headers.get("content-type") or ""
    response_headers.update(_security_headers(content_type))

    async def _body():
        try:
            async for chunk in upstream.aiter_raw():
                yield chunk
        finally:
            await upstream.aclose()
            await client.aclose()

    metrics.inc_preview_request(denied=False)
    return StreamingResponse(
        _body(), status_code=upstream.status_code,
        headers=response_headers, media_type=content_type or None,
    )


@app.websocket("/{path:path}")
async def proxy_ws(websocket: WebSocket, path: str):
    token, rest = _extract_token(websocket.headers.get("host") or "", websocket.url.path)
    if not token:
        await websocket.close(code=4404)
        return
    try:
        target = await _grant_target(token)
    except PreviewDenied:
        metrics.inc_preview_request(denied=True)
        await websocket.close(code=4403)
        return
    except Exception:  # noqa: BLE001
        metrics.inc_preview_request(denied=True)
        await websocket.close(code=1011)
        return
    if not await _acquire(target["grant_id"]):
        await websocket.close(code=4429)
        return
    if not await _rate_allowed(target["grant_id"]):
        await _release(target["grant_id"])
        await websocket.close(code=4429)
        return
    try:
        # 使用去掉 /p/{token} 前缀后的真实路径（Vite HMR 默认在 /）
        await _bridge_ws(websocket, target, rest)
    finally:
        await _release(target["grant_id"])


async def _bridge_ws(websocket: WebSocket, target: dict, path: str) -> None:
    runner_ws_base = _runner_base().replace("http://", "ws://").replace("https://", "wss://")
    upstream_url = (
        f"{runner_ws_base}/gateway/sandboxes/{target['sandbox_id']}"
        f"/ports/{target['port']}/ws/{path.lstrip('/')}"
    )
    query = "&".join(
        f"{key}={value}" for key, value in websocket.query_params.multi_items()
    )
    if query:
        upstream_url += f"?{query}"
    subprotocols = [
        item.strip()
        for item in (websocket.headers.get("sec-websocket-protocol") or "").split(",")
        if item.strip()
    ] or None
    headers = {"X-Runner-Token": settings.SANDBOX_RUNNER_TOKEN}
    for key in ("cookie", "origin", "user-agent"):
        value = websocket.headers.get(key)
        if value:
            headers[key] = value
    try:
        upstream = await websockets.connect(
            upstream_url, additional_headers=headers,
            subprotocols=subprotocols, open_timeout=15,
            ping_interval=None, max_size=None,
        )
    except Exception as exc:  # noqa: BLE001
        logger.info("Preview WebSocket 连接失败 grant=%s: %s", target["grant_id"], exc)
        await websocket.close(code=1011)
        return

    metrics.inc_preview_request(denied=False)
    await websocket.accept(subprotocol=upstream.subprotocol)

    async def client_to_upstream():
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
            except Exception:  # noqa: BLE001
                pass

    async def upstream_to_client():
        try:
            async for message in upstream:
                if isinstance(message, bytes):
                    await websocket.send_bytes(message)
                else:
                    await websocket.send_text(message)
        except Exception:  # noqa: BLE001
            pass
        finally:
            try:
                await websocket.close()
            except Exception:  # noqa: BLE001
                pass

    await asyncio.gather(client_to_upstream(), upstream_to_client(),
                         return_exceptions=True)
