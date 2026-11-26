"""项目会话文件桥接：附件上传 / 文件面板 / 预览读取统一走 Sandbox。

未归属项目的存量会话继续使用旧宿主机目录（backends.workspace_root）。
"""
from __future__ import annotations

import base64
import io
import mimetypes
import os
import tempfile
import zipfile
from contextlib import asynccontextmanager
from datetime import datetime
from typing import AsyncIterator, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.agent import AgentSession
from app.models.sandbox import Project
from app.services.sandbox import agent_client, runtime

_EXCLUDED_DIRS = {"tool-outputs", "tool_results", ".sandbox", ".run-artifacts"}
_EXCLUDED_SUFFIX = ".extracted.txt"
_MAX_FILES = 500
_MAX_DEPTH = 8
_MAX_ZIP_BYTES = 100 * 1024 * 1024


def uses_sandbox(session: AgentSession) -> bool:
    return bool(settings.SANDBOX_ENABLED and getattr(session, "project_id", None))


async def workspace_for_session(db: AsyncSession, session: AgentSession):
    project = (await db.execute(
        select(Project).where(
            Project.id == session.project_id, Project.deleted_at.is_(None))
    )).scalar_one_or_none()
    if project is None:
        from app.services.sandbox.common import SandboxNotFoundError

        raise SandboxNotFoundError("会话所属项目不存在")
    workspace = await runtime.ensure_workspace(
        db, project.id, project.owner_id, project.tenant_id)
    sandbox = await runtime.require_ready_sandbox(db, workspace)
    return workspace, sandbox


async def _sandbox(db: AsyncSession, session: AgentSession):
    _workspace, sandbox = await workspace_for_session(db, session)
    return sandbox


async def list_files(db: AsyncSession, session: AgentSession) -> tuple[list[dict], bool]:
    sandbox = await _sandbox(db, session)
    entries: list[dict] = []
    truncated = False
    queue: list[tuple[str, int]] = [("/workspace", 0)]
    while queue:
        directory, depth = queue.pop(0)
        data = await agent_client.list_dir(sandbox.id, directory)
        for entry in (data.get("entries") or []):
            name = str(entry.get("name") or "")
            virtual = str(entry.get("path") or "")
            if not name or name.startswith("."):
                continue
            if entry.get("is_dir"):
                if depth < _MAX_DEPTH and name.casefold() not in _EXCLUDED_DIRS:
                    queue.append((virtual, depth + 1))
                continue
            if name.endswith(_EXCLUDED_SUFFIX):
                continue
            rel = virtual[len("/workspace/"):] if virtual.startswith("/workspace/") else virtual
            modified = entry.get("modified_at")
            try:
                mtime = datetime.fromisoformat(modified).timestamp() if modified else 0.0
            except ValueError:
                mtime = 0.0
            entries.append({
                "path": rel,
                "name": name,
                "size": int(entry.get("size") or 0),
                "mtime": mtime,
                "modified_at": modified,
            })
            if len(entries) > _MAX_FILES:
                truncated = True
                break
        if truncated:
            break
    entries.sort(key=lambda item: item["path"])
    if len(entries) > _MAX_FILES:
        truncated = True
        entries = entries[:_MAX_FILES]
    return entries, truncated


async def read_bytes(db: AsyncSession, session: AgentSession, rel_path: str) -> tuple[bytes, str]:
    sandbox = await _sandbox(db, session)
    data = await agent_client.read_raw(sandbox.id, _virtual(rel_path))
    raw = base64.b64decode(data.get("content_b64") or "")
    mime = (data.get("content_type")
            or mimetypes.guess_type(rel_path)[0]
            or "application/octet-stream")
    return raw, mime


async def write_bytes(db: AsyncSession, session: AgentSession, rel_path: str, data: bytes) -> dict:
    sandbox = await _sandbox(db, session)
    try:
        text = data.decode("utf-8")
        return await agent_client.write_file(sandbox.id, _virtual(rel_path), text)
    except UnicodeDecodeError:
        return await agent_client.write_file(
            sandbox.id, _virtual(rel_path),
            base64.b64encode(data).decode("ascii"), encoding="base64",
        )


async def write_text(db: AsyncSession, session: AgentSession, rel_path: str, text: str) -> dict:
    sandbox = await _sandbox(db, session)
    return await agent_client.write_file(sandbox.id, _virtual(rel_path), text)


async def delete_path(db: AsyncSession, session: AgentSession, rel_path: str) -> None:
    sandbox = await _sandbox(db, session)
    try:
        await agent_client.delete_path(sandbox.id, _virtual(rel_path))
    except Exception as exc:  # noqa: BLE001 删除不存在视为成功（幂等）
        message = str(exc)
        if "不存在" in message or "404" in message:
            return
        raise


async def unique_upload_name(
    db: AsyncSession, session: AgentSession, name: str, *, reserve_sidecar: bool,
) -> str:
    sandbox = await _sandbox(db, session)
    try:
        listing = await agent_client.list_dir(sandbox.id, "/workspace/uploads")
        existing = {entry.get("name") for entry in (listing.get("entries") or [])}
    except Exception:  # noqa: BLE001 目录不存在视为空
        existing = set()
    stem, ext = os.path.splitext(name)
    candidate = name
    seq = 0
    while candidate in existing or (reserve_sidecar and f"{candidate}.extracted.txt" in existing):
        seq += 1
        candidate = f"{stem}-{seq}{ext}"
    return candidate


@asynccontextmanager
async def materialize_temp(
    db: AsyncSession, session: AgentSession, rel_path: str,
) -> AsyncIterator[str]:
    """把 Sandbox 文件落到本机临时文件（供 Office/PDF 预览解析），用完即删。"""
    raw, _mime = await read_bytes(db, session, rel_path)
    suffix = os.path.splitext(rel_path)[1]
    handle = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
    try:
        handle.write(raw)
        handle.close()
        yield handle.name
    finally:
        try:
            os.unlink(handle.name)
        except OSError:
            pass


async def build_zip(db: AsyncSession, session: AgentSession) -> bytes:
    entries, _truncated = await list_files(db, session)
    buffer = io.BytesIO()
    total = 0
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for entry in entries:
            try:
                raw, _mime = await read_bytes(db, session, entry["path"])
            except Exception:  # noqa: BLE001 单个文件失败不阻断整体下载
                continue
            total += len(raw)
            if total > _MAX_ZIP_BYTES:
                break
            archive.writestr(entry["path"], raw)
    return buffer.getvalue()


def _virtual(rel_path: str) -> str:
    raw = (rel_path or "").replace("\\", "/").lstrip("/")
    if raw.startswith("workspace/"):
        raw = raw[len("workspace/"):]
    return "/workspace/" + raw
