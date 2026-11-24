"""Artifacts API v2（§7 /api/v2/artifacts/{id}）：产物安全下载。

产物 = Run/会话工作区（AGENT_FILES_ROOT/{user}/{agent}/{session}）内的文件。
安全约束（P0-03 §15.2）：
- artifact_id 为不透明令牌（base64url 编码 session_id + 相对路径），
  不暴露服务器路径；
- 下载前校验会话归属（用户隔离）与路径包含性（realpath 必须在工作区内，
  防目录穿越/符号链接逃逸）；
- 仅允许常规文件下载，目录/特殊文件拒绝。
另有 ``/artifacts/runs/{run_id}/zip``：打包下载 Run 已发布的交付文件。
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import os
import re
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.common.zipstream import zip_stream_response
from app.api.deps import get_current_user
from app.core.database import get_db
from app.models.agent import AgentRun, AgentSession
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


def _summary_artifact_paths(summary: Optional[dict]) -> list:
    """旧版 Run 的交付清单（summary.verification.artifacts[].path）。"""
    if not isinstance(summary, dict):
        return []
    report = summary.get("verification") if isinstance(summary.get("verification"), dict) else {}
    paths = []
    for item in report.get("artifacts") or []:
        if not isinstance(item, dict) or not isinstance(item.get("path"), str):
            continue
        rel = item["path"].replace("\\", "/").lstrip("/")
        if rel and ".." not in rel.split("/"):
            paths.append(rel)
    return paths


def _collect_run_artifacts(root: str, run_id: str, fallback_paths: list) -> list:
    """收集 Run 交付文件 -> (zip 条目名, 源文件路径)。

    优先取冻结副本 ``.run-artifacts/{run_id}/{sha256}/{path}``（同名多版本取最新）；
    旧版 Run 无冻结副本时回退工作区原路径。
    """
    from app.services.agent_runtime.backends import RUN_ARTIFACT_FOLDER

    real_root = os.path.realpath(root)
    frozen_root = os.path.join(root, RUN_ARTIFACT_FOLDER, run_id)
    chosen: dict = {}
    for dirpath, dirnames, filenames in os.walk(frozen_root, topdown=True, followlinks=False):
        dirnames[:] = [name for name in sorted(dirnames) if not os.path.islink(os.path.join(dirpath, name))]
        for name in sorted(filenames):
            path = os.path.join(dirpath, name)
            if os.path.islink(path):
                continue
            rel = os.path.relpath(path, frozen_root).replace(os.sep, "/")
            digest, sep, entry = rel.partition("/")
            if not sep or not entry or not re.fullmatch(r"[a-f0-9]{64}", digest):
                continue
            real = os.path.realpath(path)
            if not (real == real_root or real.startswith(real_root + os.sep)) or not os.path.isfile(real):
                continue
            mtime = os.path.getmtime(path)
            if entry not in chosen or mtime > chosen[entry][0]:
                chosen[entry] = (mtime, path)
    if chosen:
        return sorted((entry, path) for entry, (_, path) in chosen.items())
    entries = []
    for rel in dict.fromkeys(fallback_paths):
        path = os.path.realpath(os.path.join(root, rel))
        if (path == real_root or path.startswith(real_root + os.sep)) and os.path.isfile(path):
            entries.append((rel, path))
    return sorted(entries)


@router.get("/runs/{run_id}/zip")
async def download_run_artifacts_zip(
    run_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """打包下载 Run 已发布的交付文件（zip 内条目名为工作区相对路径）。"""
    from app.services.agent_runtime.backends import workspace_root

    row = (await db.execute(
        select(AgentRun.session_id, AgentRun.agent_id, AgentRun.summary)
        .where(AgentRun.id == run_id, AgentRun.user_id == current_user.id)
    )).one_or_none()
    if row is None or row.session_id is None:
        raise HTTPException(status_code=404, detail="产物不存在")
    root = workspace_root(current_user.id, row.agent_id, row.session_id)
    entries = await asyncio.to_thread(
        _collect_run_artifacts, root, run_id, _summary_artifact_paths(row.summary),
    )
    if not entries:
        raise HTTPException(status_code=404, detail="产物不存在")
    return zip_stream_response(entries, f"run-{run_id}.zip")


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
