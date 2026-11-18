"""会话附件上传（智能体对话文件处理）。

- 上传文件写入会话工作区 ``uploads/``（即 Agent 文件工具的 /workspace/uploads/），
  模型可用 read_file 直接读取处理；
- 二进制文档（PDF/Word/Excel/PPT/音频）复用知识库解析器同步抽取文本副本
  ``<名>.extracted.txt``，消息附件注记指向文本副本，模型无需解析二进制。
"""
from __future__ import annotations

import csv
import mimetypes
import os
import re

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
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


# ── 通用工作区文件预览（前端右侧临时预览区块）─────────────────────────────
# 语言提示（代码高亮用）；未登记扩展名按纯文本兜底
_CODE_LANG = {
    "py": "python", "js": "javascript", "ts": "typescript", "jsx": "jsx", "tsx": "tsx",
    "java": "java", "go": "go", "rs": "rust", "rb": "ruby", "php": "php",
    "c": "c", "h": "c", "cpp": "cpp", "hpp": "cpp", "cc": "cpp", "cs": "csharp",
    "sh": "bash", "bash": "bash", "zsh": "bash", "ps1": "powershell", "bat": "batch",
    "sql": "sql", "html": "html", "htm": "html", "css": "css", "scss": "scss", "less": "less",
    "xml": "xml", "json": "json", "yaml": "yaml", "yml": "yaml", "toml": "ini",
    "ini": "ini", "cfg": "ini", "conf": "ini", "vue": "vue", "swift": "swift",
    "kt": "kotlin", "scala": "scala", "r": "r", "m": "objectivec", "dart": "dart",
}
_IMAGE_EXTS = {"png", "jpg", "jpeg", "gif", "bmp", "webp", "svg", "ico"}
_MD_EXTS = {"md", "markdown"}
_TABLE_EXTS = {"csv", "xlsx", "xlsm"}
_OFFICE_EXTS = {"doc", "docx", "ppt", "pptx", "xls"}  # 老格式走文本抽取
_TEXT_EXTS = {"txt", "log", "text"}
_AUDIO_EXTS = {"mp3", "wav", "m4a", "ogg", "flac"}

_MAX_PREVIEW_TEXT = 200_000  # 预览文本截断阈值（字符）
_MAX_PREVIEW_ROWS = 500  # 表格预览最大行数


def _classify(ext: str) -> tuple[str, str]:
    """扩展名 -> (kind, language)。kind 驱动前端渲染分支。"""
    e = ext.lstrip(".").lower()
    if e in _IMAGE_EXTS:
        return "image", ""
    if e == "pdf":
        return "pdf", ""
    if e in _MD_EXTS:
        return "markdown", ""
    if e in _TABLE_EXTS:
        return "table", ""
    if e in _OFFICE_EXTS or e in _AUDIO_EXTS:
        return "office", ""  # Word/PPT/Excel(旧)/音频 -> 后端抽取文本预览
    if e in _TEXT_EXTS:
        return "text", ""
    if e in _CODE_LANG:
        return "code", _CODE_LANG[e]
    return "text", ""


def _resolve_workspace_path(root: str, rel: str) -> str:
    """把工作区相对路径解析为绝对路径，拒绝路径穿越（含 /workspace/ 前缀兼容）。"""
    r = (rel or "").replace("\\", "/").strip()
    r = re.sub(r"^/+", "", r)
    if r.startswith("workspace/"):
        r = r[len("workspace/"):]
    segs = r.split("/")
    if ".." in segs:
        raise HTTPException(status_code=400, detail="非法路径")
    real_root = os.path.realpath(root)
    target = os.path.realpath(os.path.join(root, r))
    if target != real_root and not target.startswith(real_root + os.sep):
        raise HTTPException(status_code=400, detail="非法路径")
    return target


def _read_text_capped(path: str) -> tuple[str, bool]:
    """读取文本（UTF-8，容错），按字符阈值截断。"""
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        text = f.read(_MAX_PREVIEW_TEXT + 1)
    truncated = len(text) > _MAX_PREVIEW_TEXT
    return text[:_MAX_PREVIEW_TEXT], truncated


def _looks_binary(path: str) -> bool:
    """前 4KB 含 NUL 字节视为二进制（前端兜底为下载 + 元信息，不硬当文本渲染）。"""
    try:
        with open(path, "rb") as f:
            return b"\x00" in f.read(4096)
    except Exception:  # noqa: BLE001
        return False


