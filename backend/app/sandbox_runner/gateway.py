"""Runner Gateway：Backend 工具调用 / Preview Proxy 到 Sandbox 的受控转发。

- 只转发「已登记 Sandbox + 已登记 Port」，不接受任意 upstream URL（SSRF 防护）；
- 服务令牌认证（X-Runner-Token）；
- HTTP 流式转发 + WebSocket 双向转发（Vite HMR）；
- Host 重写为 localhost:{port}（兼容 Vite allowedHosts），原始 Host 通过
  X-Forwarded-Host 传递。
"""
from __future__ import annotations

import hmac
import logging
from typing import Optional

import httpx
import websockets
from fastapi import APIRouter, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, Response, StreamingResponse
from sqlalchemy import select

from app.core.config import settings
from app.core.database import AsyncSessionLocal
from app.models.sandbox import SandboxInstance, WorkspaceService
from app.services.sandbox.runtime import agent_token

logger = logging.getLogger("ai_plat.sandbox.runner.gateway")

router = APIRouter()

_HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "host", "content-length",
    "x-runner-token",
}

_ENGINE = None  # 由 api.py 注入 DockerEngine


def set_engine(engine) -> None:
    global _ENGINE
    _ENGINE = engine


def _authorized(token: Optional[str]) -> bool:
    expected = (settings.SANDBOX_RUNNER_TOKEN or "").strip()
    return bool(expected) and bool(token) and hmac.compare_digest(token.strip(), expected)


async def _sandbox_row(sandbox_id: int) -> Optional[SandboxInstance]:
    async with AsyncSessionLocal() as db:
        return (await db.execute(
            select(SandboxInstance).where(SandboxInstance.id == sandbox_id)
        )).scalar_one_or_none()


async def _resolve(sandbox_id: int, port: int, *, check_service: bool) -> tuple[str, dict]:
    sandbox = await _sandbox_row(sandbox_id)
    if sandbox is None:
        raise LookupError("sandbox_not_found")
    if sandbox.observed_state != "ready" or sandbox.desired_state != "running":
        raise LookupError("sandbox_not_ready")
    if check_service:
        async with AsyncSessionLocal() as db:
            service = (await db.execute(
                select(WorkspaceService).where(
                    WorkspaceService.sandbox_id == sandbox.id,
                    WorkspaceService.port == int(port),
                )
            )).scalar_one_or_none()
        if service is None or scope_not_running(service):
            raise LookupError("service_not_registered")
    payload = {"sandbox_id": sandbox.id, "generation": sandbox.generation}
    container_id = await _find_container(payload)
    if container_id is None:
        raise LookupError("sandbox_container_missing")
    network = f"gap-net-{sandbox.id}-g{sandbox.generation}"
    ip = await _ENGINE.container_ip(container_id, network)
    if not ip:
        raise LookupError("sandbox_network_unavailable")
    return ip, {"generation": sandbox.generation, "workspace_id": sandbox.workspace_id}


def scope_not_running(service: WorkspaceService) -> bool:
    return service.desired_state != "running" or service.observed_state in (
        "stopped", "exited", "failed",
    )


async def _find_container(payload: dict) -> Optional[str]:
    containers = await _ENGINE.list_containers({
        "platform.managed": "true",
        "platform.sandbox_id": str(payload["sandbox_id"]),
        "platform.generation": str(payload["generation"]),
    })
    if not containers:
        return None
    running = [c for c in containers if str(c.get("State")) == "running"]
    return (running or containers)[0]["Id"]


def _error(status: int, code: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"code": code, "message": code})


def _forward_headers(request: Request, *, target_port: int) -> dict:
    headers = {}
    for key, value in request.headers.items():
        if key.lower() in _HOP_BY_HOP:
            continue
        headers[key] = value
    original_host = request.headers.get("x-forwarded-host") or request.headers.get("host") or ""
    headers["Host"] = f"localhost:{target_port}"
    headers["X-Forwarded-Host"] = original_host
    headers["X-Forwarded-Proto"] = request.headers.get("x-forwarded-proto") or "http"
    client_ip = request.client.host if request.client else ""
    if client_ip:
        prior = request.headers.get("x-forwarded-for")
        headers["X-Forwarded-For"] = f"{prior}, {client_ip}" if prior else client_ip
    if "origin" in headers:
        headers["Origin"] = f"http://localhost:{target_port}"
    return headers


