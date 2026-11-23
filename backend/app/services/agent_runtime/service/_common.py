"""AgentRuntimeService 共享基础：模块级辅助、审批异常、请求摘要（自 service.py 机械拆分，逻辑零改动）。"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import uuid
from datetime import datetime
from typing import Any, AsyncGenerator, Dict, List, Optional, Tuple

from langchain_core.messages import AIMessage, AIMessageChunk, ToolMessage
from langchain.agents.middleware.model_call_limit import ModelCallLimitExceededError
from langchain.agents.middleware.tool_call_limit import ToolCallLimitExceededError
from langgraph.errors import GraphRecursionError
from langgraph.types import Command
from sqlalchemy import select

from app.core.config import settings
from app.models.agent import AgentRun as AgentRunRow, AgentSession
from app.core.database import AsyncSessionLocal
from app.services.audit.capture import run_audit_origin
from app.observability.langfuse_tracing import (
    enrich_run_trace,
    flush_traces,
    make_callback_handler,
    platform_span,
    run_context,
    trace_url,
)

from app.services.agent_runtime.context import (
    ExecutionBudget,
    ModelUsageCallback,
    RuntimeContext,
    RunScope,
    messages_to_langchain,
)
from app.services.agent_runtime.events import (
    APPROVAL_REQUIRED,
    MODEL_DELTA,
    PLAN_UPDATED,
    RUN_CANCELLED,
    RUN_COMPLETED,
    RUN_FAILED,
    RUN_RECOVERING,
    RUN_RETRYING,
    RUN_STARTED,
    RUN_SUSPENDED,
    RUN_WAITING_APPROVAL,
    RUN_WAITING_INPUT,
    SUBAGENT_COMPLETED,
    SUBAGENT_STARTED,
    TOOL_COMPLETED,
    TOOL_FAILED,
    TOOL_STARTED,
    VERIFICATION_COMPLETED,
    VERIFICATION_STARTED,
    TERMINAL_EVENTS,
    EventBus,
    LegacyEventAdapter,
    RuntimeEvent,
    redact_secrets,
    redact_sensitive,
    wait_wakeup,
)
from app.services.agent_runtime.factory import build_agent
from app.services.agent_runtime.lifecycle import (
    STATUS_CANCELLED,
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_LIMIT_EXCEEDED,
    STATUS_NEEDS_RECONCILIATION,
    STATUS_PENDING,
    STATUS_RUNNING,
    STATUS_WAITING_APPROVAL,
    STATUS_WAITING_INPUT,
    TERMINAL_STATUSES,
    InvalidTransition,
    RunExecutionLease,
    RunHandle,
    RunRegistry,
)
from app.services.agent_runtime.model_adapter import ModelCapabilityError
from app.services.agent_runtime.persistence import CheckpointerFactory, make_run_config, make_thread_id
from app.services.agent_runtime.repositories import (
    EventRepository,
    IdempotencyConflict,
    OUTBOX_DELIVERED,
    PersistenceError,
    RunRepository,
    SessionOutboxRepository,
    SessionRepository,
    ToolExecutionRepository,
)
from app.services.agent_runtime.repositories.errors import IllegalTransition, RunLeaseLost, SessionBusy
from app.services.agent_runtime.telemetry import telemetry
from app.services.agent_runtime.tool_batches import ToolLoopStopped
from app.services.agent_runtime.tools.classification import (
    CHECKPOINT_MISSING,
    CHECKPOINT_OK,
    CHECKPOINT_UNREADABLE,
    recovery_decision,
)
from app.services.agent_runtime.tools.policy import BudgetExceededError, ToolPolicy, ToolPolicyError
from app.services.agent_runtime.task_router import DOCUMENT_GRAPH_TYPES
from app.services.agent_runtime.worker import (
    RECOVERY_CONTINUE,
    RECOVERY_FRESH,
    RECOVERY_NEEDS_RECONCILIATION,
    RECOVERY_WAITING_APPROVAL,
    RunJob,
    RunWorker,
)

logger = logging.getLogger("agent_runtime")

_STOP_MESSAGES = {
    "budget": "已达到本次执行上限，任务已暂停。",
    "no_progress": "连续尝试后进度未变化，任务已自动暂停。",
}

# Langfuse Trace 富化等后台 best-effort 任务的强引用（防 GC 提前回收）
_BG_TASKS: set = set()



# ── 审批决策异常（FR-04：冲突/非法决策显式拒绝，API 层可映射 HTTP 4xx） ──


class ApprovalInvalid(Exception):
    """审批决策非法（未知决策类型 / 未知审批动作 / 运行不存在）。"""
    code = "invalid"


class ApprovalConflict(Exception):
    """审批决策与既有决策冲突：同一动作不允许改判（F-11）。"""
    code = "conflict"


class ApprovalForbidden(Exception):
    """无权审批该运行。"""
    code = "forbidden"



def _field(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _extract_text(content: Any) -> str:
    """FR-09.5：模型内容文本提取规则——str 原样返回；结构化/多模态 block 列表
    按 text 块拼接（含纯字符串块），不静默丢弃可保留的文本。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: List[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and block.get("type", "text") == "text":
                parts.append(str(block.get("text") or ""))
        return "".join(parts)
    return ""


def _derive_title(content: str, *, max_len: int = 24) -> Optional[str]:
    """从用户消息派生会话标题：取首个非空行，去掉 Markdown 噪音，限长。

    之前直接用 content[:50]，导致会话列表里出现整段消息/多行碎片当标题。
    """
    if not content:
        return None
    for line in content.splitlines():
        text = line.strip().lstrip("#>*-`").strip()
        if text:
            return text if len(text) <= max_len else text[:max_len] + "…"
    return None


def _action_from_req(req: Dict[str, Any], interrupt_id: str, idx: int) -> Dict[str, Any]:
    """从 HITL interrupt 的 action_request 构造稳定审批动作（FR-06）。

    action_id 优先取 tool_call_id；否则由 interrupt_id + 序号 + 工具 + 参数
    摘要确定 —— 重启后从 Checkpoint 重提取时保持同一 ID，天然幂等。
    """
    raw_args = dict(req.get("args") or {})
    tool = str(req.get("name") or req.get("action") or req.get("tool") or "unknown")
    args_hash = hashlib.sha256(
        json.dumps(raw_args, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()
    action_id = str(req.get("tool_call_id") or hashlib.sha256(
        f"{interrupt_id}|{idx}|{tool}|{args_hash}".encode("utf-8")
    ).hexdigest()[:40])
    return {
        "action_id": action_id,
        "tool_call_id": req.get("tool_call_id"),
        "tool": tool,
        "args": redact_sensitive(raw_args),
        "args_hash": args_hash,
        "description": req.get("description", ""),
    }


def _synthetic_terminal_event(run_row: Any) -> RuntimeEvent:
    """无事件日志的旧运行：按状态合成等价终态事件（FR-07 回放兼容）。"""
    if run_row.status == STATUS_WAITING_APPROVAL:
        return RuntimeEvent(type=RUN_WAITING_APPROVAL, run_id=run_row.id, data={})
    if run_row.status == STATUS_WAITING_INPUT:
        return RuntimeEvent(type=RUN_WAITING_INPUT, run_id=run_row.id, data={})
    if run_row.status == STATUS_COMPLETED:
        return RuntimeEvent(
            type=RUN_COMPLETED,
            run_id=run_row.id,
            data={"final_text": run_row.final_text or "", "summary": run_row.summary},
        )
    if run_row.status == STATUS_CANCELLED:
        return RuntimeEvent(type=RUN_CANCELLED, run_id=run_row.id, data={
            "final_text": run_row.final_text, "summary": run_row.summary,
        } if run_row.summary else {})
    return RuntimeEvent(
        type=RUN_FAILED,
        run_id=run_row.id,
        data={"error": run_row.error or "运行失败", "status": run_row.status,
              "final_text": run_row.final_text, "summary": run_row.summary},
    )



class _RunLimitExceeded(Exception):
    pass


# 摘要纳入的 Agent 配置版本字段（执行语义相关，必要且稳定）；
# 易变的服务端临时字段（如检索到的记忆上下文、预加载表列表）与明文凭据不纳入
_AGENT_CONFIG_VERSION_KEYS = (
    "id",
    "model_config_id",
    "system_prompt",
    "max_iterations",
    "max_tokens",
    "temperature",
    "tools",
    "knowledge_base_id",
    "database_connection_id",
)


def compute_request_hash(
    *,
    endpoint: str,
    user_id: Optional[int],
    agent_id: Any,
    session_id: Optional[int],
    user_input: str,
    agent_config: Optional[Dict[str, Any]],
    task_request: Optional[Dict[str, Any]] = None,
) -> str:
    """规范化请求摘要（P1-01）。

    至少包含：user_id、agent_id、session_id、endpoint/操作类型、用户本次输入、
    必要的 Agent 配置版本。服务端易变临时字段（``_`` 前缀 / 记忆检索上下文 /
    预加载表列表）与明文凭据不纳入摘要。
    """
    canonical = json.dumps(
        {
            "endpoint": endpoint,
            "user_id": user_id,
            "agent_id": agent_id,
            "session_id": session_id,
            "user_input": user_input,
            "config_version": {
                k: (agent_config or {}).get(k) for k in _AGENT_CONFIG_VERSION_KEYS
            },
            **({"document_task": task_request} if task_request is not None else {}),
        },
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _last_user_input(messages: List[Dict[str, Any]]) -> str:
    """取用户本次输入（最后一条 user 消息内容），作为请求摘要的输入项。"""
    for m in reversed(messages or []):
        if isinstance(m, dict) and m.get("role") == "user":
            return str(m.get("content") or "")
    return str((messages[-1].get("content") or "")) if messages else ""


