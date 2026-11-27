"""API v2 Pydantic Schema（任务书 §7 契约）。"""
from __future__ import annotations

from typing import Annotated, Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StringConstraints, model_validator


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
    knowledge_base_ids: List[int] = Field(default_factory=list)
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
    knowledge_base_ids: Optional[List[int]] = None
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
    # 项目归属（Workspace Sandbox）：缺省由服务端按 SANDBOX_ENABLED 决定是否挂接默认项目
    project_id: Optional[int] = None


class ConversationProjectPatch(BaseModel):
    """切换会话所属项目（Workspace Sandbox）；活动 Run 期间禁止切换。"""

    project_id: Optional[int] = None


class ConversationPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: Annotated[str, StringConstraints(strict=True, strip_whitespace=True, min_length=1, max_length=200)] | None = None
    is_pinned: StrictBool | None = None

    @model_validator(mode="after")
    def validate_changes(self):
        if not self.model_fields_set or any(getattr(self, field) is None for field in self.model_fields_set):
            raise ValueError("请提供有效的会话标题或置顶状态")
        return self


# ── Runs ──────────────────────────────────────────────────────────────────────

class RunCreate(BaseModel):
    message: str
    conversation_id: Optional[int] = None  # 缺省自动创建会话
    assistant_id: Optional[int] = None     # 缺省 = 会话绑定的助手 / 默认助手
    use_memory: bool = True
    # 权限模块（审批模式）：manual=高危工具执行前人工审批；auto=自动放行不再询问
    approval_mode: str = "manual"
    # 联网检索开关：true=强制保留网络检索工具；false=本次 Run 禁用（收窄）；
    # None=跟随助手能力包默认。随 Run 创建写入 agent_config 快照，续跑保持原值
    web_enabled: Optional[bool] = None
    # 历史会话检索开关：false=本次 Run 禁用自动预取与历史工具；None=跟随用户设置
    history_enabled: Optional[bool] = None
    # 会话内手动选模型：仅本次 Run 覆盖助手默认模型（None=助手默认）
    model_config_id: Optional[int] = None
    # 每次运行须由调用方显式开启团队，省略时继续使用现有单助手路线。
    team_mode: StrictBool = False
    # 目标模式（/goal）：不限模型调用/工具调用/执行时长，并允许执行中补充要求。
    # 随 Run 创建写入 agent_config 快照，崩溃恢复/断点续跑后保持原值。
    goal_mode: StrictBool = False
    # 会话附件（uploads/ 下的相对路径），随用户消息注入读取注记
    attachments: List[Dict[str, Any]] = Field(default_factory=list)
    task_mode: Literal[
        "auto", "chat", "document_analyze", "profile_extract",
        "document_generate", "document_edit",
    ] = "auto"
    document_profile_id: Optional[int] = Field(None, gt=0)
    document_id: Optional[int] = Field(None, gt=0)
    # 右侧预览文档：仅作为意图分诊的上下文提示，不强制绑定为本轮编辑目标
    preview_document_id: Optional[int] = Field(None, gt=0)
    preview_document_version_no: Optional[int] = Field(None, gt=0)
    task_options: Dict[str, Any] = Field(default_factory=dict)


class ProjectRepairRequest(BaseModel):
    model_config_id: Optional[int] = Field(None, gt=0)


class ApprovalDecisionIn(BaseModel):
    type: str = "approve"                    # approve / reject
    message: str = ""
    action_id: Optional[str] = None          # 稳定审批动作 ID（FR-06）


class RunResumeRequest(BaseModel):
    decisions: List[ApprovalDecisionIn] = Field(default_factory=list)


class RunInputSubmission(BaseModel):
    request_id: str = Field(min_length=1, max_length=64)
    values: Dict[str, Any]


class RunCancelRequest(BaseModel):
    reason: str = ""


class RunSteerRequest(BaseModel):
    """执行中的补充要求（任意运行中的对话均可提交，不新起 Run）。"""
    message: str = Field(min_length=1, max_length=8000)


# ── Workspace ─────────────────────────────────────────────────────────────────

class BootstrapResponse(BaseModel):
    user: Dict[str, Any]
    default_assistant: Dict[str, Any]
    capabilities: List[Dict[str, Any]]
    workspace: Dict[str, Any]
