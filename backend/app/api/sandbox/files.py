"""Project Workspace 文件浏览 API（项目会话与 Agent 共享同一 /workspace）。"""
from __future__ import annotations

import base64
import mimetypes
from urllib.parse import quote

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.core.database import get_db
from app.models.user import User
from app.services.sandbox import acl, agent_client, runtime
from app.services.sandbox.common import SandboxNotReadyError, ensure_enabled

router = APIRouter(prefix="/projects", tags=["workspace-files-v2"])

_MAX_RAW_BYTES = 8 * 1024 * 1024


async def _ready_sandbox(db: AsyncSession, project_id: int, user: User, min_role: str = "viewer"):
    workspace, _project, _role = await acl.require_workspace_by_project(
        db, project_id, user, min_role,
    )
    sandbox = await runtime.require_ready_sandbox(db, workspace)
    return workspace, sandbox


@router.get("/{project_id}/workspace/files")
async def list_workspace_files(
    project_id: int,
    path: str = Query("/workspace", max_length=1024),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    ensure_enabled()
    workspace, sandbox = await _ready_sandbox(db, project_id, current_user)
    data = await agent_client.list_dir(sandbox.id, path)
    await runtime.touch_workspace(db, workspace.id)
    await db.commit()
    return {
        "path": data.get("path") or path,
        "entries": data.get("entries") or [],
        "truncated": bool(data.get("truncated")),
    }


@router.get("/{project_id}/workspace/files/content")
async def read_workspace_file(
    project_id: int,
    path: str = Query(..., max_length=1024),
    offset: int = Query(0, ge=0),
    limit: int = Query(2000, ge=1, le=5000),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    ensure_enabled()
    workspace, sandbox = await _ready_sandbox(db, project_id, current_user)
    data = await agent_client.read_file(sandbox.id, path, offset=offset, limit=limit)
    await runtime.touch_workspace(db, workspace.id)
    await db.commit()
    return data


@router.get("/{project_id}/workspace/files/raw")
async def download_workspace_file(
    project_id: int,
    path: str = Query(..., max_length=1024),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    ensure_enabled()
    workspace, sandbox = await _ready_sandbox(db, project_id, current_user)
    data = await agent_client.read_raw(sandbox.id, path)
    size = int(data.get("size") or 0)
    if size > _MAX_RAW_BYTES:
        raise SandboxNotReadyError(
            f"文件过大（{size} 字节），请使用命令行工具打包下载（上限 {_MAX_RAW_BYTES // 1024 // 1024}MB）"
        )
    raw = base64.b64decode(data.get("content_b64") or "")
    await runtime.touch_workspace(db, workspace.id)
    await db.commit()
    filename = path.rsplit("/", 1)[-1] or "download.bin"
    media_type = data.get("content_type") or mimetypes.guess_type(filename)[0] or "application/octet-stream"
    return Response(
        content=raw,
        media_type=media_type,
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}",
            "X-Content-Type-Options": "nosniff",
        },
    )
