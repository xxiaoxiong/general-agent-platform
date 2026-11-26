"""RunDecision v1：可审计、可校验的结构化语义决策。

设计约束（任务书 §3.1）：
- 不使用模型自报的 ``0.98`` 类数字作为可信度，改用 ``decision_status``；
- 禁止额外字段，字段长度与列表长度有上限，防止输出失控；
- ``route`` / ``intent`` 均为服务端枚举，模型不能返回任意模块名；
- ``context_refs`` 只能引用 Context Builder 提供的候选 ID。
"""
from __future__ import annotations

import re
from typing import Annotated, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

Intent = Literal[
    "general_chat",
    "research",
    "document_analyze",
    "document_generate",
    "document_edit",
    "profile_extract",
    "software_project",
    "data_analysis",
    "database_query",
    "coding",
    "business_action",
    "unknown",
]
Route = Literal[
    "deep_agent",
    "document_graph",
    "team_graph",
    "team_project_graph",
    "clarify",
]
Continuity = Literal[
    "new_task",
    "continue_existing",
    "modify_existing",
    "resume_existing",
    "unrelated",
]
DeliverableFormat = Literal[
    "docx", "md", "txt", "csv", "xlsx", "pdf", "pptx", "html", "json", "none",
]
Target = Literal["none", "preview", "latest_draft", "attachment"]
EditScope = Literal["content", "style", "both"]
ProjectScope = Literal["fullstack", "frontend", "backend"]

# 逐项先限制一个硬上限，再在字段校验中裁剪到 200 字，避免单条超长撑爆上下文
ShortText = Annotated[str, Field(max_length=1000)]


def _norm(value: str) -> str:
    return re.sub(r"[\s_\-]+", "", value.strip().lower())


_INTENT_ALIASES = {
    "chat": "general_chat", "qa": "general_chat", "generalchat": "general_chat",
    "docgenerate": "document_generate", "documentgeneration": "document_generate",
    "word": "document_generate", "doccreate": "document_generate",
    "documentgenerate": "document_generate",
    "docedit": "document_edit", "documentedit": "document_edit",
    "editdocument": "document_edit",
    "docanalyze": "document_analyze", "documentanalyze": "document_analyze",
    "documentanalysis": "document_analyze",
    "profile": "profile_extract", "profileextract": "profile_extract",
    "templateextract": "profile_extract",
    "software": "software_project", "softwareproject": "software_project",
    "softwareengineering": "software_project", "project": "software_project",
    "codingtask": "coding", "code": "coding",
    "db": "database_query", "sql": "database_query", "database": "database_query",
    "databasequery": "database_query", "dataanalysis": "data_analysis",
    "business": "business_action", "businessaction": "business_action",
}
_ROUTE_ALIASES = {
    "agent": "deep_agent", "deepagent": "deep_agent",
    "document": "document_graph", "docgraph": "document_graph",
    "documentgraph": "document_graph",
    "team": "team_graph", "teamgraph": "team_graph",
    "teamproject": "team_project_graph", "projectgraph": "team_project_graph",
    "teamprojectgraph": "team_project_graph",
    "ask": "clarify", "question": "clarify",
}
_CONTINUITY_ALIASES = {
    "continue": "continue_existing", "continueexisting": "continue_existing",
    "continuation": "continue_existing",
    "modify": "modify_existing", "modifyexisting": "modify_existing",
    "editexisting": "modify_existing",
    "resume": "resume_existing", "resumeexisting": "resume_existing",
    "new": "new_task", "newtask": "new_task", "fresh": "new_task",
    "switch": "unrelated", "newtopic": "unrelated", "unrelated": "unrelated",
}
_FORMAT_ALIASES = {
    "word": "docx", "doc": "docx", "docx": "docx",
    "markdown": "md", "md": "md", "text": "txt", "plaintext": "txt", "txt": "txt",
    "excel": "xlsx", "xlsx": "xlsx", "xls": "xlsx", "powerpoint": "pptx",
    "ppt": "pptx", "pptx": "pptx", "htm": "html", "html": "html",
    "json": "json", "csv": "csv", "pdf": "pdf", "none": "none",
}
_TARGET_ALIASES = {
    "none": "none", "preview": "preview", "currentdocument": "preview",
    "previewdocument": "preview", "latest": "latest_draft", "latestdraft": "latest_draft",
    "lastdraft": "latest_draft", "attachment": "attachment", "upload": "attachment",
    "uploadedfile": "attachment",
}
_EDIT_SCOPE_ALIASES = {
    "content": "content", "style": "style", "both": "both", "all": "both",
    "contentandstyle": "both", "styleonly": "style", "contentonly": "content",
}
_PROJECT_SCOPE_ALIASES = {
    "fullstack": "fullstack", "both": "fullstack", "all": "fullstack",
    "frontend": "frontend", "web": "frontend", "ui": "frontend",
    "backend": "backend", "server": "backend", "api": "backend",
}


