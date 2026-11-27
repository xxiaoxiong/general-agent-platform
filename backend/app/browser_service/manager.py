"""内置浏览器服务（browser-service）：共享 Chromium + 每会话独立 Context。

设计要点：
- 浏览器只在平台内部网络，不映射宿主机端口；
- 页面经 Preview Proxy 内部域名访问，Host 携带 Grant Token，仍由 Proxy 做授权校验；
- CDP ``Page.startScreencast`` 输出 JPEG 帧，WebSocket 二进制转发给 Backend；
- 键鼠 / 文本 / 滚动 / 历史导航由 Backend 转发为 JSON 控制消息。
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import socket
import time
from dataclasses import dataclass, field
from typing import Any, Optional
from urllib.parse import urlsplit

logger = logging.getLogger("browser_service")


class BrowserDisabledError(Exception):
    """未配置内部服务令牌。"""


class BrowserCapacityError(Exception):
    """并发浏览器会话超限。"""


class BrowserNavigationError(Exception):
    """页面打开失败或目标地址不被允许。"""


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "") or default)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, "") or default)
    except ValueError:
        return default


@dataclass
class BrowserSession:
    id: str
    url: str
    viewport: dict[str, int]
    context: Any
    page: Any
    cdp: Any
    created_at: float = field(default_factory=time.monotonic)
    last_active: float = field(default_factory=time.monotonic)
    clients: set[Any] = field(default_factory=set)
    screencast: bool = False
    closed: bool = False
    latest_frame: bytes = b""
    last_frame_at: float = 0.0
    frame_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    input_lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class BrowserManager:
    def __init__(self) -> None:
        self.token = (os.getenv("BROWSER_SERVICE_TOKEN") or "").strip()
        self.preview_host = (os.getenv("BROWSER_PREVIEW_HOST") or "preview-proxy").strip()
        self.preview_domain = (
            os.getenv("BROWSER_PREVIEW_DOMAIN") or "preview.internal"
        ).strip().lower()
        self.max_sessions = max(1, _env_int("BROWSER_MAX_SESSIONS", 4))
        self.session_ttl = max(60, _env_int("BROWSER_SESSION_TTL", 1800))
        self.idle_timeout = max(15, _env_int("BROWSER_SESSION_IDLE_TIMEOUT", 120))
        self.default_width = max(320, _env_int("BROWSER_VIEWPORT_WIDTH", 1280))
        self.default_height = max(240, _env_int("BROWSER_VIEWPORT_HEIGHT", 800))
        self.frame_quality = max(20, min(90, _env_int("BROWSER_FRAME_QUALITY", 60)))
        self.frame_interval = max(0.016, _env_float("BROWSER_FRAME_INTERVAL_MS", 70) / 1000.0)
        self.empty_browser_timeout = max(60, _env_int("BROWSER_EMPTY_TIMEOUT", 300))
        self._playwright: Any = None
        self._browser: Any = None
        self._proxy_ip = ""
        self._sessions: dict[str, BrowserSession] = {}
        self._lock = asyncio.Lock()
        self._reaper: Optional[asyncio.Task] = None
        self._empty_since: Optional[float] = None

    # ── 生命周期 ──────────────────────────────────────────────────────────
    async def start(self) -> None:
        if not self.token:
            logger.warning("BROWSER_SERVICE_TOKEN 未配置：内置浏览器会话请求将全部拒绝")
        self._reaper = asyncio.create_task(self._reap_loop())

    async def stop(self) -> None:
        if self._reaper is not None:
            self._reaper.cancel()
            try:
                await self._reaper
            except asyncio.CancelledError:
                pass
            self._reaper = None
        await self._reset_browser()
        if self._playwright is not None:
            try:
                await self._playwright.stop()
            except Exception:  # noqa: BLE001 关闭失败不阻断进程退出
                pass
            self._playwright = None

    def stats(self) -> dict:
        return {
            "sessions": len(self._sessions),
            "browser": bool(self._browser is not None and self._browser.is_connected()),
        }

    def get(self, session_id: str) -> Optional[BrowserSession]:
        session = self._sessions.get(session_id)
        if session is None or session.closed:
            return None
        return session

    # ── 会话 ──────────────────────────────────────────────────────────────
    async def create_session(
        self, session_id: str, url: str, *, width: int, height: int,
    ) -> BrowserSession:
        if not self.token:
            raise BrowserDisabledError("内置浏览器未启用（BROWSER_SERVICE_TOKEN 为空）")
        if not self._allowed_url(url):
            raise BrowserNavigationError("只允许打开平台 Preview Proxy 的内部地址")
        width = max(320, min(3840, int(width or self.default_width)))
        height = max(240, min(2160, int(height or self.default_height)))
        async with self._lock:
            if session_id in self._sessions:
                raise BrowserNavigationError("浏览器会话已存在")
            if len(self._sessions) >= self.max_sessions:
                raise BrowserCapacityError(
                    f"内置浏览器并发会话已达上限（{self.max_sessions}），请关闭其他预览后重试"
                )
            await self._ensure_browser()
            context = await self._browser.new_context(
                viewport={"width": width, "height": height},
                device_scale_factor=1,
                accept_downloads=False,
                ignore_https_errors=True,
                user_agent=(
                    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
                ),
            )
            page = await context.new_page()
            page.set_default_timeout(15_000)
            cdp = await context.new_cdp_session(page)
            session = BrowserSession(
                id=session_id, url=url,
                viewport={"width": width, "height": height},
                context=context, page=page, cdp=cdp,
            )
            cdp.on("Page.screencastFrame", lambda event: self._schedule_frame(session, event))
            page.on("framenavigated", lambda frame: self._on_navigated(session, frame))
            page.on("load", lambda: self._schedule_meta(session))
            page.on("crash", lambda: self._schedule_error(session, "页面渲染进程崩溃"))
            self._sessions[session_id] = session
            self._empty_since = None
        try:
            response = await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
        except Exception as exc:  # noqa: BLE001 Playwright 网络错误统一转换为可读信息
            await self.close_session(session_id)
            raise BrowserNavigationError(f"页面打开失败：{exc}") from exc
        if response is not None and response.status >= 400:
            await self.close_session(session_id)
            raise BrowserNavigationError(
                f"预览上游返回 HTTP {response.status}，请确认服务已启动"
            )
        return session

    async def close_session(self, session_id: str) -> None:
        session = self._sessions.pop(session_id, None)
        if session is None:
            return
        session.closed = True
        for websocket in list(session.clients):
            try:
                await websocket.close(code=1000)
            except Exception:  # noqa: BLE001 客户端可能已断开
                pass
        session.clients.clear()
        try:
            await session.cdp.send("Page.stopScreencast")
        except Exception:  # noqa: BLE001 未开始投屏或页面已关闭
            pass
        try:
            await session.context.close()
        except Exception as exc:  # noqa: BLE001 关闭失败只记日志
            logger.info("关闭浏览器 Context 失败 session=%s: %s", session_id, exc)

    # ── 客户端接入 / 输入 ─────────────────────────────────────────────────
    async def attach(self, session: BrowserSession, websocket: Any) -> None:
        session.clients.add(websocket)
        session.last_active = time.monotonic()
        await websocket.send_text(json.dumps({
            "type": "viewport",
            "width": session.viewport["width"],
            "height": session.viewport["height"],
        }))
        if not session.screencast:
            await self._start_screencast(session)
        if session.latest_frame:
            try:
                await websocket.send_bytes(session.latest_frame)
            except Exception:  # noqa: BLE001 首个帧发送失败不阻断后续
                pass
        await self._send_meta(session)

    async def detach(self, session: BrowserSession, websocket: Any) -> None:
        session.clients.discard(websocket)
        session.last_active = time.monotonic()
        if session.closed or session.clients:
            return
        # 无客户端时停止投屏，避免服务端 Chromium 空转
        session.screencast = False
        try:
            await session.cdp.send("Page.stopScreencast")
        except Exception:  # noqa: BLE001 页面可能已关闭
            pass

    async def handle_input(self, session: BrowserSession, message: dict) -> None:
        """执行一条来自控制台的输入消息（已由 Backend 鉴权）。"""
        if session.closed:
            return
        kind = str(message.get("type") or "")
        async with session.input_lock:
            page = session.page
            try:
                if kind == "mouse":
                    await self._handle_mouse(session, message)
                elif kind == "key":
                    action = str(message.get("action") or "")
                    key = str(message.get("key") or "")[:32]
                    if not key:
                        return
                    if action == "down":
                        await page.keyboard.down(key)
                    elif action == "up":
                        await page.keyboard.up(key)
                    elif action == "press":
                        await page.keyboard.press(key)
                elif kind == "shortcut":
                    keys = str(message.get("keys") or "")[:64]
                    if keys:
                        await page.keyboard.press(keys)
                elif kind == "text":
                    text = str(message.get("text") or "")[:4096]
                    if text:
                        await page.keyboard.insert_text(text)
                elif kind == "resize":
                    await self._resize(session, message)
                elif kind == "navigate":
                    action = str(message.get("action") or "")
                    if action == "back":
                        await page.go_back(wait_until="domcontentloaded")
                    elif action == "forward":
                        await page.go_forward(wait_until="domcontentloaded")
                    elif action == "reload":
                        await page.reload(wait_until="domcontentloaded")
            except Exception as exc:  # noqa: BLE001 输入失败不终止连接
                logger.info("浏览器输入执行失败 session=%s type=%s: %s", session.id, kind, exc)

    async def _handle_mouse(self, session: BrowserSession, message: dict) -> None:
        page = session.page
        action = str(message.get("action") or "")
        width = session.viewport["width"]
        height = session.viewport["height"]
        try:
            x = max(0.0, min(float(width), float(message.get("x") or 0)))
            y = max(0.0, min(float(height), float(message.get("y") or 0)))
        except (TypeError, ValueError):
            return
        button = str(message.get("button") or "left")
        if button not in ("left", "right", "middle"):
            button = "left"
        if action == "move":
            await page.mouse.move(x, y)
        elif action == "down":
            await page.mouse.move(x, y)
            await page.mouse.down(button=button)
        elif action == "up":
            await page.mouse.up(button=button)
        elif action == "dblclick":
            await page.mouse.dblclick(x, y, button=button)
        elif action == "wheel":
            await page.mouse.wheel(
                float(message.get("deltaX") or 0), float(message.get("deltaY") or 0),
            )

    async def _resize(self, session: BrowserSession, message: dict) -> None:
        try:
            width = max(320, min(3840, int(message.get("width") or 0)))
            height = max(240, min(2160, int(message.get("height") or 0)))
        except (TypeError, ValueError):
            return
        if width == session.viewport["width"] and height == session.viewport["height"]:
            return
        session.viewport = {"width": width, "height": height}
        await session.page.set_viewport_size({"width": width, "height": height})
        if session.screencast:
            await self._stop_screencast(session)
            await self._start_screencast(session)
        await self._broadcast_text(session, json.dumps({
            "type": "viewport", "width": width, "height": height,
        }))

    # ── 帧与事件 ──────────────────────────────────────────────────────────
    def _schedule_frame(self, session: BrowserSession, event: dict) -> None:
        if session.closed:
            return
        asyncio.create_task(self._handle_frame(session, event))

    async def _handle_frame(self, session: BrowserSession, event: dict) -> None:
        async with session.frame_lock:
            if session.closed:
                return
            wait = self.frame_interval - (time.monotonic() - session.last_frame_at)
            if wait > 0:
                await asyncio.sleep(wait)
            if session.closed:
                return
            session.last_frame_at = time.monotonic()
            try:
                frame = base64.b64decode(event.get("data") or "")
            except (ValueError, TypeError):
                frame = b""
            if frame:
                session.latest_frame = frame
                await self._broadcast_bytes(session, frame)
            frame_id = event.get("sessionId")
            if frame_id is not None:
                try:
                    await session.cdp.send("Page.screencastFrameAck", {"sessionId": frame_id})
                except Exception:  # noqa: BLE001 页面关闭后 ACK 失败可忽略
                    pass

    async def _start_screencast(self, session: BrowserSession) -> None:
        if session.closed or session.screencast:
            return
        await session.cdp.send("Page.startScreencast", {
            "format": "jpeg",
            "quality": self.frame_quality,
            "maxWidth": session.viewport["width"],
            "maxHeight": session.viewport["height"],
            "everyNthFrame": 1,
        })
        session.screencast = True

    async def _stop_screencast(self, session: BrowserSession) -> None:
        session.screencast = False
        try:
            await session.cdp.send("Page.stopScreencast")
        except Exception:  # noqa: BLE001 页面关闭后停止投屏失败可忽略
            pass

    def _on_navigated(self, session: BrowserSession, frame: Any) -> None:
        if session.closed or frame != session.page.main_frame:
            return
        asyncio.create_task(self._broadcast_text(
            session, json.dumps({"type": "navigated", "url": frame.url}),
        ))

    def _schedule_meta(self, session: BrowserSession) -> None:
        if session.closed:
            return
        asyncio.create_task(self._send_meta(session))

    def _schedule_error(self, session: BrowserSession, message: str) -> None:
        if session.closed:
            return
        asyncio.create_task(self._broadcast_text(
            session, json.dumps({"type": "error", "message": message}, ensure_ascii=False),
        ))

    async def _send_meta(self, session: BrowserSession) -> None:
        title = ""
        url = session.url
        try:
            title = await session.page.title()
            url = session.page.url
        except Exception:  # noqa: BLE001 页面已关闭
            pass
        await self._broadcast_text(session, json.dumps(
            {"type": "meta", "url": url, "title": title}, ensure_ascii=False,
        ))

    async def _broadcast_bytes(self, session: BrowserSession, payload: bytes) -> None:
        for websocket in list(session.clients):
            try:
                await websocket.send_bytes(payload)
            except Exception:  # noqa: BLE001 客户端断开后移除
                session.clients.discard(websocket)

    async def _broadcast_text(self, session: BrowserSession, payload: str) -> None:
        for websocket in list(session.clients):
            try:
                await websocket.send_text(payload)
            except Exception:  # noqa: BLE001 客户端断开后移除
                session.clients.discard(websocket)

    # ── Chromium 生命周期 ─────────────────────────────────────────────────
    async def _ensure_browser(self) -> None:
        if not self.token:
            raise BrowserDisabledError("内置浏览器未启用（BROWSER_SERVICE_TOKEN 为空）")
        proxy_ip = await asyncio.to_thread(self._resolve_proxy_ip)
        if self._browser is not None:
            if not self._browser.is_connected():
                await self._reset_browser()
            elif proxy_ip and proxy_ip != self._proxy_ip:
                logger.info(
                    "Preview Proxy 地址变化（%s → %s），重建浏览器进程",
                    self._proxy_ip, proxy_ip,
                )
                await self._reset_browser()
        if self._browser is not None:
            return
        from playwright.async_api import async_playwright

        if self._playwright is None:
            self._playwright = await async_playwright().start()
        args = [
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-gpu",
            "--hide-scrollbars",
            "--mute-audio",
            "--disable-background-timer-throttling",
            "--disable-renderer-backgrounding",
            "--disable-backgrounding-occluded-windows",
        ]
        if proxy_ip:
            # 把内部 Preview 域名解析到 preview-proxy 容器地址；Host 仍携带 Grant Token
            args.append(
                f"--host-resolver-rules=MAP *.{self.preview_domain} {proxy_ip}"
            )
        self._browser = await self._playwright.chromium.launch(
            headless=True, args=args, timeout=30_000,
        )
        self._proxy_ip = proxy_ip
        logger.info("服务端 Chromium 已启动（preview-proxy=%s）", proxy_ip or "未知")

    async def _reset_browser(self) -> None:
        for session_id in list(self._sessions):
            await self.close_session(session_id)
        if self._browser is not None:
            try:
                await self._browser.close()
            except Exception:  # noqa: BLE001 关闭失败不阻断重建
                pass
            self._browser = None
        self._proxy_ip = ""
        self._empty_since = None

    def _resolve_proxy_ip(self) -> str:
        try:
            infos = socket.getaddrinfo(
                self.preview_host, None, family=socket.AF_INET,
                type=socket.SOCK_STREAM,
            )
        except OSError:
            return ""
        return infos[0][4][0] if infos else ""

    def _allowed_url(self, url: str) -> bool:
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https"):
            return False
        host = (parts.hostname or "").lower()
        return host == self.preview_domain or host.endswith("." + self.preview_domain)

    async def _reap_loop(self) -> None:
        while True:
            await asyncio.sleep(15)
            now = time.monotonic()
            expired: list[str] = []
            async with self._lock:
                for session_id, session in self._sessions.items():
                    idle = (
                        not session.clients
                        and now - session.last_active > self.idle_timeout
                    )
                    too_old = now - session.created_at > self.session_ttl
                    if idle or too_old:
                        expired.append(session_id)
            for session_id in expired:
                await self.close_session(session_id)
            if self._sessions:
                self._empty_since = None
                continue
            if self._empty_since is None:
                self._empty_since = now
            elif self._browser is not None and now - self._empty_since > self.empty_browser_timeout:
                logger.info("长时间无浏览器会话，释放服务端 Chromium")
                await self._reset_browser()
