"""API v2 序列化助手（Assistants/Conversations/Runs 统一输出口径）。"""
from __future__ import annotations

from typing import Any, Dict, Optional

from app.models.agent import Agent, AgentSession, AgentVersion


def assistant_to_dict(a: Agent, version: Optional[AgentVersion] = None) -> Dict[str, Any]:
    """助手输出（§7 assistants 契约）：含能力包与默认助手标记。

    ``version`` 提供时附带当前发布版本信息（version_no / published_at）。
    """
    out: Dict[str, Any] = {
        "id": a.id,
        "name": a.name,
        "description": a.description or "",
        "icon": a.icon or "🤖",
        "system_prompt": a.system_prompt or "",
        "capability_ids": list(a.capability_ids or []),
        "tools": list(a.tools or []),
        "model_config_id": a.model_config_id,
        "knowledge_base_id": a.knowledge_base_id,
        "knowledge_base_ids": list(
            getattr(a, "knowledge_base_ids", None)
            or ([a.knowledge_base_id] if a.knowledge_base_id else [])
        ),
        "database_connection_id": a.database_connection_id,
        "max_iterations": a.max_iterations,
        "temperature": a.temperature,
        "max_tokens": a.max_tokens,
        "is_default": bool(a.is_default),
        "is_active": bool(a.is_active),
        "owner_id": a.owner_id,
        "created_at": a.created_at.isoformat() if a.created_at else None,
        "updated_at": a.updated_at.isoformat() if a.updated_at else None,
    }
    if version is not None:
        out["version"] = {
            "id": version.id,
            "version_no": version.version_no,
            "published_at": version.published_at.isoformat() if version.published_at else None,
        }
    return out


def conversation_to_dict(
    s: AgentSession, message_count: Optional[int] = None
) -> Dict[str, Any]:
    """会话输出。

    ``message_count`` 由列表查询用 SQL（JSON 函数）计算后传入，避免为计数
    把 messages 大列取回——列表 ORDER BY 时大 JSON 参与排序会撑爆
    MySQL sort buffer（错误 1038）；不传时从已加载实体读取。
    """
    return {
        "id": s.id,
        "assistant_id": s.agent_id,
        "title": s.title or "新对话",
        "is_pinned": s.pinned_at is not None,
        "pinned_at": s.pinned_at.isoformat() if s.pinned_at else None,
        "message_count": (
            len(s.messages or []) if message_count is None else int(message_count)
        ),
        "active_run_id": s.active_run_id,
        "project_id": getattr(s, "project_id", None),
        "created_at": s.created_at.isoformat() if s.created_at else None,
        "updated_at": s.updated_at.isoformat() if s.updated_at else None,
    }
