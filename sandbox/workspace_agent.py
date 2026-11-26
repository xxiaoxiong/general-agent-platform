#!/usr/bin/env python3
"""workspace-agent：Sandbox 内的受控执行代理（仅标准库）。

职责（任务书 §4.3 / §9.1 / §10.1）：
- 命令执行：同步等待 / 异步 Operation / 进程组取消 / 输出截断；
- 文件读写 / 列表 / grep / glob / 原始下载，全部限定在 /workspace；
- 长期 Service：启动、状态、健康检查、日志轮转、自动重启、进程组清理；
- 只接受 Runner Gateway 的 Bearer Token 请求；不访问 Docker Socket。

安全：
- 所有路径解析到 WORKSPACE_ROOT 之下（realpath + commonpath），拒绝符号链接逃逸；
- 子进程环境移除 WORKSPACE_AGENT_TOKEN，避免凭据进入用户命令；
- 命令必须设置超时与最大输出，避免无限输出拖垮 Sandbox。
"""
from __future__ import annotations

import base64
import fnmatch
import json
import mimetypes
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Optional
from urllib.parse import parse_qs, urlsplit

WORKSPACE_ROOT = os.path.realpath(os.environ.get("WORKSPACE_ROOT", "/workspace"))
AGENT_PORT = int(os.environ.get("WORKSPACE_AGENT_PORT", "19999"))
AGENT_TOKEN = os.environ.get("WORKSPACE_AGENT_TOKEN", "")
MAX_OUTPUT_BYTES = int(os.environ.get("WORKSPACE_AGENT_MAX_OUTPUT", "200000"))
MAX_FILE_MB = int(os.environ.get("WORKSPACE_AGENT_MAX_FILE_MB", "8"))
LOG_MAX_BYTES = int(os.environ.get("WORKSPACE_AGENT_LOG_MAX_BYTES", "5000000"))
SERVICE_LOG_DIR = os.path.join(WORKSPACE_ROOT, ".sandbox", "services")
OP_RETENTION_SECONDS = 3600
MAX_OPERATIONS = 100

_LOCK = threading.RLock()
_OPERATIONS: dict[str, "Operation"] = {}
_SERVICES: dict[str, "Service"] = {}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── 路径守卫 ─────────────────────────────────────────────────────────────

class PathError(Exception):
    pass


def resolve_path(raw: str) -> str:
    path = (raw or "/workspace").strip()
    if path in ("", "/"):
        path = WORKSPACE_ROOT
    elif path == "/workspace" or path.startswith("/workspace/"):
        path = WORKSPACE_ROOT + path[len("/workspace"):]
    elif not path.startswith("/"):
        path = os.path.join(WORKSPACE_ROOT, path)
    real = os.path.realpath(path)
    root = WORKSPACE_ROOT
    if real != root and not real.startswith(root + os.sep):
        raise PathError(f"路径越界（仅允许 /workspace 之下）: {raw}")
    return real


def virtual_path(real: str) -> str:
    rel = os.path.relpath(real, WORKSPACE_ROOT)
    if rel == ".":
        return "/workspace"
    return "/workspace/" + rel.replace(os.sep, "/")


def _file_info(real: str) -> dict:
    try:
        stat = os.stat(real)
        return {
            "path": virtual_path(real),
            "name": os.path.basename(real),
            "is_dir": os.path.isdir(real),
            "size": stat.st_size,
            "modified_at": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
        }
    except OSError:
        return {"path": virtual_path(real), "name": os.path.basename(real), "is_dir": False}


# ── 命令 Operation ───────────────────────────────────────────────────────

