"""AgentRuntimeService 包（自 service.py 机械拆分）。

对外导入路径保持 ``app.services.agent_runtime.service`` 不变；
``build_agent`` / ``settings`` / ``_service`` 留在包级属性供测试 monkeypatch。
"""
from __future__ import annotations

from ._common import *  # noqa: F401,F403 兼容既有模块级符号引用
from ._common import (
    _AGENT_CONFIG_VERSION_KEYS,
    _BG_TASKS,
    _STOP_MESSAGES,
    _RunLimitExceeded,
    _action_from_req,
    _derive_title,
    _extract_text,
    _field,
    _last_user_input,
    _synthetic_terminal_event,
    compute_request_hash,
)

from app.services.agent_runtime.factory import build_agent  # noqa: E402 包级工厂入口（测试 monkeypatch 目标）

from .core import AgentRuntimeService

_service: Optional[AgentRuntimeService] = None


def get_runtime_service() -> AgentRuntimeService:
    global _service
    if _service is None:
        _service = AgentRuntimeService()
    return _service


__all__ = [
    "AgentRuntimeService",
    "ApprovalConflict",
    "ApprovalForbidden",
    "ApprovalInvalid",
    "build_agent",
    "compute_request_hash",
    "get_runtime_service",
    "settings",
]
