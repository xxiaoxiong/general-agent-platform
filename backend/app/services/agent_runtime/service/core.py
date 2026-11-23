"""AgentRuntimeService 组装类（自 service.py 机械拆分，逻辑零改动）。"""
from __future__ import annotations

from ._common import *  # noqa: F401,F403 机械拆分共享依赖
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

from .approval import ApprovalMixin
from .config import ConfigMixin
from .delivery import DeliveryMixin
from .lifecycle import LifecycleMixin
from .stream import StreamMixin


class AgentRuntimeService(LifecycleMixin, StreamMixin, ApprovalMixin, DeliveryMixin, ConfigMixin):
    """统一 Agent 执行服务（单例使用）。"""

    def __init__(self) -> None:
        self.registry = RunRegistry.instance()
        self.worker: Optional[RunWorker] = RunWorker(self)
