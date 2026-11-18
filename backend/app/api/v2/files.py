"""会话附件上传（智能体对话文件处理）。

- 上传文件写入会话工作区 ``uploads/``（即 Agent 文件工具的 /workspace/uploads/），
  模型可用 read_file 直接读取处理；
- 二进制文档（PDF/Word/Excel/PPT/音频）复用知识库解析器同步抽取文本副本
  ``<名>.extracted.txt``，消息附件注记指向文本副本，模型无需解析二进制。
"""
from __future__ import annotations

import os
import re

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.core.config import settings
from app.core.database import get_db
from app.models.user import User
from app.services.agent_runtime.backends import workspace_root

from .conversations import _get_owned_session

router = APIRouter(prefix="/conversations", tags=["files-v2"])

# 可文本抽取的二进制格式（复用知识库解析器）
_EXTRACT_EXTS = {
    "pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx",
    "mp3", "wav", "m4a", "ogg", "flac",
}


def _safe_name(name: str) -> str:
    """仅保留 basename 并清除路径分隔/控制字符，防路径穿越。"""
    base = os.path.basename((name or "").replace("\\", "/")).strip()
    base = re.sub(r'[\x00-\x1f<>:"|?*]', "_", base)
    return base or "file"


@router.post("/{session_id}/files")
async def upload_conversation_file(
    session_id: int,
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """上传附件到会话工作区 ``uploads/``（重复名自动加序号；超限 413）。"""
    session = await _get_owned_session(db, session_id, current_user)
    upload_dir = os.path.join(
        workspace_root(current_user.id, session.agent_id, session.id), "uploads"
    )
    os.makedirs(upload_dir, exist_ok=True)

    name = _safe_name(file.filename or "")
    stem, ext = os.path.splitext(name)
    dest = os.path.join(upload_dir, name)
    seq = 1
    while os.path.exists(dest):
        name = f"{stem}-{seq}{ext}"
        dest = os.path.join(upload_dir, name)
        seq += 1

    max_bytes = int(getattr(settings, "AGENT_MAX_FILE_SIZE_MB", 10) * 1024 * 1024)
    size = 0
    with open(dest, "wb") as out:
        while chunk := await file.read(1024 * 1024):
            size += len(chunk)
            if size > max_bytes:
                out.close()
                os.remove(dest)
                raise HTTPException(status_code=413, detail="文件超过大小限制")
            out.write(chunk)

    # 二进制文档抽取文本副本（失败不阻断上传，仅无文本副本）
    text_path = None
    ext_l = ext.lstrip(".").lower()
    if ext_l in _EXTRACT_EXTS:
        try:
            from app.services.knowledge_service import KnowledgeService

            text = await KnowledgeService.extract_text(dest, ext_l)
        except Exception:  # noqa: BLE001
            text = ""
        if text and not text.startswith("[文档解析失败") and not text.startswith("[音频转写"):
            tname = f"{name}.extracted.txt"
            with open(os.path.join(upload_dir, tname), "w", encoding="utf-8") as f:
                f.write(text)
            text_path = f"uploads/{tname}"

    return {"name": name, "path": f"uploads/{name}", "text_path": text_path, "size": size}


@router.delete("/{session_id}/files/{filename}")
async def delete_conversation_file(
    session_id: int,
    filename: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """删除已上传附件（含文本副本；仅限 uploads/ 目录内）。"""
    session = await _get_owned_session(db, session_id, current_user)
    upload_dir = os.path.join(
        workspace_root(current_user.id, session.agent_id, session.id), "uploads"
    )
    name = _safe_name(filename)
    for fname in (name, f"{name}.extracted.txt"):
        target = os.path.realpath(os.path.join(upload_dir, fname))
        if not target.startswith(os.path.realpath(upload_dir) + os.sep):
            raise HTTPException(status_code=400, detail="非法文件名")
        if os.path.isfile(target):
            os.remove(target)
    return {"message": "已删除"}