def _read_table(path: str, ext: str) -> tuple[list[list[str]], bool]:
    """表格预览：csv / xlsx(xlsm) 解析为二维数组（截断行数）。"""
    e = ext.lstrip(".").lower()
    if e == "csv":
        rows: list[list[str]] = []
        with open(path, "r", encoding="utf-8", errors="replace", newline="") as f:
            for i, row in enumerate(csv.reader(f)):
                if i >= _MAX_PREVIEW_ROWS:
                    return rows, True
                rows.append([str(c) for c in row])
        return rows, False
    if e in ("xlsx", "xlsm"):
        try:
            import openpyxl

            wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
            ws = wb.worksheets[0] if wb.worksheets else None
            if ws is None:
                return [], False
            rows = []
            for i, row in enumerate(ws.iter_rows(values_only=True)):
                if i >= _MAX_PREVIEW_ROWS:
                    return rows, True
                rows.append(["" if c is None else str(c) for c in row])
            return rows, False
        except Exception:  # noqa: BLE001
            return [], False
    return [], False


async def _extract_office_text(path: str, ext: str) -> str:
    """Office/音频 -> 抽取文本预览（复用知识库解析器；失败返回空）。"""
    try:
        from app.services.knowledge_service import KnowledgeService

        return await KnowledgeService.extract_text(path, ext.lstrip(".").lower()) or ""
    except Exception:  # noqa: BLE001
        return ""


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


@router.get("/{session_id}/files/{filename}")
async def download_conversation_file(
    session_id: int,
    filename: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """下载已上传附件（前端图片预览用；仅限 uploads/ 目录内）。"""
    session = await _get_owned_session(db, session_id, current_user)
    upload_dir = os.path.join(
        workspace_root(current_user.id, session.agent_id, session.id), "uploads"
    )
    name = _safe_name(filename)
    target = os.path.realpath(os.path.join(upload_dir, name))
    if not target.startswith(os.path.realpath(upload_dir) + os.sep):
        raise HTTPException(status_code=400, detail="非法文件名")
    if not os.path.isfile(target):
        raise HTTPException(status_code=404, detail="文件不存在")
    return FileResponse(target)


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


@router.get("/{session_id}/file-preview")
async def preview_conversation_file(
    session_id: int,
    path: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """通用工作区文件预览元数据（前端右侧临时预览区块）。

    覆盖会话工作区任意文件（uploads/ 附件、Agent 读写产物等）。
    按扩展名分类返回 ``kind``；文本/代码/Markdown 返回 ``text``，
    表格返回 ``rows``，Office/音频返回抽取 ``text``；图片/PDF/二进制仅返回
    元信息（字节经 ``file-bytes`` 取）。
    """
    session = await _get_owned_session(db, session_id, current_user)
    root = workspace_root(current_user.id, session.agent_id, session.id)
    target = _resolve_workspace_path(root, path)
    if not os.path.isfile(target):
        raise HTTPException(status_code=404, detail="文件不存在")

    name = os.path.basename(target)
    ext = os.path.splitext(name)[1]
    kind, language = _classify(ext)
    size = os.path.getsize(target)
    mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
    rel = os.path.relpath(target, root).replace(os.sep, "/")

    # 文本类兜底：含 NUL 的"文本"按二进制处理（下载 + 元信息，不硬渲染乱码）
    if kind in ("text", "code", "markdown") and _looks_binary(target):
        kind = "binary"
        language = ""

    data: dict = {
        "name": name, "path": rel, "kind": kind, "size": size,
        "mime": mime, "language": language, "truncated": False,
    }
    if kind in ("text", "code", "markdown"):
        text, truncated = _read_text_capped(target)
        data["text"] = text
        data["truncated"] = truncated
    elif kind == "table":
        rows, truncated = _read_table(target, ext)
        data["rows"] = rows
        data["truncated"] = truncated
        if not rows and ext.lstrip(".").lower() in ("xlsx", "xlsm"):
            # xlsx 解析失败（缺 openpyxl / 损坏）兜底：尝试文本抽取
            data["text"] = await _extract_office_text(target, ext)
    elif kind == "office":
        data["text"] = await _extract_office_text(target, ext)
    return data


@router.get("/{session_id}/file-bytes")
async def conversation_file_bytes(
    session_id: int,
    path: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """返回工作区任意文件字节（图片/PDF 预览、二进制下载）。"""
    session = await _get_owned_session(db, session_id, current_user)
    root = workspace_root(current_user.id, session.agent_id, session.id)
    target = _resolve_workspace_path(root, path)
    if not os.path.isfile(target):
        raise HTTPException(status_code=404, detail="文件不存在")
    mime = mimetypes.guess_type(target)[0] or "application/octet-stream"
    return FileResponse(target, media_type=mime)
