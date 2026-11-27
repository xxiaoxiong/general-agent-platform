"""browser-service HTTP / WebSocket 入口（仅内部网络，不映射宿主机端口）。

Backend → POST /sessions（创建）→ WS /sessions/{id}/stream（帧 + 输入）
所有端点要求 ``Authorization: Bearer <BROWSER_SERVICE_TOKEN>``。
"""
from __future__ import annotations

import json
import logging
import re
import secrets
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect

from .manager import (
    BrowserCapacityError, BrowserDisabledError, BrowserManager, BrowserNavigationError,
)

logger = logging.getLogger("browser_service")
manager = BrowserManager()

_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,80}$")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    await manager.start()
    try:
        yield
    finally:
        await manager.stop()


app = FastAPI(title="Built-in Browser Service", lifespan=lifespan)


def _credential(raw: str) -> str:
    value = (raw or "").strip()
    if value.lower().startswith("bearer "):
        return value[7:].strip()
    return value


def _authorized(raw: str) -> bool:
    """接受 ``Bearer <token>`` 头或裸 token（WebSocket 查询参数）。"""
    token = manager.token
    candidate = _credential(raw)
    return bool(token and candidate and secrets.compare_digest(candidate, token))


def _require_http_auth(request: Request) -> None:
    if not _authorized(request.headers.get("authorization") or ""):
        raise HTTPException(status_code=401, detail="内部服务令牌无效")


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "service": "browser-service",
        "enabled": bool(manager.token),
        **manager.stats(),
    }


@app.post("/sessions")
async def create_session(request: Request):
    _require_http_auth(request)
    try:
        payload = await request.json()
    except ValueError:
        raise HTTPException(status_code=400, detail="请求体必须为 JSON") from None
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="请求体必须为 JSON 对象")
    session_id = str(payload.get("session_id") or "").strip()
    url = str(payload.get("url") or "").strip()
    if not _SESSION_ID_RE.match(session_id):
        raise HTTPException(status_code=400, detail="session_id 不合法")
    if not url.lower().startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail="url 必须为 HTTP(S) 地址")
    viewport = payload.get("viewport") or {}
    if not isinstance(viewport, dict):
        viewport = {}
    try:
        width = int(viewport.get("width") or 0)
        height = int(viewport.get("height") or 0)
    except (TypeError, ValueError):
        width, height = 0, 0
    try:
        session = await manager.create_session(
            session_id, url, width=width, height=height,
        )
    except BrowserDisabledError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except BrowserCapacityError as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    except BrowserNavigationError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 未预期异常记录日志并返回可读错误
        logger.exception("创建浏览器会话失败 session=%s", session_id)
        raise HTTPException(status_code=500, detail=f"浏览器会话创建失败：{exc}") from exc
    return {
        "id": session.id,
        "url": session.url,
        "viewport": session.viewport,
    }


@app.get("/sessions/{session_id}")
async def get_session(session_id: str, request: Request):
    _require_http_auth(request)
    session = manager.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="浏览器会话不存在或已结束")
    return {"id": session.id, "url": session.url, "viewport": session.viewport}


@app.delete("/sessions/{session_id}")
async def delete_session(session_id: str, request: Request):
    _require_http_auth(request)
    await manager.close_session(session_id)
    return {"status": "closed", "id": session_id}


@app.websocket("/sessions/{session_id}/stream")
async def stream_session(websocket: WebSocket, session_id: str):
    raw_token = (
        websocket.query_params.get("token")
        or websocket.headers.get("authorization")
        or ""
    )
    if not _authorized(raw_token):
        await websocket.close(code=4403)
        return
    session = manager.get(session_id)
    if session is None:
        await websocket.close(code=4404)
        return
    await websocket.accept()
    await manager.attach(session, websocket)
    try:
        while True:
            message = await websocket.receive()
            if message.get("type") == "websocket.disconnect":
                break
            text = message.get("text")
            if not text:
                continue
            try:
                payload = json.loads(text)
            except ValueError:
                continue
            if isinstance(payload, dict):
                await manager.handle_input(session, payload)
    except WebSocketDisconnect:
        pass
    except Exception:  # noqa: BLE001 连接异常仅记录，不阻塞其他会话
        logger.info("浏览器会话 WebSocket 异常 session=%s", session_id, exc_info=True)
    finally:
        await manager.detach(session, websocket)
