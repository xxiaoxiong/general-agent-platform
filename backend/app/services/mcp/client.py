"""MCP 协议客户端（最小实现，零新增依赖）。

支持 MCP 两种主流传输：
- ``stdio``：本地子进程，JSONL（每行一个 JSON-RPC 2.0 消息）over stdin/stdout；
- ``streamable_http``：远端 HTTP 端点，POST JSON-RPC，响应为 application/json
  或 text/event-stream（SSE 帧），会话由 ``Mcp-Session-Id`` 头维持。

协议面只覆盖 Agent 接入所需最小集：initialize 握手 / tools/list / tools/call。
一次性会话语义（每次调用独立 建连→握手→请求→关闭），避免长连接生命周期治理；
单次调用超时由 AGENT_MCP_TIMEOUT_SECONDS 兜底。
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import threading
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import httpx

from app.core.config import settings

# MCP 协议版本（2025-06-18 为当前稳定版；服务端可协商回退）
PROTOCOL_VERSION = "2025-06-18"
CLIENT_INFO = {"name": "general-agent-platform", "version": "1.0"}


class McpClientError(Exception):
    """MCP 调用失败（连接 / 协议 / 远端错误的统一出口）。"""


@dataclass
class McpConnectionConfig:
    """MCP 服务连接配置（由 MCPServer 行派生，运行时随 agent_config 注入）。"""

    transport: str = "stdio"
    command: str = ""
    args: List[str] = field(default_factory=list)
    env: Dict[str, str] = field(default_factory=dict)
    url: str = ""
    headers: Dict[str, str] = field(default_factory=dict)
    timeout: float = 0.0

    def effective_timeout(self) -> float:
        return float(self.timeout or getattr(settings, "AGENT_MCP_TIMEOUT_SECONDS", 30.0))


@dataclass
class McpToolCallResult:
    """tools/call 结果：文本拼接 + 远端错误标记。"""

    text: str
    is_error: bool = False


def _new_id() -> str:
    return uuid.uuid4().hex


def _rpc(method: str, params: Optional[Dict[str, Any]], req_id: Optional[str]) -> Dict[str, Any]:
    msg: Dict[str, Any] = {"jsonrpc": "2.0", "method": method}
    if params is not None:
        msg["params"] = params
    if req_id is not None:
        msg["id"] = req_id
    return msg


def _check_error(resp: Dict[str, Any]) -> Dict[str, Any]:
    """JSON-RPC 响应校验：error 对象 / 缺 result 一律 McpClientError。"""
    if "error" in resp and resp["error"]:
        err = resp["error"]
        raise McpClientError(
            f"JSON-RPC 错误 {err.get('code')}: {err.get('message')}"
            + (f" ({err.get('data')})" if err.get("data") else "")
        )
    if "result" not in resp:
        raise McpClientError(f"响应缺少 result 字段: {json.dumps(resp, ensure_ascii=False)[:200]}")
    return resp["result"]


# ── stdio 传输（JSONL over 子进程 stdin/stdout）────────────────────────────────

class _StdioPeer:
    """一次性 stdio 会话：spawn 子进程 → 请求/响应按 id 配对 → 关闭。

    子进程 I/O 走 ``subprocess`` + 线程池（``asyncio.to_thread``），不依赖
    ``asyncio.create_subprocess_exec``——Windows 下 Selector 事件循环
    （如 persistence 导入时全局切换）不支持 asyncio 子进程。
    """

    def __init__(self, cfg: McpConnectionConfig):
        self.cfg = cfg
        self.proc: Optional[subprocess.Popen] = None
        self._stderr_tail: List[str] = []

    async def __aenter__(self) -> "_StdioPeer":
        if not (self.cfg.command or "").strip():
            raise McpClientError("stdio 传输缺少 command 配置")
        env = {**os.environ, **{str(k): str(v) for k, v in (self.cfg.env or {}).items()}}
        argv = [self.cfg.command, *[str(a) for a in (self.cfg.args or [])]]
        try:
            self.proc = await asyncio.to_thread(
                subprocess.Popen,
                argv,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
            )
        except OSError as e:
            raise McpClientError(f"启动 MCP 子进程失败: {argv[0]} ({e})")
        proc = self.proc

        def _drain_stderr() -> None:
            try:
                for raw in (proc.stderr or b""):
                    line = raw.decode("utf-8", errors="replace").rstrip()
                    if line:
                        self._stderr_tail = (self._stderr_tail + [line])[-20:]
            except (OSError, ValueError):
                pass

        self._stderr_task = threading.Thread(target=_drain_stderr, daemon=True)
        self._stderr_task.start()
        return self

    def _exit_reason(self) -> str:
        tail = "; ".join(self._stderr_tail[-5:])
        return f"MCP 子进程标准输出已关闭（进程退出）{': ' + tail if tail else ''}"

    async def __aexit__(self, *exc) -> None:
        proc = self.proc
        if proc is None:
            return
        try:
            if proc.stdin:
                proc.stdin.close()
        except OSError:
            pass

        def _shutdown() -> None:
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()

        await asyncio.to_thread(_shutdown)

    def _write_line(self, msg: Dict[str, Any]) -> None:
        assert self.proc and self.proc.stdin
        self.proc.stdin.write((json.dumps(msg, ensure_ascii=False) + "\n").encode("utf-8"))
        self.proc.stdin.flush()

    def _read_match(self, req_id: str) -> Any:
        assert self.proc and self.proc.stdout
        while True:
            raw = self.proc.stdout.readline()
            if not raw:
                raise McpClientError(self._exit_reason())
            text = raw.decode("utf-8", errors="replace").strip()
            if not text:
                continue
            try:
                msg = json.loads(text)
            except json.JSONDecodeError:
                continue  # 非协议行（子进程日志噪音）跳过
            if msg.get("id") != req_id:
                continue  # 通知/其他响应（进度通知等）跳过
            return _check_error(msg)

    async def request(self, method: str, params: Optional[Dict[str, Any]] = None) -> Any:
        req_id = _new_id()
        await asyncio.to_thread(self._write_line, _rpc(method, params, req_id))
        timeout = self.cfg.effective_timeout()
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(self._read_match, req_id), timeout=timeout
            )
        except asyncio.TimeoutError:
            raise McpClientError(f"MCP 请求超时（{timeout:.0f}s）: {method}")

    async def notify(self, method: str, params: Optional[Dict[str, Any]] = None) -> None:
        await asyncio.to_thread(self._write_line, _rpc(method, params, None))


# ── streamable_http 传输（POST JSON-RPC，JSON / SSE 响应）─────────────────────

class _StreamableHttpPeer:
    """一次性 HTTP 会话：同一 httpx client + Mcp-Session-Id 头维持会话。"""

    def __init__(self, cfg: McpConnectionConfig):
        self.cfg = cfg
        self.client: Optional[httpx.AsyncClient] = None
        self.session_id: Optional[str] = None

    async def __aenter__(self) -> "_StreamableHttpPeer":
        if not (self.cfg.url or "").strip():
            raise McpClientError("streamable_http 传输缺少 url 配置")
        verify = getattr(settings, "AGENT_CA_BUNDLE", "") or getattr(settings, "AGENT_TLS_VERIFY", True)
        self.client = httpx.AsyncClient(
            timeout=self.cfg.effective_timeout(),
            verify=verify,
            proxy=(getattr(settings, "AGENT_EGRESS_PROXY", "") or None),
        )
        return self

    async def __aexit__(self, *exc) -> None:
        # DELETE 终止服务端会话（尽力而为），再关闭连接
        try:
            if self.client and self.session_id:
                await self.client.delete(
                    self.cfg.url,
                    headers={**self._base_headers(), "Mcp-Session-Id": self.session_id},
                )
        except Exception:
            pass
        if self.client:
            await self.client.aclose()

    def _base_headers(self) -> Dict[str, str]:
        return {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            **{str(k): str(v) for k, v in (self.cfg.headers or {}).items()},
        }

    def _parse_sse(self, body: str, req_id: str) -> Any:
        for block in body.split("\n\n"):
            for line in block.splitlines():
                if line.startswith("data:"):
                    data = line[5:].strip()
                    if not data:
                        continue
                    try:
                        msg = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    if msg.get("id") == req_id:
                        return _check_error(msg)
        raise McpClientError("SSE 响应中未找到匹配请求的 JSON-RPC 响应")

    async def request(self, method: str, params: Optional[Dict[str, Any]] = None) -> Any:
        assert self.client
        req_id = _new_id()
        headers = self._base_headers()
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        try:
            resp = await self.client.post(
                self.cfg.url, json=_rpc(method, params, req_id), headers=headers
            )
        except httpx.HTTPError as e:
            raise McpClientError(f"MCP HTTP 请求失败: {e}")
        sid = resp.headers.get("mcp-session-id") or resp.headers.get("Mcp-Session-Id")
        if sid:
            self.session_id = sid
        if resp.status_code == 202:
            raise McpClientError(f"服务端仅返回 202 Accepted，未给出响应: {method}")
        if resp.status_code >= 400:
            raise McpClientError(f"HTTP {resp.status_code}: {resp.text[:200]}")
        ctype = (resp.headers.get("content-type") or "").lower()
        if "text/event-stream" in ctype:
            return self._parse_sse(resp.text, req_id)
        try:
            msg = resp.json()
        except Exception:
            raise McpClientError(f"响应不是合法 JSON: {resp.text[:200]}")
        return _check_error(msg)

    async def notify(self, method: str, params: Optional[Dict[str, Any]] = None) -> None:
        assert self.client
        headers = self._base_headers()
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        try:
            await self.client.post(self.cfg.url, json=_rpc(method, params, None), headers=headers)
        except httpx.HTTPError:
            pass  # 通知无响应语义，失败不影响主流程


# ── 公共 API ──────────────────────────────────────────────────────────────────

async def _open_peer(cfg: McpConnectionConfig):
    """建立传输会话并完成 initialize 握手。"""
    peer = _StreamableHttpPeer(cfg) if cfg.transport == "streamable_http" else _StdioPeer(cfg)
    await peer.__aenter__()
    try:
        init = await peer.request(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": CLIENT_INFO,
            },
        )
        await peer.notify("notifications/initialized")
    except Exception:
        await peer.__aexit__(None, None, None)
        raise
    return peer, init


async def list_tools(cfg: McpConnectionConfig) -> List[Dict[str, Any]]:
    """发现工具：tools/list（含分页游标续读）。"""
    peer, _ = await _open_peer(cfg)
    try:
        tools: List[Dict[str, Any]] = []
        cursor: Optional[str] = None
        while True:
            params: Dict[str, Any] = {"cursor": cursor} if cursor else {}
            result = await peer.request("tools/list", params)
            tools.extend(result.get("tools") or [])
            cursor = result.get("nextCursor")
            if not cursor:
                return tools
    finally:
        await peer.__aexit__(None, None, None)


async def call_tool(
    cfg: McpConnectionConfig, name: str, arguments: Optional[Dict[str, Any]] = None
) -> McpToolCallResult:
    """调用工具：tools/call，文本内容拼接返回，isError 透传。"""
    peer, _ = await _open_peer(cfg)
    try:
        result = await peer.request(
            "tools/call", {"name": name, "arguments": arguments or {}}
        )
    finally:
        await peer.__aexit__(None, None, None)
    parts: List[str] = []
    for item in result.get("content") or []:
        if isinstance(item, dict) and item.get("type") == "text":
            parts.append(str(item.get("text", "")))
        elif isinstance(item, dict):
            parts.append(f"[{item.get('type', 'unknown')} 内容已省略]")
    return McpToolCallResult(
        text="\n".join(p for p in parts if p) or "(无文本结果)",
        is_error=bool(result.get("isError")),
    )


async def test_connection(cfg: McpConnectionConfig) -> Dict[str, Any]:
    """连通性测试：握手 + 工具发现，返回客观结果（不抛异常）。"""
    try:
        peer, init = await _open_peer(cfg)
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}", "server_info": None, "tools": []}
    try:
        result = await peer.request("tools/list", {})
        tools = result.get("tools") or []
        return {
            "ok": True,
            "error": None,
            "server_info": init.get("serverInfo") or {},
            "tools": [
                {
                    "name": t.get("name"),
                    "description": t.get("description") or "",
                    "inputSchema": t.get("inputSchema") or {},
                }
                for t in tools
            ],
        }
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}", "server_info": init.get("serverInfo") or {}, "tools": []}
    finally:
        await peer.__aexit__(None, None, None)
