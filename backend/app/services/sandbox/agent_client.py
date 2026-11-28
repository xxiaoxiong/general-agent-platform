"""Backend -> Sandbox Runner Gateway -> workspace-agent 客户端。

Backend 不访问 Docker Socket，也不解析 Sandbox 容器 IP；所有命令、文件、Service
调用都经 Runner Gateway 受控转发（任务书 §4.3 / §7.5 / §9.2）。
"""
from __future__ import annotations

from typing import Any, Optional

import httpx

from app.core.config import settings

from .common import SandboxOperationError, redact_log_text


def _base_url() -> str:
    return (settings.SANDBOX_RUNNER_CONTROL_URL or "").rstrip("/")


def _headers() -> dict:
    return {"X-Runner-Token": settings.SANDBOX_RUNNER_TOKEN}


async def _request(
    method: str,
    path: str,
    *,
    json: Optional[dict] = None,
    params: Optional[dict] = None,
    timeout: float = 60.0,
) -> dict:
    url = f"{_base_url()}{path}"
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.request(
                method, url, json=json, params=params, headers=_headers(),
            )
    except httpx.HTTPError as exc:
        raise SandboxOperationError(
            f"Sandbox Runner 不可达（{_base_url()}）：{exc}"
        ) from exc
    if response.status_code >= 400:
        detail: Any
        try:
            detail = response.json()
            message = detail.get("detail") or detail.get("error") or detail
        except Exception:  # noqa: BLE001
            message = response.text[:500]
        raise SandboxOperationError(
            f"Sandbox 操作失败（HTTP {response.status_code}）：{message}",
            code="sandbox_agent_error",
        )
    try:
        return response.json()
    except Exception as exc:  # noqa: BLE001
        raise SandboxOperationError(f"Sandbox 返回非 JSON 响应：{exc}") from exc


def agent_path(sandbox_id: int, suffix: str) -> str:
    return f"/gateway/sandboxes/{int(sandbox_id)}/agent/{suffix.lstrip('/')}"


# ── 命令 ─────────────────────────────────────────────────────────────────

async def health(sandbox_id: int) -> dict:
    return await _request("GET", agent_path(sandbox_id, "health"), timeout=15)


async def exec_command(
    sandbox_id: int, command: str, *, working_directory: str = "/workspace",
    timeout_seconds: int = 600, wait_seconds: float = 30.0,
) -> dict:
    return await _request(
        "POST", agent_path(sandbox_id, "exec"),
        json={
            "command": command,
            "working_directory": working_directory,
            "timeout_seconds": int(timeout_seconds),
            "wait_seconds": float(wait_seconds),
        },
        timeout=max(30.0, float(wait_seconds) + 30.0),
    )


async def operation_status(sandbox_id: int, operation_id: str) -> dict:
    return await _request("GET", agent_path(sandbox_id, f"exec/{operation_id}"), timeout=30)


async def cancel_operation(sandbox_id: int, operation_id: str) -> dict:
    return await _request(
        "POST", agent_path(sandbox_id, f"exec/{operation_id}/cancel"), timeout=30,
    )


# ── 文件 ─────────────────────────────────────────────────────────────────

async def list_dir(sandbox_id: int, path: str = "/workspace") -> dict:
    return await _request("GET", agent_path(sandbox_id, "files/list"),
                          params={"path": path}, timeout=60)


async def read_file(
    sandbox_id: int, path: str, *, offset: int = 0, limit: int = 2000,
) -> dict:
    return await _request(
        "POST", agent_path(sandbox_id, "files/read"),
        json={"path": path, "offset": int(offset), "limit": int(limit)},
        timeout=60,
    )


async def write_file(sandbox_id: int, path: str, content: str,
                     *, encoding: str = "utf-8") -> dict:
    return await _request(
        "POST", agent_path(sandbox_id, "files/write"),
        json={"path": path, "content": content, "encoding": encoding}, timeout=120,
    )


async def edit_file(
    sandbox_id: int, path: str, old_string: str, new_string: str,
    replace_all: bool = False,
) -> dict:
    return await _request(
        "POST", agent_path(sandbox_id, "files/edit"),
        json={"path": path, "old_string": old_string, "new_string": new_string,
              "replace_all": bool(replace_all)},
        timeout=120,
    )


async def delete_path(sandbox_id: int, path: str) -> dict:
    return await _request(
        "POST", agent_path(sandbox_id, "files/delete"),
        json={"path": path}, timeout=60,
    )


async def grep_files(
    sandbox_id: int, pattern: str, *, path: str = "/workspace",
    glob: Optional[str] = None, max_count: int = 200,
) -> dict:
    return await _request(
        "POST", agent_path(sandbox_id, "files/grep"),
        json={"pattern": pattern, "path": path, "glob": glob,
              "max_count": int(max_count)},
        timeout=90,
    )


async def glob_files(
    sandbox_id: int, pattern: str, *, path: str = "/workspace",
) -> dict:
    return await _request(
        "POST", agent_path(sandbox_id, "files/glob"),
        json={"pattern": pattern, "path": path}, timeout=90,
    )


async def read_raw(sandbox_id: int, path: str) -> dict:
    """读取原始字节（base64），用于文件面板下载；服务端限制单文件大小。"""
    return await _request(
        "POST", agent_path(sandbox_id, "files/raw"),
        json={"path": path}, timeout=120,
    )


# ── Service ──────────────────────────────────────────────────────────────

async def start_service(sandbox_id: int, config: dict) -> dict:
    return await _request(
        "POST", agent_path(sandbox_id, "services"), json=config, timeout=120,
    )


async def list_services(sandbox_id: int) -> dict:
    return await _request("GET", agent_path(sandbox_id, "services"), timeout=30)


async def service_status(sandbox_id: int, name: str) -> dict:
    return await _request("GET", agent_path(sandbox_id, f"services/{name}"), timeout=30)


async def stop_service(sandbox_id: int, name: str) -> dict:
    return await _request(
        "POST", agent_path(sandbox_id, f"services/{name}/stop"), timeout=60,
    )


async def restart_service(sandbox_id: int, name: str) -> dict:
    return await _request(
        "POST", agent_path(sandbox_id, f"services/{name}/restart"), timeout=120,
    )


async def stop_all_services(sandbox_id: int) -> dict:
    """停止全部服务并清理脱离进程组的残留进程。"""
    return await _request(
        "POST", agent_path(sandbox_id, "services/stop-all"), timeout=120,
    )


async def service_logs(sandbox_id: int, name: str, *, tail: int = 200) -> dict:
    data = await _request(
        "GET", agent_path(sandbox_id, f"services/{name}/logs"),
        params={"tail": int(tail)}, timeout=60,
    )
    if isinstance(data.get("content"), str):
        data["content"] = redact_log_text(data["content"], limit=100_000)
    return data