class Operation:
    def __init__(self, command: str, cwd: str, timeout_seconds: int) -> None:
        self.id = os.urandom(12).hex()
        self.command = command
        self.cwd = cwd
        self.timeout_seconds = max(1, int(timeout_seconds))
        self.status = "running"
        self.exit_code: Optional[int] = None
        self.stdout = bytearray()
        self.stderr = bytearray()
        self.truncated = False
        self.started_at = _now_iso()
        self.finished_at: Optional[str] = None
        self.process: Optional[subprocess.Popen] = None
        self._lock = threading.RLock()
        self._cancelled = False

    def _append(self, target: bytearray, chunk: bytes) -> None:
        with self._lock:
            remain = MAX_OUTPUT_BYTES - len(target)
            if remain <= 0:
                self.truncated = True
                return
            if len(chunk) > remain:
                target.extend(chunk[:remain])
                self.truncated = True
            else:
                target.extend(chunk)

    def _pump(self, stream, target: bytearray) -> None:
        try:
            while True:
                chunk = stream.read(8192)
                if not chunk:
                    break
                self._append(target, chunk)
        except Exception:  # noqa: BLE001
            pass
        finally:
            try:
                stream.close()
            except Exception:  # noqa: BLE001
                pass

    def _watchdog(self) -> None:
        process = self.process
        if process is None:
            return
        try:
            process.wait(timeout=self.timeout_seconds)
        except subprocess.TimeoutExpired:
            self._terminate_group(force=True)
            with self._lock:
                if self.status == "running":
                    self.status = "timeout"
                    self.finished_at = _now_iso()
                    try:
                        self.exit_code = process.wait(timeout=5)
                    except Exception:  # noqa: BLE001
                        self.exit_code = -9
            return
        with self._lock:
            if self.status == "running":
                self.status = "succeeded" if process.returncode == 0 else "failed"
                self.exit_code = process.returncode
                self.finished_at = _now_iso()

    def _terminate_group(self, *, force: bool = False) -> None:
        process = self.process
        if process is None or process.poll() is not None:
            return
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGKILL if force else signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            return
        if not force:
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(os.getpgid(process.pid), signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass

    def start(self) -> None:
        env = {key: value for key, value in os.environ.items()
               if key not in ("WORKSPACE_AGENT_TOKEN",)}
        env.setdefault("TERM", "dumb")
        env.setdefault("PWD", self.cwd)
        self.process = subprocess.Popen(  # noqa: S603 命令由 Agent 显式执行
            ["/bin/bash", "-lc", self.command],
            cwd=self.cwd,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
        threading.Thread(target=self._pump, args=(self.process.stdout, self.stdout),
                         daemon=True).start()
        threading.Thread(target=self._pump, args=(self.process.stderr, self.stderr),
                         daemon=True).start()
        threading.Thread(target=self._watchdog, daemon=True).start()

    def cancel(self) -> None:
        with self._lock:
            self._cancelled = True
        self._terminate_group(force=False)
        with self._lock:
            if self.status == "running":
                self.status = "cancelled"
                self.finished_at = _now_iso()
                if self.exit_code is None and self.process is not None:
                    try:
                        self.exit_code = self.process.wait(timeout=3)
                    except Exception:  # noqa: BLE001
                        self.exit_code = -15

    def to_dict(self, *, include_output: bool = True) -> dict:
        with self._lock:
            data = {
                "operation_id": self.id,
                "status": self.status,
                "exit_code": self.exit_code,
                "truncated": self.truncated,
                "started_at": self.started_at,
                "finished_at": self.finished_at,
                "timeout_seconds": self.timeout_seconds,
            }
            if include_output:
                data["stdout"] = self.stdout.decode("utf-8", "replace")
                data["stderr"] = self.stderr.decode("utf-8", "replace")
            return data

    def wait(self, seconds: float) -> None:
        deadline = time.monotonic() + max(0.0, seconds)
        while time.monotonic() < deadline:
            with self._lock:
                if self.status != "running":
                    return
            time.sleep(0.1)


def _gc_operations() -> None:
    now = time.time()
    with _LOCK:
        finished = [
            (op_id, op) for op_id, op in _OPERATIONS.items()
            if op.status != "running"
        ]
        for op_id, op in finished:
            try:
                if op.finished_at and (
                    datetime.fromisoformat(op.finished_at).timestamp()
                    < now - OP_RETENTION_SECONDS
                ):
                    _OPERATIONS.pop(op_id, None)
            except Exception:  # noqa: BLE001
                _OPERATIONS.pop(op_id, None)
        if len(_OPERATIONS) > MAX_OPERATIONS:
            removable = [
                (op.started_at, op_id) for op_id, op in _OPERATIONS.items()
                if op.status != "running"
            ]
            removable.sort()
            for _started, op_id in removable[: max(0, len(_OPERATIONS) - MAX_OPERATIONS)]:
                _OPERATIONS.pop(op_id, None)


def exec_command(payload: dict) -> dict:
    command = str(payload.get("command") or "").strip()
    if not command:
        raise ValueError("command 不能为空")
    cwd = resolve_path(str(payload.get("working_directory") or "/workspace"))
    if not os.path.isdir(cwd):
        raise ValueError(f"工作目录不存在: {payload.get('working_directory')}")
    timeout_seconds = int(payload.get("timeout_seconds") or 600)
    wait_seconds = min(60.0, max(0.0, float(payload.get("wait_seconds") or 0)))
    op = Operation(command, cwd, timeout_seconds)
    with _LOCK:
        _OPERATIONS[op.id] = op
    op.start()
    op.wait(wait_seconds)
    _gc_operations()
    return op.to_dict()


def get_operation(operation_id: str) -> dict:
    with _LOCK:
        op = _OPERATIONS.get(operation_id)
    if op is None:
        raise KeyError("operation 不存在或已过期")
    return op.to_dict()


def cancel_operation(operation_id: str) -> dict:
    with _LOCK:
        op = _OPERATIONS.get(operation_id)
    if op is None:
        raise KeyError("operation 不存在或已过期")
    op.cancel()
    return op.to_dict()


# ── 文件 ─────────────────────────────────────────────────────────────────

def list_dir(path: str) -> dict:
    real = resolve_path(path)
    if not os.path.isdir(real):
        raise FileNotFoundError(f"目录不存在: {path}")
    entries = []
    with os.scandir(real) as iterator:
        for entry in iterator:
            entries.append(_file_info(entry.path))
    entries.sort(key=lambda item: (not item.get("is_dir"), item.get("name", "").lower()))
    return {"path": virtual_path(real), "entries": entries, "truncated": False}


def read_file(payload: dict) -> dict:
    real = resolve_path(str(payload.get("path") or ""))
    offset = max(0, int(payload.get("offset") or 0))
    limit = max(1, min(5000, int(payload.get("limit") or 2000)))
    if not os.path.isfile(real):
        raise FileNotFoundError(f"文件不存在: {payload.get('path')}")
    size = os.path.getsize(real)
    if size > MAX_FILE_MB * 1024 * 1024 * 4:
        raise ValueError(f"文件过大（{size} 字节），请使用命令行分段读取或下载")
    with open(real, "rb") as handle:
        raw = handle.read()
    if b"\x00" in raw[:8192]:
        content = base64.b64encode(raw[: MAX_FILE_MB * 1024 * 1024]).decode("ascii")
        return {
            "path": virtual_path(real), "encoding": "base64", "content": content,
            "total_lines": None, "start_line": None, "end_line": None,
            "next_offset": None, "truncated": len(raw) > MAX_FILE_MB * 1024 * 1024,
        }
    text = raw.decode("utf-8", "replace")
    lines = text.split("\n")
    total = len(lines)
    window = lines[offset: offset + limit]
    end_line = offset + len(window)
    return {
        "path": virtual_path(real),
        "encoding": "utf-8",
        "content": "\n".join(window),
        "total_lines": total,
        "start_line": offset + 1 if window else None,
        "end_line": end_line if window else None,
        "next_offset": end_line if end_line < total else None,
        "truncated": False,
    }


def write_file(payload: dict) -> dict:
    real = resolve_path(str(payload.get("path") or ""))
    content = str(payload.get("content") or "")
    encoding = str(payload.get("encoding") or "utf-8")
    if encoding == "base64":
        try:
            raw = base64.b64decode(content)
        except Exception as exc:  # noqa: BLE001
            raise ValueError(f"base64 内容非法: {exc}") from exc
    else:
        raw = content.encode("utf-8")
    if len(raw) > MAX_FILE_MB * 1024 * 1024:
        raise ValueError(f"文件超过 {MAX_FILE_MB}MB 上限")
    os.makedirs(os.path.dirname(real) or WORKSPACE_ROOT, exist_ok=True)
    with open(real, "wb") as handle:
        handle.write(raw)
    return {"path": virtual_path(real), "size": len(raw)}


def edit_file(payload: dict) -> dict:
    real = resolve_path(str(payload.get("path") or ""))
    old_string = str(payload.get("old_string") or "")
    new_string = str(payload.get("new_string") or "")
    replace_all = bool(payload.get("replace_all"))
    if not old_string:
        raise ValueError("old_string 不能为空")
    if not os.path.isfile(real):
        raise FileNotFoundError(f"文件不存在: {payload.get('path')}")
    with open(real, "r", encoding="utf-8", errors="replace") as handle:
        text = handle.read()
    occurrences = text.count(old_string)
    if occurrences == 0:
        raise ValueError("old_string 未在文件中找到（需精确匹配）")
    text = text.replace(old_string, new_string) if replace_all else text.replace(
        old_string, new_string, 1)
    with open(real, "w", encoding="utf-8") as handle:
        handle.write(text)
    return {
        "path": virtual_path(real),
        "occurrences": occurrences if replace_all else 1,
    }


def delete_path(payload: dict) -> dict:
    real = resolve_path(str(payload.get("path") or ""))
    if real == WORKSPACE_ROOT:
        raise ValueError("不能删除工作区根目录")
    if os.path.isdir(real) and not os.path.islink(real):
        shutil.rmtree(real)
    elif os.path.exists(real) or os.path.islink(real):
        os.remove(real)
    else:
        raise FileNotFoundError(f"路径不存在: {payload.get('path')}")
    return {"path": virtual_path(real), "deleted": True}


_SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", ".sandbox", ".next", "dist"}


def _walk_files(root: str, max_files: int = 5000):
    count = 0
    for current, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
        for name in files:
            count += 1
            if count > max_files:
                return
            yield os.path.join(current, name)


def grep_files(payload: dict) -> dict:
    pattern = str(payload.get("pattern") or "")
    if not pattern:
        raise ValueError("pattern 不能为空")
    base = resolve_path(str(payload.get("path") or "/workspace"))
    include = str(payload.get("glob") or "").strip()
    max_count = max(1, min(1000, int(payload.get("max_count") or 200)))
    matches = []
    truncated = False
    for path in _walk_files(base):
        rel = os.path.relpath(path, WORKSPACE_ROOT).replace(os.sep, "/")
        if include and not (
            fnmatch.fnmatch(rel, include) or fnmatch.fnmatch(os.path.basename(path), include)
        ):
            continue
        try:
            if os.path.getsize(path) > 2 * 1024 * 1024:
                continue
            with open(path, "r", encoding="utf-8", errors="ignore") as handle:
                for number, line in enumerate(handle, start=1):
                    if pattern in line:
                        matches.append({
                            "path": virtual_path(path),
                            "line": number,
                            "text": line.rstrip("\n")[:2000],
                        })
                        if len(matches) >= max_count:
                            truncated = True
                            break
        except (OSError, UnicodeError):
            continue
        if truncated:
            break
    return {"matches": matches, "truncated": truncated}


def glob_files(payload: dict) -> dict:
    pattern = str(payload.get("pattern") or "").strip()
    if not pattern:
        raise ValueError("pattern 不能为空")
    base = resolve_path(str(payload.get("path") or "/workspace"))
    matches = []
    truncated = False
    for path in _walk_files(base):
        rel = os.path.relpath(path, WORKSPACE_ROOT).replace(os.sep, "/")
        if fnmatch.fnmatch(rel, pattern) or fnmatch.fnmatch(os.path.basename(path), pattern):
            matches.append(_file_info(path))
            if len(matches) >= 500:
                truncated = True
                break
    return {"matches": matches, "truncated": truncated,
            "truncation_reason": "budget" if truncated else None}


def raw_file(payload: dict) -> dict:
    real = resolve_path(str(payload.get("path") or ""))
    if not os.path.isfile(real):
        raise FileNotFoundError(f"文件不存在: {payload.get('path')}")
    size = os.path.getsize(real)
    if size > MAX_FILE_MB * 1024 * 1024:
        raise ValueError(f"文件超过 {MAX_FILE_MB}MB，请使用命令行打包")
    with open(real, "rb") as handle:
        raw = handle.read()
    content_type = mimetypes.guess_type(real)[0] or "application/octet-stream"
    return {
        "path": virtual_path(real),
        "size": size,
        "content_type": content_type,
        "content_b64": base64.b64encode(raw).decode("ascii"),
    }


def workspace_usage() -> dict:
    """Workspace Volume 实际占用（du -sb），用于容量监控（软配额）。"""
    try:
        result = subprocess.run(  # noqa: S603 固定命令
            ["du", "-sb", WORKSPACE_ROOT],
            capture_output=True, text=True, timeout=15, check=False,
        )
        if result.returncode == 0 and result.stdout.strip():
            return {"bytes": int(result.stdout.split()[0])}
    except (OSError, ValueError, subprocess.TimeoutExpired):
        pass
    return {"bytes": 0}


# ── Service ──────────────────────────────────────────────────────────────

class Service:
    def __init__(self, payload: dict) -> None:
        self.name = str(payload.get("name") or "").strip()
        self.command = str(payload.get("command") or "").strip()
        self.cwd = resolve_path(str(payload.get("working_directory") or "/workspace"))
        self.port = int(payload.get("port") or 0)
        self.protocol = str(payload.get("protocol") or "http")
        self.health_type = str(payload.get("health_check_type") or "http")
        self.health_path = str(payload.get("health_check_path") or "/")
        self.health_timeout = max(5, min(600, int(payload.get("health_check_timeout") or 60)))
        self.auto_restart = bool(payload.get("auto_restart", True))
        self.process: Optional[subprocess.Popen] = None
        self.status = "created"
        self.exit_code: Optional[int] = None
        self.last_started_at: Optional[str] = None
        self.last_exited_at: Optional[str] = None
        self.restart_count = 0
        self.stopped_by_user = False
        self.log_path = os.path.join(SERVICE_LOG_DIR, f"{self.name}.log")
        self._lock = threading.RLock()

    def _rotate_log(self) -> None:
        try:
            if os.path.exists(self.log_path) and os.path.getsize(self.log_path) > LOG_MAX_BYTES:
                os.replace(self.log_path, self.log_path + ".1")
        except OSError:
            pass

    def start(self) -> None:
        with self._lock:
            if self.process is not None and self.process.poll() is None:
                raise ValueError(f"服务 '{self.name}' 已在运行")
            if not self.command:
                raise ValueError("command 不能为空")
            if not os.path.isdir(self.cwd):
                raise ValueError(f"工作目录不存在: {self.cwd}")
            os.makedirs(SERVICE_LOG_DIR, exist_ok=True)
            self._rotate_log()
            env = {key: value for key, value in os.environ.items()
                   if key not in ("WORKSPACE_AGENT_TOKEN",)}
            env.update({
                "PORT": str(self.port),
                "HOST": "0.0.0.0",
                "TERM": "xterm-256color",
                "PWD": self.cwd,
            })
            log_handle = open(self.log_path, "ab", buffering=0)
            log_handle.write(
                f"\n[{_now_iso()}] starting: {self.command}\n".encode("utf-8", "replace")
            )
            self.process = subprocess.Popen(  # noqa: S603 用户显式配置的服务命令
                ["/bin/bash", "-lc", self.command],
                cwd=self.cwd, env=env,
                stdout=log_handle, stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
            )
            self.status = "starting"
            self.stopped_by_user = False
            self.last_started_at = _now_iso()
            self.exit_code = None
            threading.Thread(target=self._monitor, daemon=True).start()
            threading.Thread(target=self._health_check, daemon=True).start()

    def _monitor(self) -> None:
        process = self.process
        if process is None:
            return
        code = process.wait()
        with self._lock:
            self.exit_code = code
            self.last_exited_at = _now_iso()
            if self.status not in ("stopping",):
                self.status = "exited" if code == 0 else "failed"
            should_restart = (
                self.auto_restart and not self.stopped_by_user
                and self.restart_count < 5
            )
            if should_restart:
                self.restart_count += 1
        if should_restart:
            time.sleep(min(10, 2 * self.restart_count))
            try:
                with self._lock:
                    if not self.stopped_by_user:
                        self.start()
            except Exception:  # noqa: BLE001
                pass

    def _health_check(self) -> None:
        if self.health_type == "none":
            with self._lock:
                if self.status == "starting":
                    self.status = "healthy"
            return
        deadline = time.time() + self.health_timeout
        while time.time() < deadline:
            process = self.process
            if process is None or process.poll() is not None:
                with self._lock:
                    if self.status in ("starting", "healthy", "unhealthy"):
                        self.status = "exited" if (process and process.returncode == 0) else "failed"
                return
            try:
                if self.health_type == "tcp":
                    with socket.create_connection(("127.0.0.1", self.port), timeout=2):
                        with self._lock:
                            if self.status in ("starting", "unhealthy"):
                                self.status = "healthy"
                        return
                else:
                    url = f"http://127.0.0.1:{self.port}{self.health_path}"
                    with urllib.request.urlopen(url, timeout=3) as response:  # noqa: S310 内网自检
                        if response.status < 500:
                            with self._lock:
                                if self.status in ("starting", "unhealthy"):
                                    self.status = "healthy"
                            return
            except (urllib.error.URLError, OSError, ValueError):
                pass
            time.sleep(1.5)
        with self._lock:
            if self.status == "starting":
                self.status = "unhealthy"

    def stop(self) -> None:
        with self._lock:
            self.stopped_by_user = True
            process = self.process
            self.status = "stopping"
        if process is not None and process.poll() is None:
            try:
                os.killpg(os.getpgid(process.pid), signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(os.getpgid(process.pid), signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
                try:
                    process.wait(timeout=5)
                except Exception:  # noqa: BLE001
                    pass
        with self._lock:
            self.status = "stopped"
            self.last_exited_at = _now_iso()
            if process is not None:
                self.exit_code = process.returncode

    def restart(self) -> None:
        self.stop()
        with self._lock:
            self.restart_count = 0
            self.start()

    def logs(self, tail: int = 200) -> dict:
        if not os.path.exists(self.log_path):
            return {"name": self.name, "content": "", "truncated": False}
        try:
            with open(self.log_path, "r", encoding="utf-8", errors="replace") as handle:
                lines = handle.readlines()
        except OSError as exc:
            return {"name": self.name, "content": f"读取日志失败: {exc}", "truncated": False}
        selected = lines[-max(1, tail):]
        return {
            "name": self.name,
            "content": "".join(selected),
            "truncated": len(lines) > len(selected),
        }

    def to_dict(self) -> dict:
        with self._lock:
            process = self.process
            return {
                "name": self.name,
                "port": self.port,
                "protocol": self.protocol,
                "observed_state": self.status,
                "process_id": str(process.pid) if process and process.poll() is None else None,
                "exit_code": self.exit_code,
                "last_started_at": self.last_started_at,
                "last_exited_at": self.last_exited_at,
                "auto_restart": self.auto_restart,
                "restart_count": self.restart_count,
                "log_reference": self.log_path,
            }


def start_service(payload: dict) -> dict:
    name = str(payload.get("name") or "").strip()
    if not name or not re.fullmatch(r"[A-Za-z0-9._-]{1,80}", name):
        raise ValueError("服务名只能包含字母/数字/._-，长度 1-80")
    with _LOCK:
        existing = _SERVICES.get(name)
        if existing is not None and existing.process is not None and existing.process.poll() is None:
            raise ValueError(f"服务 '{name}' 已在运行")
        service = Service(payload)
        _SERVICES[name] = service
    service.start()
    return service.to_dict()


def list_services() -> dict:
    with _LOCK:
        services = list(_SERVICES.values())
    return {"services": [service.to_dict() for service in services]}


def _get_service(name: str) -> Service:
    with _LOCK:
        service = _SERVICES.get(name)
    if service is None:
        raise KeyError(f"服务 '{name}' 不存在")
    return service


def stop_service(name: str) -> dict:
    service = _get_service(name)
    service.stop()
    return service.to_dict()


def restart_service(name: str) -> dict:
    service = _get_service(name)
    service.restart()
    return service.to_dict()


def service_logs(name: str, tail: int) -> dict:
    return _get_service(name).logs(tail)


def stop_all() -> None:
    for operation in list(_OPERATIONS.values()):
        try:
            operation.cancel()
        except Exception:  # noqa: BLE001
            pass
    for service in list(_SERVICES.values()):
        try:
            service.stop()
        except Exception:  # noqa: BLE001
            pass


# ── HTTP ─────────────────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):
    server_version = "workspace-agent/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def _send(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self) -> bool:
        if not AGENT_TOKEN:
            return False
        header = self.headers.get("Authorization") or ""
        return header == f"Bearer {AGENT_TOKEN}"

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        if length > 20 * 1024 * 1024:
            raise ValueError("请求体过大")
        raw = self.rfile.read(length)
        return json.loads(raw.decode("utf-8"))

    def _handle(self, method: str) -> None:
        parsed = urlsplit(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)
        if path == "/health":
            self._send(200, {
                "status": "ok",
                "pid": os.getpid(),
                "workspace": WORKSPACE_ROOT,
                "workspace_writable": os.access(WORKSPACE_ROOT, os.W_OK),
                "operations": len(_OPERATIONS),
                "services": len(_SERVICES),
            })
            return
        if not self._authorized():
            self._send(401, {"detail": "unauthorized"})
            return
        try:
            payload = self._read_json() if method in ("POST", "PUT", "PATCH") else {}
            if path == "/exec" and method == "POST":
                self._send(200, exec_command(payload))
            elif path.startswith("/exec/") and path.endswith("/cancel") and method == "POST":
                self._send(200, cancel_operation(path.split("/")[2]))
            elif path.startswith("/exec/") and method == "GET":
                self._send(200, get_operation(path.split("/")[2]))
            elif path == "/files/list" and method == "GET":
                self._send(200, list_dir((query.get("path") or ["/workspace"])[0]))
            elif path == "/files/read" and method == "POST":
                self._send(200, read_file(payload))
            elif path == "/files/write" and method == "POST":
                self._send(200, write_file(payload))
            elif path == "/files/edit" and method == "POST":
                self._send(200, edit_file(payload))
            elif path == "/files/delete" and method == "POST":
                self._send(200, delete_path(payload))
            elif path == "/files/grep" and method == "POST":
                self._send(200, grep_files(payload))
            elif path == "/files/glob" and method == "POST":
                self._send(200, glob_files(payload))
            elif path == "/files/raw" and method == "POST":
                self._send(200, raw_file(payload))
            elif path == "/files/usage" and method == "GET":
                self._send(200, workspace_usage())
            elif path == "/services" and method == "GET":
                self._send(200, list_services())
            elif path == "/services" and method == "POST":
                self._send(200, start_service(payload))
            elif path.startswith("/services/"):
                parts = path.strip("/").split("/")
                name = parts[1]
                if len(parts) == 2 and method == "GET":
                    self._send(200, _get_service(name).to_dict())
                elif len(parts) == 3 and parts[2] == "stop" and method == "POST":
                    self._send(200, stop_service(name))
                elif len(parts) == 3 and parts[2] == "restart" and method == "POST":
                    self._send(200, restart_service(name))
                elif len(parts) == 3 and parts[2] == "logs" and method == "GET":
                    tail = int((query.get("tail") or ["200"])[0])
                    self._send(200, service_logs(name, tail))
                else:
                    self._send(404, {"detail": "not found"})
            else:
                self._send(404, {"detail": "not found"})
        except PathError as exc:
            self._send(400, {"detail": str(exc)})
        except FileNotFoundError as exc:
            self._send(404, {"detail": str(exc)})
        except KeyError as exc:
            self._send(404, {"detail": str(exc).strip("'\"")})
        except ValueError as exc:
            self._send(400, {"detail": str(exc)})
        except json.JSONDecodeError:
            self._send(400, {"detail": "invalid json"})
        except Exception as exc:  # noqa: BLE001
            self.log_message("internal error: %s", exc)
            self._send(500, {"detail": f"internal error: {exc}"})

    def do_GET(self) -> None:  # noqa: N802
        self._handle("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._handle("POST")

    def do_PUT(self) -> None:  # noqa: N802
        self._handle("PUT")

    def do_DELETE(self) -> None:  # noqa: N802
        self._handle("DELETE")


def main() -> None:
    if not AGENT_TOKEN:
        print("WORKSPACE_AGENT_TOKEN 未配置，拒绝启动", file=sys.stderr)
        sys.exit(1)
    os.makedirs(WORKSPACE_ROOT, exist_ok=True)
    os.makedirs(SERVICE_LOG_DIR, exist_ok=True)

    def _shutdown(signum, _frame):  # noqa: ANN001
        print(f"workspace-agent 收到信号 {signum}，清理进程组", file=sys.stderr)
        stop_all()
        sys.exit(0)

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    server = ThreadingHTTPServer(("0.0.0.0", AGENT_PORT), Handler)
    server.daemon_threads = True
    print(f"workspace-agent listening on 0.0.0.0:{AGENT_PORT} root={WORKSPACE_ROOT}",
          file=sys.stderr)
    server.serve_forever()


if __name__ == "__main__":
    main()
