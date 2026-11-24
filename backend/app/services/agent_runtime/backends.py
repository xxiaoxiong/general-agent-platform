"""
虚拟文件后端：CompositeBackend 组合。

- 默认 StateBackend：会话内虚拟文件（随 checkpoint 保存，files 为 state 键）
- /workspace/ 前缀：FilesystemBackend 映射到 data/agent_files/{user}/{agent}/{session}
  virtual_mode=True，禁止指向宿主机根目录，max_file_size_mb 限制
- /memory/ 前缀：StoreBackend（P2-04 长期记忆）——跨 Session 持久化在
  LangGraph Store，namespace 按用户隔离（user_memory/{user_id}）
"""
from __future__ import annotations

import os
import re
import stat
from collections import OrderedDict
from pathlib import PurePosixPath
from threading import Lock
from typing import Any, Optional

from app.core.config import settings

RUN_ARTIFACT_FOLDER = ".run-artifacts"

# 宿主机/本地电脑风格路径（如 /Users/xxx、C:\Users\xxx）：平台无法写入用户电脑，
# 模型容易幻觉出这类路径（143 实录）。识别后引导改写到工作区。
_HOST_PATH = re.compile(r"^(?:/Users/|/home/|/root/|/Documents/|[A-Za-z]:[\\/]|\\\\)")


def _host_path_error(file_path: str) -> str | None:
    """宿主机风格路径：这类路径落到虚拟文件区，用户根本拿不到，直接拒绝并引导。"""
    if _HOST_PATH.match((file_path or "").strip()):
        return (
            f"路径 `{file_path}` 是用户电脑上的路径，平台无法写入用户的本地磁盘。"
            "请改写到工作区（如 `/workspace/报告.md`），交付文件保存到工作区后用户可直接下载。"
        )
    return None


