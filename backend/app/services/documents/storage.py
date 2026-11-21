"""平台管理的文档版本文件。路径只由所有者、文档和版本数字派生。"""

import hashlib
import os
import re
import shutil
import stat
import tempfile
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Iterator

from app.core.config import settings
from app.services.documents.errors import DocumentError

DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
_RUN_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")
_ID = re.compile(r"[1-9][0-9]{0,9}")
_VERSION_FILE = re.compile(r"v([1-9][0-9]{0,9})\.docx")
_STAGING_FOLDER = re.compile(r"([A-Za-z0-9_-]{1,64})-[a-z0-9_]{8}")


@dataclass(frozen=True)
class VersionFile:
    owner_id: int
    document_id: int
    version_no: int


@dataclass(frozen=True)
class StagingFolder:
    owner_id: int
    run_id: str
    folder: str


def _key(value: int) -> str:
    if type(value) is not int or value < 1:
        raise DocumentError(400, "无效的文档存储标识")
    return str(value)


def _child(base: Path, parts: tuple[str, ...]) -> Path:
    target = base
    for part in parts:
        if not part or part in {".", ".."} or "/" in part or "\\" in part or ":" in part:
            raise DocumentError(400, "无效的文档存储路径")
        target /= part
        if target.is_symlink():
            raise DocumentError(400, "文档存储路径包含符号链接")
    if not target.resolve().is_relative_to(base):
        raise DocumentError(400, "文档存储路径越界")
    return target


def root() -> Path:
    return Path(settings.AGENT_FILES_ROOT).resolve()


def relative_path(owner_id: int, document_id: int, version_no: int) -> str:
    return f"{_key(owner_id)}/documents/{_key(document_id)}/v{_key(version_no)}.docx"


def version_path(owner_id: int, document_id: int, version_no: int, stored: str | None = None) -> Path:
    expected = relative_path(owner_id, document_id, version_no)
    if stored is not None and stored != expected:
        raise DocumentError(409, "文档版本存储标识不一致")
    return _child(root(), PurePosixPath(expected).parts)


@contextmanager
def staging_docx(owner_id: int, run_id: str) -> Iterator[Path]:
    if not _RUN_ID.fullmatch(run_id):
        raise DocumentError(400, "无效的文档运行标识")
    staging = _child(root(), (_key(owner_id), "documents", ".staging"))
    staging.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=staging, prefix=f"{run_id}-") as folder:
        yield Path(folder) / "draft.docx"


def fingerprint(path: Path) -> tuple[str, int]:
    try:
        with path.open("rb") as stream:
            size = os.fstat(stream.fileno()).st_size
            return hashlib.file_digest(stream, "sha256").hexdigest(), size
    except OSError:
        raise DocumentError(404, "文档文件不可用") from None


def put_version(staged: Path, owner_id: int, document_id: int, version_no: int) -> Path:
    """调用方须持资产锁，确认目标版本在数据库尚未发布。"""
    target = version_path(owner_id, document_id, version_no)
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        with staged.open("r+b") as stream:
            os.fsync(stream.fileno())
        os.replace(staged, target)
    except OSError:
        raise DocumentError(503, "无法保存文档文件，请稍后重试") from None
    return target


def verified_version(owner_id: int, document_id: int, version_no: int,
                     stored: str, digest: str, size: int) -> Path:
    target = version_path(owner_id, document_id, version_no, stored)
    if fingerprint(target) != (digest, size):
        raise DocumentError(409, "文档文件完整性校验失败")
    return target


def _children(parent: Path) -> Iterator[Path]:
    try:
        yield from parent.iterdir()
    except FileNotFoundError:
        return


def _old(path: Path, cutoff: float) -> bool:
    try:
        return path.stat(follow_symlinks=False).st_mtime <= cutoff
    except FileNotFoundError:
        return False


def cleanup_candidates(cutoff: float) -> Iterator[VersionFile | StagingFolder]:
    """只枚举程序生成的文档及暂存命名，不进入用户的会话附件路径。"""
    base = root()
    if not base.is_dir():
        return
    for owner in _children(base):
        if not _ID.fullmatch(owner.name) or owner.is_symlink():
            continue
        documents = owner / "documents"
        if documents.is_symlink() or not documents.is_dir():
            continue
        for directory in _children(documents):
            if directory.is_symlink() or not directory.is_dir():
                continue
            if directory.name == ".staging":
                for folder in _children(directory):
                    match = _STAGING_FOLDER.fullmatch(folder.name)
                    if match and not folder.is_symlink() and folder.is_dir() and _old(folder, cutoff):
                        yield StagingFolder(int(owner.name), match[1], folder.name)
            elif _ID.fullmatch(directory.name):
                for file in _children(directory):
                    match = _VERSION_FILE.fullmatch(file.name)
                    if match and not file.is_symlink() and file.is_file() and _old(file, cutoff):
                        yield VersionFile(int(owner.name), int(directory.name), int(match[1]))


def discard_version_candidate(candidate: VersionFile, *, apply: bool,
                              cutoff: float | None = None, expected: tuple[str, int] | None = None) -> bool:
    """数据库已确认路径没有已提交版本，且调用方在清理期间持有相应行锁。"""
    target = version_path(candidate.owner_id, candidate.document_id, candidate.version_no)
    try:
        info = target.stat(follow_symlinks=False)
    except FileNotFoundError:
        return False
    if not stat.S_ISREG(info.st_mode) or (cutoff is not None and info.st_mtime > cutoff):
        return False
    if expected is not None and fingerprint(target) != expected:
        return False
    if apply:
        target.unlink(missing_ok=True)
        with suppress(OSError):
            target.parent.rmdir()
    return True


def discard_staging_candidate(candidate: StagingFolder, *, apply: bool, cutoff: float) -> bool:
    """只移除过期的标准暂存目录，遇到意外内容时保留目录供检查。"""
    directory = _child(root(), (_key(candidate.owner_id), "documents", ".staging", candidate.folder))
    try:
        if directory.stat().st_mtime > cutoff:
            return False
        files = list(directory.iterdir())
        for file in files:
            info = file.stat(follow_symlinks=False)
            if file.name != "draft.docx" or not stat.S_ISREG(info.st_mode) or info.st_mtime > cutoff:
                return False
    except FileNotFoundError:
        return False
    if apply:
        for file in files:
            file.unlink(missing_ok=True)
        with suppress(FileNotFoundError):
            directory.rmdir()
    return True


def delete_files(owner_id: int, document_id: int) -> None:
    folder = _child(root(), (_key(owner_id), "documents", _key(document_id)))
    if folder.exists():
        try:
            shutil.rmtree(folder)
        except OSError:
            raise DocumentError(503, "清理文档文件失败，请重试删除") from None
