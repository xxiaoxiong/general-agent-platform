"""会话附件上传（智能体对话文件处理）。

- 上传文件写入会话工作区 ``uploads/``（即 Agent 文件工具的 /workspace/uploads/），
  模型可用 read_file 直接读取处理；
- 常见文档和音频抽取文本副本 ``<名>.extracted.txt``，消息附件注记指向文本副本，
  模型无需解析二进制。文档智能支持的二进制格式经过受限解析器校验。
"""
from __future__ import annotations

import asyncio
import csv
from contextlib import suppress
from itertools import count
import mimetypes
import os
from pathlib import Path
import re

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.common.zipstream import zip_stream_response
from app.api.deps import get_current_user
from app.core.config import settings
from app.core.database import get_db
from app.models.user import User
from app.services.agent_runtime.backends import workspace_root
from app.services.audit.context import record_attachment_change
from app.services.documents.errors import DocumentError
from app.services.documents.office_preview import render_docx_pdf
from app.services.documents.parser import DocumentParseError, parse_document, validate_reference_file

from .conversations import _get_owned_session

router = APIRouter(prefix="/conversations", tags=["files-v2"])

# 旧格式和音频保留知识库抽取流程。
_DOCUMENT_EXTS = {"pdf", "docx", "xlsx", "pptx"}
_LEGACY_OFFICE_EXTS = {"doc", "xls", "ppt"}
_AUDIO_EXTS = {"mp3", "wav", "m4a", "ogg", "flac"}
_EXTRACT_EXTS = _DOCUMENT_EXTS | _LEGACY_OFFICE_EXTS | _AUDIO_EXTS


def _safe_name(name: str) -> str:
    """仅保留 basename 并清除路径分隔/控制字符，防路径穿越。"""
    base = os.path.basename((name or "").replace("\\", "/")).strip()
    base = re.sub(r'[\x00-\x1f\x7f<>:"|?*]', "_", base).rstrip(". ")
    if re.fullmatch(r"(?:con|prn|aux|nul|com[1-9]|lpt[1-9])", base.partition(".")[0], re.I):
        base = f"_{base}"
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
_TABLE_EXTS = {"csv", "xlsx"}
_OFFICE_EXTS = _LEGACY_OFFICE_EXTS | {"docx", "pptx"}
_TEXT_EXTS = {"txt", "log", "text"}

_MAX_PREVIEW_TEXT = 200_000  # 预览文本截断阈值（字符）
_MAX_PREVIEW_ROWS = 500  # 表格预览最大行数

# 工作区文件清单（右侧文件面板）：排除平台内部目录与模型用文本副本
_INTERNAL_DIRS = {"tool-outputs", "tool_results"}
_EXTRACT_SUFFIX = ".extracted.txt"
_MAX_LIST_FILES = 500  # 清单条目上限（超出置 truncated）
_MAX_LIST_DEPTH = 8  # 清单递归深度上限


def _classify(ext: str) -> tuple[str, str]:
    """扩展名 -> (kind, language)。kind 驱动前端渲染分支。"""
    e = ext.lstrip(".").lower()
    if e in _IMAGE_EXTS:
        return "image", ""
    if e == "pdf":
        return "pdf", ""
    if e in {"docm", "xlsm", "pptm"}:
        return "binary", ""
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
    """表格预览按第一张工作表返回有界的二维数组。"""
    e = ext.lstrip(".").lower()
    if e == "csv":
        rows: list[list[str]] = []
        with open(path, "r", encoding="utf-8", errors="replace", newline="") as f:
            for i, row in enumerate(csv.reader(f)):
                if i >= _MAX_PREVIEW_ROWS:
                    return rows, True
                rows.append([str(c) for c in row])
        return rows, False
    if e == "xlsx":
        from openpyxl import load_workbook

        checked = validate_reference_file(path)
        try:
            workbook = load_workbook(checked, read_only=True, data_only=True, keep_links=False)
            try:
                sheet = workbook.worksheets[0] if workbook.worksheets else None
                if sheet is None:
                    return [], False
                max_columns = 30
                rows = []
                remaining = _MAX_PREVIEW_TEXT
                truncated = (sheet.max_column or 0) > max_columns
                for row in sheet.iter_rows(max_col=min(sheet.max_column or max_columns, max_columns), values_only=True):
                    if len(rows) >= _MAX_PREVIEW_ROWS or not remaining:
                        return rows, True
                    cells = []
                    for cell in row:
                        value = "" if cell is None else str(cell)
                        preview = value[:min(2_000, remaining)]
                        truncated |= len(preview) < len(value)
                        remaining -= len(preview)
                        cells.append(preview)
                    rows.append(cells)
                return rows, truncated
            finally:
                workbook.close()
        except Exception as exc:
            raise DocumentParseError("无法解析工作表，请检查文件格式") from exc
    return [], False


