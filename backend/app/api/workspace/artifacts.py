"""Artifacts API v2（§7 /api/v2/artifacts/{id}）：产物安全下载。

产物 = Run/会话工作区（AGENT_FILES_ROOT/{user}/{agent}/{session}）内的文件。
安全约束（P0-03 §15.2）：
- artifact_id 为不透明令牌（base64url 编码 session_id + 相对路径），
  不暴露服务器路径；
- 下载前校验会话归属（用户隔离）与路径包含性（realpath 必须在工作区内，
  防目录穿越/符号链接逃逸）；
- 仅允许常规文件下载，目录/特殊文件拒绝。
"""
from __future__ import annotations

import base64
import binascii
import os
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.core.database import get_db
from app.models.agent import AgentSession
from app.models.user import User

router = APIRouter(prefix="/artifacts", tags=["artifacts-v2"])


def make_artifact_id(session_id: int, rel_path: str) -> str:
    """生成不透明产物令牌（会话 ID + 工作区内相对路径）。"""
    raw = f"{session_id}:{rel_path}".encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def parse_artifact_id(artifact_id: str) -> tuple:
    """解析产物令牌 -> (session_id, rel_path)；非法令牌抛 ValueError。"""
    pad = "=" * (-len(artifact_id) % 4)
    try:
        raw = base64.urlsafe_b64decode(artifact_id + pad)
        text = raw.decode("utf-8")
    except (binascii.Error, UnicodeDecodeError) as e:
        raise ValueError("非法产物标识") from e
    session_part, sep, rel_path = text.partition(":")
    if not sep or not session_part.isdigit() or not rel_path:
        raise ValueError("非法产物标识")
    rel_path = rel_path.replace("\\", "/").lstrip("/")
    if ".." in rel_path.split("/"):
        raise ValueError("非法产物路径")
    return int(session_part), rel_path


@router.get("/{artifact_id}")
async def download_artifact(
    artifact_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """安全下载产物（UX-06）：归属校验 + 路径包含校验。"""
    from app.services.agent_runtime.backends import workspace_root

    try:
        session_id, rel_path = parse_artifact_id(artifact_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    res = await db.execute(
        select(AgentSession).where(
            AgentSession.id == session_id,
            AgentSession.user_id == current_user.id,
        )
    )
    session = res.scalar_one_or_none()
    if session is None:
        raise HTTPException(status_code=404, detail="产物不存在")

    root = workspace_root(current_user.id, session.agent_id, session.id)
    target = os.path.realpath(os.path.join(root, rel_path))
    root_real = os.path.realpath(root)
    if not (target == root_real or target.startswith(root_real + os.sep)):
        raise HTTPException(status_code=404, detail="产物不存在")
    if not os.path.isfile(target):
        raise HTTPException(status_code=404, detail="产物不存在")

    return FileResponse(target, filename=os.path.basename(target))