class RunDecision(BaseModel):
    """一次 Auto Run 的语义决策（模型输出经服务端校验后才会执行）。"""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    schema_version: Literal["v1"] = "v1"
    intent: Intent
    route: Route
    continuity: Continuity = "new_task"

    goal: str = Field(default="", max_length=400)
    constraints: list[ShortText] = Field(default_factory=list, max_length=8)
    deliverables: list[ShortText] = Field(default_factory=list, max_length=8)
    capability_suggestions: list[ShortText] = Field(default_factory=list, max_length=8)
    context_refs: list[ShortText] = Field(default_factory=list, max_length=12)
    acceptance_criteria: list[ShortText] = Field(default_factory=list, max_length=8)

    project_scope: Optional[ProjectScope] = None
    decision_status: Literal["clear", "ambiguous"] = "clear"
    clarification_question: Optional[str] = Field(default=None, max_length=300)
    rationale: str = Field(default="", max_length=300)

    # 执行绑定信息（服务端仍会按资源归属重新校验）
    deliverable_format: DeliverableFormat = "none"
    target: Target = "none"
    edit_scope: Optional[EditScope] = None

    @field_validator("intent", mode="before")
    @classmethod
    def _normalize_intent(cls, value):
        return _INTENT_ALIASES.get(_norm(value), value) if isinstance(value, str) else value

    @field_validator("route", mode="before")
    @classmethod
    def _normalize_route(cls, value):
        return _ROUTE_ALIASES.get(_norm(value), value) if isinstance(value, str) else value

    @field_validator("continuity", mode="before")
    @classmethod
    def _normalize_continuity(cls, value):
        return _CONTINUITY_ALIASES.get(_norm(value), value) if isinstance(value, str) else value

    @field_validator("deliverable_format", mode="before")
    @classmethod
    def _normalize_format(cls, value):
        """模型常把格式写成 word/excel/ppt：归一化，非法值安全回落到 none。"""
        if value is None:
            return "none"
        return _FORMAT_ALIASES.get(_norm(str(value)), "none")

    @field_validator("target", mode="before")
    @classmethod
    def _normalize_target(cls, value):
        if value is None:
            return "none"
        return _TARGET_ALIASES.get(_norm(str(value)), "none")

    @field_validator("edit_scope", mode="before")
    @classmethod
    def _normalize_edit_scope(cls, value):
        if value in (None, ""):
            return None
        return _EDIT_SCOPE_ALIASES.get(_norm(str(value)))

    @field_validator("project_scope", mode="before")
    @classmethod
    def _normalize_project_scope(cls, value):
        """project_scope 是可选字段：非法值置空，不因小字段丢弃整次决策。"""
        if value in (None, ""):
            return None
        return _PROJECT_SCOPE_ALIASES.get(_norm(str(value)))

    @field_validator("constraints", "deliverables", "capability_suggestions",
                     "context_refs", "acceptance_criteria")
    @classmethod
    def _clip_items(cls, value: list[str]) -> list[str]:
        """逐项限长并去重，避免单条超长把上下文/事件撑爆。"""
        seen: list[str] = []
        for item in value:
            text = str(item or "").strip()[:200]
            if text and text not in seen:
                seen.append(text)
        return seen

    def snapshot(self) -> dict:
        """决策快照（写入 Run 持久化上下文；不含隐藏推理链）。"""
        return self.model_dump(mode="json")
