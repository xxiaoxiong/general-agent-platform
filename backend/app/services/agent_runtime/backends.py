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
from typing import Any, Optional

from app.core.config import settings

RUN_ARTIFACT_FOLDER = ".run-artifacts"


def build_backend(user_id: int, agent_id: int, session_id: Optional[int], *, store: Optional[Any] = None):
    from deepagents.backends import CompositeBackend, FilesystemBackend, StateBackend
    from deepagents.backends.protocol import DeleteResult, EditResult, FileUploadResponse, WriteResult

    default_backend = StateBackend()

    base = getattr(settings, "AGENT_FILES_ROOT", "./data/agent_files")
    session_part = str(session_id) if session_id is not None else "ephemeral"
    root_dir = os.path.abspath(os.path.join(base, str(user_id), str(agent_id), session_part))
    os.makedirs(root_dir, exist_ok=True)

    class WorkspaceFiles(FilesystemBackend):
        def _protected(self, path: str) -> bool:
            try:
                root = self._resolve_path("/")
                relative = self._resolve_path(path).relative_to(root)
            except (ValueError, OSError, RuntimeError):
                return True
            return not relative.parts or relative.parts[0].casefold() in {"uploads", "team-projects", RUN_ARTIFACT_FOLDER}

        def write(self, file_path, content):
            if self._protected(file_path) or file_path.casefold().endswith(".docx"):
                return WriteResult(error="原始附件和历史交付由平台管理。请将新文件保存到可编辑区域，Word 请使用文档任务。")
            return super().write(file_path, content)

        def edit(self, file_path, old_string, new_string, replace_all=False):
            if self._protected(file_path) or file_path.casefold().endswith(".docx"):
                return EditResult(error="原始附件和历史交付由平台管理。请编辑工作文件，Word 请使用文档任务。")
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