@router.api_route(
    "/gateway/sandboxes/{sandbox_id}/agent/{path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"],
)
async def proxy_agent(request: Request, sandbox_id: int, path: str):
    if not _authorized(request.headers.get("x-runner-token")):
        return _error(401, "runner_auth_failed")
    if _ENGINE is None:
        return _error(503, "runner_not_ready")
    try:
        ip, meta = await _resolve(sandbox_id, int(settings.SANDBOX_AGENT_PORT),
                                  check_service=False)
    except LookupError as exc:
        return _error(404, str(exc))
    sandbox = await _sandbox_row(sandbox_id)
    token = agent_token(sandbox_id, sandbox.generation if sandbox else 0)
    headers = _forward_headers(request, target_port=int(settings.SANDBOX_AGENT_PORT))
    headers["Authorization"] = f"Bearer {token}"
    return await _http_proxy(request, f"http://{ip}:{settings.SANDBOX_AGENT_PORT}/{path}",
                             headers, timeout=600.0)


@router.api_route(
    "/gateway/sandboxes/{sandbox_id}/ports/{port}/{path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"],
)
async def proxy_port(request: Request, sandbox_id: int, port: int, path: str):
    if not _authorized(request.headers.get("x-runner-token")):
        return _error(401, "runner_auth_failed")
    if _ENGINE is None:
        return _error(503, "runner_not_ready")
    try:
        ip, _meta = await _resolve(sandbox_id, int(port), check_service=True)
    except LookupError as exc:
        return _error(404, str(exc))
    headers = _forward_headers(request, target_port=int(port))
    return await _http_proxy(request, f"http://{ip}:{int(port)}/{path}", headers, timeout=600.0)


async def _http_proxy(request: Request, url: str, headers: dict, *, timeout: float):
    # workspace-agent 基于 stdlib http.server，只识别 Content-Length，不支持 chunked
    # 请求体；这里先缓冲并显式发送 Content-Length（上限 20MB）。
    try:
        body = await request.body()
    except Exception:  # noqa: BLE001 客户端断开
        body = b""
    if len(body) > 20 * 1024 * 1024:
        return _error(413, "request_body_too_large")
    client = httpx.AsyncClient(
        timeout=httpx.Timeout(connect=10.0, read=timeout, write=timeout, pool=10.0),
        follow_redirects=False,
    )
    try:
        upstream_request = client.build_request(
            request.method, url,
            headers=headers,
            content=body if body else None,
            params=list(request.query_params.multi_items()),
        )
        upstream = await client.send(upstream_request, stream=True)
    except httpx.HTTPError as exc:
        await client.aclose()
        return _error(502, f"upstream_unreachable: {exc}"[:400])

    response_headers = {
        key: value for key, value in upstream.headers.items()
        if key.lower() not in _HOP_BY_HOP
    }
    media_type = upstream.headers.get("content-type")

    async def _body():
        try:
            async for chunk in upstream.aiter_raw():
                yield chunk
        finally:
            await upstream.aclose()
            await client.aclose()

    return StreamingResponse(
        _body(), status_code=upstream.status_code,
        headers=response_headers, media_type=media_type,
    )


@router.websocket("/gateway/sandboxes/{sandbox_id}/ports/{port}/ws/{path:path}")
async def proxy_port_ws(websocket: WebSocket, sandbox_id: int, port: int, path: str):
    token = websocket.headers.get("x-runner-token") or websocket.query_params.get("__token")
    if not _authorized(token):
        await websocket.close(code=4401)
        return
    if _ENGINE is None:
        await websocket.close(code=1011)
        return
    try:
        ip, _meta = await _resolve(sandbox_id, int(port), check_service=True)
    except LookupError:
        await websocket.close(code=4404)
        return

    incoming_headers = {}
    for key, value in websocket.headers.items():
        if key.lower() in {
            "host", "connection", "upgrade", "sec-websocket-key",
            "sec-websocket-version", "sec-websocket-extensions",
            "sec-websocket-protocol", "origin", "x-runner-token",
        }:
            continue
        incoming_headers[key.lower()] = value
    # 统一小写键，避免与显式 Origin 形成重复头（Vite WS 会拒绝/挂起）
    incoming_headers["origin"] = f"http://localhost:{int(port)}"
    subprotocols = [
        item.strip()
        for item in (websocket.headers.get("sec-websocket-protocol") or "").split(",")
        if item.strip()
    ] or None
    upstream_url = f"ws://{ip}:{int(port)}/{path}"
    if websocket.query_params:
        query = "&".join(
            f"{key}={value}" for key, value in websocket.query_params.multi_items()
            if key != "__token"
        )
        if query:
            upstream_url += f"?{query}"
    try:
        upstream = await websockets.connect(
            upstream_url,
            additional_headers=incoming_headers,
            subprotocols=subprotocols,
            open_timeout=15,
            ping_interval=None,
            max_size=None,
        )
    except Exception as exc:  # noqa: BLE001
        logger.info("WebSocket 上游连接失败 %s: %s", upstream_url, exc)
        await websocket.close(code=1011)
        return

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

    import asyncio

    await asyncio.gather(client_to_upstream(), upstream_to_client(),
                         return_exceptions=True)


@router.get("/gateway/health")
async def gateway_health():
    return {"status": "ok", "socket": bool(_ENGINE)}
