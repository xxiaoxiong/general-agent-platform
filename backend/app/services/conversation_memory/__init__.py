"""Conversation Memory：历史会话索引、自动检索、Agent 工具与权限校验。

阶段一（P0）：历史会话检索 MVP；阶段二（P1）长期记忆在此基础上扩展。
"""
from .context_builder import build_history_context
from .indexer import (
    current_index_version, delete_session_index, index_message, index_session,
    mark_session_deleted, reindex_user,
)
from .planner import plan_retrieval, parse_plan
from .repository import (
    enqueue_delete_job, enqueue_index_job, enqueue_reindex_job, load_settings,
    memory_enabled_for_run, update_settings,
)
from .retrieval import ConversationMemoryService, HistoryRetrievalResult, HistorySource
from .service import PreparedHistory, prepare_history_context
from .tools import MEMORY_TOOLS, TOOLS as MEMORY_TOOL_SPECS
from .worker import ConversationIndexWorker, get_conversation_index_worker

__all__ = [
    "build_history_context",
    "current_index_version",
    "delete_session_index",
    "index_message",
    "index_session",
    "mark_session_deleted",
    "reindex_user",
    "plan_retrieval",
    "parse_plan",
    "enqueue_delete_job",
    "enqueue_index_job",
    "enqueue_reindex_job",
    "load_settings",
    "memory_enabled_for_run",
    "update_settings",
    "ConversationMemoryService",
    "HistoryRetrievalResult",
    "HistorySource",
    "PreparedHistory",
    "prepare_history_context",
    "MEMORY_TOOLS",
    "MEMORY_TOOL_SPECS",
    "ConversationIndexWorker",
    "get_conversation_index_worker",
]
