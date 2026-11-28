"""把校验后的 RunDecision 映射为 Runtime 兼容的 execution_context。

只做确定性映射与资源绑定提示；真正的资源归属/版本校验仍由
``documents.repository.resolve_task_resources`` 完成。
"""
from __future__ import annotations

from typing import Any

from .context import DecisionContext
from .validator import ValidatedDecision

_WORD_FORMATS = frozenset({"docx"})
_FILE_DELIVERABLES = frozenset({"md", "txt", "csv", "xlsx", "pdf", "pptx", "html", "json"})


def _target_document(validated: ValidatedDecision, context: DecisionContext) -> dict[str, Any] | None:
    decision = validated.decision
    for ref in validated.valid_refs:
        if ref == "preview_document" and context.preview_document:
            return context.preview_document
        if ref.startswith("artifact_"):
            artifact = context.artifact_by_ref(ref)
            if artifact:
                return artifact
    if decision.target == "preview":
        return context.preview_document
    if decision.target == "latest_draft":
        return context.artifacts[0] if context.artifacts else None
    # 模型没给引用但意图明确指向已有文档：取会话最近产物（仍由服务端校验归属）
    if decision.intent in {"document_edit", "document_analyze", "profile_extract"}:
        return context.artifacts[0] if context.artifacts else None
    return None


def _clear_document_binding(route: dict[str, Any]) -> None:
    route["document_id"] = None
    route["document_version"] = None
    route["style_edit"] = False
    route.pop("document_context", None)


def apply_decision(
    validated: ValidatedDecision,
    rules: dict[str, Any],
    *,
    context: DecisionContext,
) -> dict[str, Any]:
    """返回执行用 route；保留 ``task_mode`` 等确定性字段，覆盖语义字段。"""
    decision = validated.decision
    intent = validated.intent
    fmt = decision.deliverable_format
    explicit_fmt = rules.get("deliverable_format")
    if explicit_fmt and explicit_fmt != "docx" and intent in {
        "general_chat", "document_generate", "document_edit",
    }:
        # 用户本轮点名的格式是确定性事实，模型不得改写
        fmt = explicit_fmt
    if fmt == "none" and intent == "document_generate":
        fmt = "docx"
    target = _target_document(validated, context)

    route = dict(rules)
    route.pop("document_context", None)
    route.pop("confidence", None)
    route["intent"] = intent
    route["intent_source"] = "llm"
    route["intent_reason"] = decision.rationale
    route["decision_status"] = decision.decision_status
    route["deliverable_format"] = fmt
    route["decision_goal"] = decision.goal
    route["decision_constraints"] = list(decision.constraints)
    route["decision_acceptance"] = list(decision.acceptance_criteria)
    route["decision_context_refs"] = list(validated.valid_refs)
    route["decision_capabilities"] = list(validated.allowed_capabilities)
    route["requires_action"] = bool(decision.requires_action)

    if intent == "general_chat":
        _clear_document_binding(route)
        route.update(task_type="general_chat", reason="模型判定本轮为通用对话")
        if validated.clarification_question:
            route["clarify_question"] = validated.clarification_question
        elif fmt in _FILE_DELIVERABLES:
            route["deliverable"] = {"format": fmt}
        else:
            route.pop("deliverable", None)
        return route

    if intent == "document_generate":
        _clear_document_binding(route)
        if fmt in _WORD_FORMATS:
            route.update(task_type="document_generate",
                         reason="模型判定需要生成 Word 文档交付物")
            route.pop("deliverable", None)
        else:
            route.update(task_type="general_chat",
                         reason="模型判定需要交付非 Word 格式文件")
            if fmt in _FILE_DELIVERABLES:
                route["deliverable"] = {"format": fmt}
        return route

    if intent == "document_edit":
        route.update(task_type="document_edit", reason="模型判定需要修改已有文档")
        route["document_id"] = target.get("document_id") if target else None
        if target and target.get("version_no"):
            route["document_version"] = target["version_no"]
        route["style_edit"] = decision.edit_scope == "style"
        route["edit_scope"] = decision.edit_scope
        route.pop("deliverable", None)
        return route

    if intent == "document_analyze":
        route.update(task_type="document_analyze", reason="模型判定需要分析文档内容")
        route["document_id"] = target.get("document_id") if target else None
        if target and target.get("version_no"):
            route["document_version"] = target["version_no"]
        route["style_edit"] = False
        route.pop("deliverable", None)
        return route

    if intent == "profile_extract":
        route.update(task_type="profile_extract", reason="模型判定需要提炼可复用文档规范")
        route["document_id"] = target.get("document_id") if target else None
        route["style_edit"] = False
        route.pop("deliverable", None)
        return route

    if intent == "software_project":
        _clear_document_binding(route)
        if validated.route == "team_project_graph":
            route.update(task_type="software_project", team_mode=True,
                         project_scope=decision.project_scope,
                         reason="模型判定需要实际构建可交付的软件项目")
        else:
            route.update(task_type="general_chat", wants_software_project=True,
                         reason="模型判定为软件项目任务，但当前未开启团队协作")
        return route

    # research / data_analysis / database_query / coding / business_action / unknown
    _clear_document_binding(route)
    route.update(task_type="general_chat", reason=f"模型判定本轮任务类型为 {intent}")
    if fmt in _FILE_DELIVERABLES:
        route["deliverable"] = {"format": fmt}
    else:
        route.pop("deliverable", None)
    return route
