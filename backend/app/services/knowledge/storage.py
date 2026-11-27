"""知识库文件存储：路径全部由服务端生成，上传边读边计数。

目录结构（``KNOWLEDGE_STORAGE_DIR``）::

    kb_{kb_id}/doc_{doc_id}/v{version_no}/source.{ext}
    kb_{kb_id}/doc_{doc_id}/v{version_no}/parsed.json
    kb_{kb_id}/doc_{doc_id}/v{version_no}/embedded.json   # Chunk 预览/审计快照

旧版上传目录（``UPLOAD_DIR/{kb_id}``）保留只读兼容，用于 Phase 6 迁移。
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from app.core.config import settings


@dataclass
class StoredFile:
    path: Path
    size: int
    sha256: str
    storage_key: str


def knowledge_root() -> Path:
    root = Path(settings.KNOWLEDGE_STORAGE_DIR).resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


def _safe_component(value: str) -> str:
    text = "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in str(value))
    return text[:120] or "x"


def document_dir(kb_id: int, doc_id: int) -> Path:
    path = knowledge_root() / f"kb_{int(kb_id)}" / f"doc_{int(doc_id)}"
    path.mkdir(parents=True, exist_ok=True)
    return path


def version_dir(kb_id: int, doc_id: int, version_no: int) -> Path:
    path = document_dir(kb_id, doc_id) / f"v{int(version_no)}"
    path.mkdir(parents=True, exist_ok=True)
    return path


def resolve_storage_key(storage_key: Optional[str]) -> Optional[Path]:
    """把数据库中的相对 storage_key 解析为受控绝对路径（越界返回 None）。"""
    if not storage_key:
        return None
    root = knowledge_root()
    target = (root / storage_key).resolve()
    if target == root or str(target).startswith(str(root) + os.sep):
        return target
    return None


def _relative(path: Path) -> str:
    return str(path.resolve().relative_to(knowledge_root())).replace("\\", "/")


def relative_key(path: Path) -> str:
    """把知识库存储目录内的路径转为数据库 storage_key。"""
    return _relative(path)


async def save_upload_stream(upload, *, kb_id: int, doc_id: int, version_no: int,
                             ext: str, max_bytes: int) -> StoredFile:
    """流式写入上传文件：边读边计数 + SHA256，超限立即终止并清理。"""
    dest_dir = version_dir(kb_id, doc_id, version_no)
    final_path = dest_dir / f"source.{ext}"
    part_path = dest_dir / f"source.{ext}.part"
    digest = hashlib.sha256()
    size = 0
    try:
        with open(part_path, "wb") as fh:
            while True:
                chunk = await upload.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > max_bytes:
                    raise ValueError(f"文件大小超过限制 {settings.KNOWLEDGE_MAX_UPLOAD_SIZE_MB}MB")
                digest.update(chunk)
                fh.write(chunk)
        if size == 0:
            raise ValueError("文件为空")
        os.replace(part_path, final_path)
    except Exception:
        part_path.unlink(missing_ok=True)
        final_path.unlink(missing_ok=True)
        raise
    return StoredFile(
        path=final_path, size=size, sha256=digest.hexdigest(), storage_key=_relative(final_path),
    )


def save_text_source(*, kb_id: int, doc_id: int, version_no: int, text: str,
                     ext: str = "txt") -> StoredFile:
    """把网页抓取等文本内容保存为受控源文件。"""
    dest_dir = version_dir(kb_id, doc_id, version_no)
    path = dest_dir / f"source.{ext}"
    data = text.encode("utf-8")
    path.write_bytes(data)
    return StoredFile(
        path=path, size=len(data), sha256=hashlib.sha256(data).hexdigest(),
        storage_key=_relative(path),
    )


def write_json_artifact(path: Path, payload: Any) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False)
    os.replace(tmp, path)
    return _relative(path)


def read_json_artifact(path: Optional[Path]) -> Optional[Any]:
    if path is None or not path.is_file():
        return None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None


def parsed_artifact_path(kb_id: int, doc_id: int, version_no: int) -> Path:
    return version_dir(kb_id, doc_id, version_no) / "parsed.json"


def delete_document_files(kb_id: int, doc_id: int) -> None:
    path = knowledge_root() / f"kb_{int(kb_id)}" / f"doc_{int(doc_id)}"
    if path.exists():
        shutil.rmtree(path, ignore_errors=False)


def delete_kb_files(kb_id: int) -> None:
    path = knowledge_root() / f"kb_{int(kb_id)}"
    if path.exists():
        shutil.rmtree(path, ignore_errors=False)


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sanitize_filename(name: str) -> str:
    """展示用文件名（不允许路径分隔符与控制字符）。"""
    base = os.path.basename(str(name or "document"))
    base = "".join(ch for ch in base if ch.isprintable() and ch not in '\\/:*?"<>|')
    return base[:300] or "document"
