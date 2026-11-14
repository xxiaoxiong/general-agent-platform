"""API v2 Pydantic Schema（任务书 §7 契约）。"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


# ── Assistants ────────────────────────────────────────────────────────────────

class AssistantCreate(BaseModel):
    name: str
    description: str = ""
    icon: str = "🤖"
    system_prompt: str = ""
    capability_ids: List[str] = Field(default_factory=list)
    tools: List[Any] = Field(default_factory=list)  # [{"name","enabled"}] 显式收窄
    model_config_id: Optional[int] = None
    knowledge_base_id: Optional[int] = None
    database_connection_id: Optional[int] = None
    max_iterations: int = 10
    temperature: str = "0.7"
    max_tokens: int = 4096


class AssistantPatch(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    icon: Optional[str] = None
    system_prompt: Optional[str] = None
    capability_ids: Optional[List[str]] = None
    tools: Optional[List[Any]] = None
    model_config_id: Optional[int] = None
    knowledge_base_id: Optional[int] = None
    database_connection_id: Optional[int] = None
    max_iterations: Optional[int] = None
    temperature: Optional[str] = None
    max_tokens: Optional[int] = None


class AssistantDraftRequest(BaseModel):
    """自然语言 Builder：一段描述 → 助手草稿（UX-02）。"""
    description: str
    name: Optional[str] = None  # 可选显式名称，缺省由 Builder 提取


# ── Conversations ─────────────────────────────────────────────────────────────

class ConversationCreate(BaseModel):
    assistant_id: Optional[int] = None  # 缺省 = 默认助手
    title: str = "新对话"


# ── Runs ──────────────────────────────────────────────────────────────────────

class RunCreate(BaseModel):
    message: str
    conversation_id: Optional[int] = None  # 缺省自动创建会话
    assistant_id: Optional[int] = None     # 缺省 = 会话绑定的助手 / 默认助手
    use_memory: bool = True


class ApprovalDecisionIn(BaseModel):
    type: str = "approve"                    # approve / reject
    message: str = ""
    action_id: Optional[str] = None          # 稳定审批动作 ID（FR-06）


class RunResumeRequest(BaseModel):
    decisions: List[ApprovalDecisionIn] = Field(default_factory=list)


class RunCancelRequest(BaseModel):
    reason: str = ""


# ── Workspace ─────────────────────────────────────────────────────────────────

class BootstrapResponse(BaseModel):
    user: Dict[str, Any]
    default_assistant: Dict[str, Any]
    capabilities: List[Dict[str, Any]]
    workspace: Dict[str, Any]