def build_backend(user_id: int, agent_id: int, session_id: Optional[int], *, store: Optional[Any] = None):
    from deepagents.backends import CompositeBackend, FilesystemBackend, StateBackend
    from deepagents.backends.protocol import DeleteResult, EditResult, FileUploadResponse, WriteResult

    class GuardedState(StateBackend):
        """默认虚拟文件区：拦截宿主机风格路径（143 实录：/Users/... 落到这里，用户拿不到）。"""

        def write(self, file_path, content):
            if err := _host_path_error(file_path):
                return WriteResult(error=err)
            return super().write(file_path, content)

        def edit(self, file_path, old_string, new_string, replace_all=False):
            if err := _host_path_error(file_path):
                return EditResult(error=err)
            return super().edit(file_path, old_string, new_string, replace_all)

    default_backend = GuardedState()

    base = getattr(settings, "AGENT_FILES_ROOT", "./data/agent_files")
    session_part = str(session_id) if session_id is not None else "ephemeral"
    root_dir = os.path.abspath(os.path.join(base, str(user_id), str(agent_id), session_part))
    os.makedirs(root_dir, exist_ok=True)
    observed_conflicts: OrderedDict[PurePosixPath, tuple[PurePosixPath, int, int, int, int, int]] = OrderedDict()
    conflict_lock = Lock()

    class WorkspaceFiles(FilesystemBackend):
        def _protected(self, path: str) -> bool:
            try:
                root = self._resolve_path("/")
                relative = self._resolve_path(path).relative_to(root)
            except (ValueError, OSError, RuntimeError):
                return True
            return not relative.parts or relative.parts[0].casefold() in {"uploads", "team-projects", RUN_ARTIFACT_FOLDER}

        def _path_conflict(self, file_path: str) -> str | None:
            """只报告工作区中的虚拟路径，并保留阻塞文件供核对。"""
            relative = PurePosixPath("/" + file_path.lstrip("/")).parts[1:]
            root = self._resolve_path("/")
            path = root
            virtual = PurePosixPath("/workspace")
            target = virtual.joinpath(*relative)
            for part in relative[:-1]:
                path /= part
                try:
                    info = path.stat()
                except OSError:
                    # 不存在的父目录由原生 write 创建，其他系统错误仍交给原生操作报告。
                    break
                if not stat.S_ISREG(info.st_mode):
                    continue
                blocker = virtual.joinpath(*path.relative_to(root).parts)
                stamp = (blocker, info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
                with conflict_lock:
                    repeated = observed_conflicts.get(target) == stamp
                    observed_conflicts[target] = stamp
                    observed_conflicts.move_to_end(target)
                    if len(observed_conflicts) > 64:
                        observed_conflicts.popitem(last=False)
                recovery = ("该文件当前为 0 字节。请确认它是否为本任务误建的占位文件。"
                            if info.st_size == 0 and not path.is_symlink()
                            else "请用 read_file 核对阻塞文件。需保留内容时先另存并核对。")
                if repeated:
                    recovery += "阻塞路径未变化。无法安全清除时，请将此项保留为受阻，继续不依赖它的工作。"
                return (f"工作区路径冲突。父路径 `{blocker}` 已是文件，无法处理 `{target}`。原样重试仍会失败。"
                        + recovery + "确认可移除后使用 delete 清理阻塞路径，再写入原目标。write_file 会自动创建缺失的父目录。")
            try:
                is_directory = root.joinpath(*relative).is_dir()
            except OSError:
                return None
            if is_directory:
                return f"工作区路径冲突。`{target}` 已是目录。请指定该目录下的具体文件名，父目录无需单独创建。"
            return None

        def write(self, file_path, content):
            if err := _host_path_error(file_path):
                return WriteResult(error=err)
            if self._protected(file_path) or file_path.casefold().endswith(".docx"):
                return WriteResult(error="原始附件和历史交付由平台管理。请将新文件保存到可编辑区域，Word 请使用文档任务。")
            if conflict := self._path_conflict(file_path):
                return WriteResult(error=conflict)
            return super().write(file_path, content)

        def edit(self, file_path, old_string, new_string, replace_all=False):
            if err := _host_path_error(file_path):
                return EditResult(error=err)
            if self._protected(file_path) or file_path.casefold().endswith(".docx"):
                return EditResult(error="原始附件和历史交付由平台管理。请编辑工作文件，Word 请使用文档任务。")
            if conflict := self._path_conflict(file_path):
                return EditResult(error=conflict)
            return super().edit(file_path, old_string, new_string, replace_all)

        def delete(self, file_path):
            if self._protected(file_path):
                return DeleteResult(error="已上传的附件和历史交付由平台管理。")
            return super().delete(file_path)

        def upload_files(self, files):
            allowed = [item for item in files if not self._protected(item[0])]
            results = iter(super().upload_files(allowed))
            return [FileUploadResponse(path, error="permission_denied") if self._protected(path)
                    else next(results) for path, _ in files]

    fs_backend = WorkspaceFiles(
        root_dir=root_dir,
        virtual_mode=True,
        max_file_size_mb=getattr(settings, "AGENT_MAX_FILE_SIZE_MB", 10),
    )

    routes = {"/workspace/": fs_backend}
    if store is not None:
        # P2-04：长期记忆（AGENTS.md 等）按用户 namespace 持久化在 Store，
        # 跨 Session/Run 可见且用户间隔离
        from deepagents.backends import StoreBackend

        routes["/memory/"] = StoreBackend(
            namespace=lambda _rt, _u=user_id: ("user_memory", str(_u)),
            store=store,
        )

    return CompositeBackend(
        default=default_backend,
        routes=routes,
        artifacts_root="/",
    )


def workspace_root(user_id: int, agent_id: int, session_id: Optional[int]) -> str:
    base = getattr(settings, "AGENT_FILES_ROOT", "./data/agent_files")
    session_part = str(session_id) if session_id is not None else "ephemeral"
    return os.path.abspath(os.path.join(base, str(user_id), str(agent_id), session_part))
