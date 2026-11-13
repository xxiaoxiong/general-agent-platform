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


def build_backend(user_id: int, agent_id: int, session_id: Optional[int], *, store: Optional[Any] = None):
    from deepagents.backends import CompositeBackend, FilesystemBackend, StateBackend

    default_backend = StateBackend()

    base = getattr(settings, "AGENT_FILES_ROOT", "./data/agent_files")
    session_part = str(session_id) if session_id is not None else "ephemeral"
    root_dir = os.path.abspath(os.path.join(base, str(user_id), str(agent_id), session_part))
    os.makedirs(root_dir, exist_ok=True)

    fs_backend = FilesystemBackend(
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
