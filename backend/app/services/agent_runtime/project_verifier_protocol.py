"""离线工程验收的文件协议，由平台进程和验收容器共享。"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from pathlib import Path

MAX_PROJECT_FILES = 96
MAX_PROJECT_FILE_BYTES = 240_000
MAX_PROJECT_BYTES = 3_000_000
MAX_JOB_BYTES = 2 * MAX_PROJECT_BYTES + 32_768
MAX_RESULT_BYTES = 65_536
MAX_OPENAPI_BYTES = 48_000
JOB_TIMEOUT_SECONDS = 90.0
HEARTBEAT_SECONDS = 10.0
JOB_NAME = re.compile(r"job-[a-f0-9]{32}\Z")
_PATH_PART = re.compile(r"[A-Za-z0-9_@().+\[\]-]{1,90}\Z")


def source_digest(files: dict[str, str]) -> str:
    data = json.dumps(sorted(files.items()), ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(data.encode()).hexdigest()


def openapi_source(document: object) -> str | None:
    """接收来自可运行服务的精简 OpenAPI 文本。"""
    if not isinstance(document, dict):
        return None
    version, info, paths = (document.get(key) for key in ("openapi", "info", "paths"))
    if (not isinstance(version, str) or not re.fullmatch(r"3\.\d+\.\d+", version)
            or not isinstance(info, dict)
            or any(not isinstance(info.get(key), str) or not info[key].strip() for key in ("title", "version"))
            or not isinstance(paths, dict) or not any(isinstance(path, str) and path.startswith("/") and isinstance(operations, dict) and any(
                isinstance(operations.get(method), dict)
                for method in ("get", "post", "put", "patch", "delete", "head", "options", "trace")
            ) for path, operations in paths.items())):
        return None
    try:
        source = json.dumps(document, ensure_ascii=False, allow_nan=False, separators=(",", ":")) + "\n"
        return source if len(source.encode()) <= MAX_OPENAPI_BYTES else None
    except (ValueError, RecursionError):
        return None


def valid_job_path(path: str) -> bool:
    parts = path.split("/")
    return (0 < len(path) <= 240 and len(parts) <= 12
            and all(_PATH_PART.fullmatch(part) and part not in {".", ".."} for part in parts))


def read_json(path: Path, limit: int) -> dict:
    with path.open("rb") as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise ValueError("工程验收报文超过限额")
    payload = json.loads(data)
    if not isinstance(payload, dict):
        raise ValueError("工程验收报文须为对象")
    return payload


def write_json(path: Path, payload: dict) -> None:
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
        staged = Path(stream.name)
    try:
        staged.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        os.replace(staged, path)
    finally:
        staged.unlink(missing_ok=True)