async def _extract_office_text(path: str, ext: str) -> str:
    """旧 Office 格式和音频保留知识库解析器的文本预览。"""
    try:
        from app.services.knowledge_service import KnowledgeService

        return await KnowledgeService.extract_text(path, ext.lstrip(".").lower()) or ""
    except Exception:  # noqa: BLE001
        return ""


def _document_text(path: str | Path, mime: str | None = None) -> str:
    parsed = parse_document(path, mime)
    return "\n\n".join([parsed.as_text(), *parsed.warnings])


@router.post("/{session_id}/files")
async def upload_conversation_file(
    session_id: int,
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """上传附件到会话工作区 ``uploads/``（重复名自动加序号；超限 413）。"""
    session = await _get_owned_session(db, session_id, current_user)
    root = workspace_root(current_user.id, session.agent_id, session.id)
    upload_dir = Path(_resolve_workspace_path(root, "uploads"))
    upload_dir.mkdir(parents=True, exist_ok=True)

    name = _safe_name(file.filename or "")
    if len(name.encode("utf-8")) > 230:
        raise HTTPException(status_code=400, detail="文件名过长")
    stem, ext = os.path.splitext(name)
    ext_l = ext.lstrip(".").lower()
    reserve_sidecar = ext_l in _EXTRACT_EXTS
    extracted = None
    for seq in count():
        if seq:
            name = f"{stem}-{seq}{ext}"
        dest = upload_dir / name
        sidecar = upload_dir / f"{name}.extracted.txt"
        if dest.is_dir() or (reserve_sidecar and sidecar.is_dir()):
            continue
        try:
            out = dest.open("xb")
        except FileExistsError:
            continue
        if reserve_sidecar:
            try:
                extracted = sidecar.open("x", encoding="utf-8")
            except FileExistsError:
                out.close()
                dest.unlink()
                continue
            except OSError:
                out.close()
                dest.unlink()
                raise
        break

    text_path = None
    max_bytes = int(getattr(settings, "AGENT_MAX_FILE_SIZE_MB", 10) * 1024 * 1024)
    size = 0
    try:
        with out:
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if size > max_bytes:
                    raise HTTPException(status_code=413, detail="文件超过大小限制")
                out.write(chunk)

        text = ""
        if ext_l in _DOCUMENT_EXTS:
            try:
                text = await asyncio.to_thread(_document_text, dest, file.content_type)
            except DocumentParseError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
        elif ext_l in _EXTRACT_EXTS:
            text = await _extract_office_text(str(dest), ext)
        if extracted is not None:
            has_text = bool(text) and not text.startswith(("[文档解析失败", "[音频转写"))
            with extracted:
                if has_text:
                    extracted.write(text)
            if has_text:
                text_path = f"uploads/{sidecar.name}"
            else:
                sidecar.unlink()
    except BaseException:
        with suppress(OSError):
            dest.unlink()
        if extracted is not None:
            with suppress(OSError):
                extracted.close()
            with suppress(OSError):
                sidecar.unlink()
        raise

    record_attachment_change(session.id, after={"name": name, "size": size})
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
    root = workspace_root(current_user.id, session.agent_id, session.id)
    upload_dir = _resolve_workspace_path(root, "uploads")
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
    root = workspace_root(current_user.id, session.agent_id, session.id)
    upload_dir = _resolve_workspace_path(root, "uploads")
    name = _safe_name(filename)
    names = [name]
    if os.path.splitext(name)[1].lstrip(".").lower() in _EXTRACT_EXTS:
        names.append(f"{name}.extracted.txt")
    for fname in names:
        target = os.path.realpath(os.path.join(upload_dir, fname))
        if not target.startswith(os.path.realpath(upload_dir) + os.sep):
            raise HTTPException(status_code=400, detail="非法文件名")
        if os.path.isfile(target):
            size = os.stat(target).st_size
            os.remove(target)
            if fname == name:
                record_attachment_change(session.id, before={"name": name, "size": size})
    return {"message": "已删除"}


# ── 工作区文件清单与打包（右侧文件面板）────────────────────────────────────
def _iter_workspace_files(root: str) -> tuple[list[tuple[str, str]], bool]:
    """递归列出工作区用户可见文件 -> [(工作区内相对路径, 绝对路径)], 是否截断。

    排除点前缀隐藏条目（.run-artifacts 等平台内部目录）、工具输出转储目录、
    模型用文本副本 ``*.extracted.txt`` 与符号链接；限制深度和条目数防超大目录。
    """
    real_root = os.path.realpath(root)
    found: list[tuple[str, str]] = []
    truncated = False
    for dirpath, dirnames, filenames in os.walk(root, topdown=True, followlinks=False):
        rel_dir = os.path.relpath(dirpath, root)
        depth = 0 if rel_dir == "." else rel_dir.count(os.sep) + 1
        dirnames[:] = [
            name for name in sorted(dirnames)
            if depth < _MAX_LIST_DEPTH and not name.startswith(".")
            and name.casefold() not in _INTERNAL_DIRS
            and not os.path.islink(os.path.join(dirpath, name))
        ]
        for name in sorted(filenames):
            path = os.path.join(dirpath, name)
            if name.startswith(".") or name.endswith(_EXTRACT_SUFFIX) or os.path.islink(path):
                continue
            real = os.path.realpath(path)
            if not (real == real_root or real.startswith(real_root + os.sep)) or not os.path.isfile(real):
                continue
            found.append((os.path.relpath(path, root).replace(os.sep, "/"), path))
            if len(found) > _MAX_LIST_FILES:
                truncated = True
                break
        if truncated:
            break
    found.sort(key=lambda item: item[0])
    if len(found) > _MAX_LIST_FILES:
        truncated = True
        found = found[:_MAX_LIST_FILES]
    return found, truncated


@router.get("/{session_id}/file-list")
async def list_conversation_files(
    session_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """列出会话工作区文件（右侧文件面板：过程中产生的文件可点击预览）。"""
    session = await _get_owned_session(db, session_id, current_user)
    root = workspace_root(current_user.id, session.agent_id, session.id)
    files, truncated = await asyncio.to_thread(_iter_workspace_files, root)
    entries = []
    for rel, path in files:
        try:
            stat = os.stat(path)
        except OSError:
            continue
        entries.append({
            "path": rel,
            "name": os.path.basename(rel),
            "size": stat.st_size,
            "mtime": stat.st_mtime,
            "kind": _classify(os.path.splitext(rel)[1])[0],
        })
    return {"files": entries, "truncated": truncated}


@router.get("/{session_id}/files.zip")
async def download_conversation_files_zip(
    session_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """打包下载会话工作区文件（与 file-list 同一范围，条目名为工作区相对路径）。"""
    session = await _get_owned_session(db, session_id, current_user)
    root = workspace_root(current_user.id, session.agent_id, session.id)
    files, _ = await asyncio.to_thread(_iter_workspace_files, root)
    return zip_stream_response(files, f"workspace-{session.id}.zip")


@router.get("/{session_id}/file-preview")
async def preview_conversation_file(
    session_id: int,
    path: str,
    layout_only: bool = False,
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
    try:
        if kind in ("text", "code", "markdown"):
            data["text"], data["truncated"] = await asyncio.to_thread(_read_text_capped, target)
        elif kind == "table":
            data["rows"], data["truncated"] = await asyncio.to_thread(_read_table, target, ext)
        elif kind == "office" and layout_only and ext.lower() == ".docx":
            # 版式端点会统一校验并转换，预览元数据无需重复抽取全文。
            pass
        elif kind == "office" and ext.lstrip(".").lower() in _DOCUMENT_EXTS:
            data["text"] = await asyncio.to_thread(_document_text, target)
        elif kind == "office":
            data["text"] = await _extract_office_text(target, ext)
        elif kind == "pdf":
            await asyncio.to_thread(validate_reference_file, target)
    except DocumentParseError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except csv.Error as exc:
        raise HTTPException(status_code=422, detail="无法解析 CSV，请检查文件格式") from exc
    return data


@router.get("/{session_id}/file-preview-pdf", response_class=Response)
async def preview_conversation_word_layout(
    session_id: int,
    path: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """按原始版式渲染工作区中的 DOCX。"""
    session = await _get_owned_session(db, session_id, current_user)
    root = workspace_root(current_user.id, session.agent_id, session.id)
    target = Path(_resolve_workspace_path(root, path))
    if not target.is_file():
        raise HTTPException(status_code=404, detail="文件不存在")
    await db.rollback()
    try:
        data = await render_docx_pdf(target)
    except DocumentError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc
    return Response(data, media_type="application/pdf", headers={
        "Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff",
    })


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
