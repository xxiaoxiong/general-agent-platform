"""DecisionValidator：把模型建议收敛到服务端允许的执行边界。

模型拥有语义决策权，但没有权限授予权与执行绕过权：
- route 必须与 intent 的受控映射一致；
- context_refs 必须是 Context Builder 提供的真实候选；
- capability_suggestions 不能扩大当前助手工具面；
- 缺目标文档/附件时降级到通用对话，不误启动专用工作流。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .context import DecisionContext
from .schemas import RunDecision

DOCUMENT_INTENTS = frozenset({"document_generate", "document_edit", "profile_extract"})
GENERAL_INTENTS = frozenset({
    "general_chat", "research", "data_analysis", "database_query",
    "coding", "business_action", "unknown",
})
CONTINUATION_VALUES = frozenset({
    "continue_existing", "modify_existing", "resume_existing",
})
_DEFAULT_CLARIFY = "为了更准确地开始，请补充最关键的一点：目标是什么、针对哪份文档、希望交付什么格式？"


@dataclass
class ValidatedDecision:
    decision: RunDecision
    intent: str
    route: str
    allow_upgrade: bool = False
    degraded: bool = False
    notes: list[str] = field(default_factory=list)
    clarification_question: str | None = None
    valid_refs: list[str] = field(default_factory=list)
    allowed_capabilities: list[str] = field(default_factory=list)


def expected_route(intent: str, *, auto_team: bool) -> str:
    """intent → 服务端允许的执行路线（不信任模型自报 route）。"""
    if intent in DOCUMENT_INTENTS:
        return "document_graph"
    if intent == "software_project":
        return "team_project_graph" if auto_team else "deep_agent"
    return "deep_agent"


def _has_word_attachment(context: DecisionContext) -> bool:
    return any(item["name"].casefold().endswith(".docx") for item in context.attachments)


def _has_document_target(context: DecisionContext) -> bool:
    return bool(context.artifacts or context.preview_document)


def validate_decision(
    decision: RunDecision,
    *,
    context: DecisionContext,
    auto_team: bool = False,
    user_format: str | None = None,
) -> ValidatedDecision:
    notes: list[str] = []
    intent = decision.intent
    status = decision.decision_status
    target = decision.target
    if target == "preview" and not context.preview_document:
        target = "none"
        notes.append("target=preview 但无预览文档，已清除")
    if target == "latest_draft" and not context.artifacts:
        target = "none"
        notes.append("target=latest_draft 但会话无文档产物，已清除")
    if target == "attachment" and not context.attachments:
        target = "none"
        notes.append("target=attachment 但本轮无附件，已清除")

    candidates = context.candidate_refs()
    valid_refs = [ref for ref in decision.context_refs if ref in candidates]
    dropped = [ref for ref in decision.context_refs if ref not in candidates]
    if dropped:
        notes.append(f"已丢弃不存在的 context_refs：{', '.join(dropped[:3])}")
    allowed_capabilities = [
        item for item in decision.capability_suggestions if item in set(context.capabilities)
    ]
    dropped_caps = [
        item for item in decision.capability_suggestions if item not in set(context.capabilities)
    ]
    if dropped_caps:
        notes.append(f"已拒绝未授权能力建议：{', '.join(dropped_caps[:3])}")

    has_docs = _has_document_target(context)
    has_word = _has_word_attachment(context)
    fmt = decision.deliverable_format or "none"
    # 用户本轮点名的格式是确定性事实：模型漏填或填错时以用户为准
    if user_format:
        fmt = user_format
    # 新任务/换话题时不得继承上一轮的文件格式（历史交付过 md/Word 不代表本轮还要文件）
    if (intent in GENERAL_INTENTS and intent != "unknown" and not user_format
            and fmt != "none" and decision.continuity in {"new_task", "unrelated"}):
        notes.append("本轮未点名文件格式，不继承历史交付格式")
        fmt = "none"
    if intent == "document_generate" and fmt == "none":
        # 模型明确要生成文档但未填格式：按平台唯一支持的 DOCX 处理
        fmt = "docx"

    # 1) 明确要求 Word 交付时，一律进入文档流程（只有文档图能产出 DOCX）。
    # 先做格式归一，再判断歧义：用户已点名 Word 时不因缺细节而澄清。
    if fmt == "docx" and intent in GENERAL_INTENTS and intent != "unknown":
        notes.append("交付格式为 Word，进入文档生成流程")
        intent = "document_generate"
    elif fmt == "docx" and intent == "document_analyze":
        notes.append("分析任务要求 Word 交付，进入文档生成流程")
        intent = "document_generate"

    # 2) 文档意图的资源安全边界（文档图只处理 DOCX）
    degraded = False
    if intent == "document_generate" and fmt != "docx":
        intent = "general_chat"
        degraded = True
        notes.append("非 Word 交付格式交由通用能力生成与交付")
    elif intent == "document_generate" and user_format != "docx":
        # 用户没有点名 Word、也没有既有文档延续目标时，不得默认生成 DOCX；
        # 继续/修改既有 Word 是明确资源操作，允许沿用文档流程。
        has_target = bool(context.artifacts or context.preview_document)
        if not (has_target and decision.continuity in CONTINUATION_VALUES):
            intent = "general_chat"
            degraded = True
            notes.append("用户未点名 Word 且没有既有文档目标，按通用能力交付正文")
    elif intent == "document_edit":
        if not has_docs and not has_word:
            intent = "general_chat"
            degraded = True
            notes.append("没有可编辑的 Word 文档，已降级为通用对话")
        elif fmt not in ("docx", "none"):
            intent = "general_chat"
            degraded = True
            notes.append("附件/交付格式不是 Word，已降级为通用能力处理")
    elif intent in {"document_analyze", "profile_extract"}:
        if not has_docs and not has_word:
            intent = "general_chat"
            degraded = True
            notes.append("没有可用的文档目标，已降级为通用对话")

    # 3) 关键歧义：先澄清，不启动专用工作流。
    # 例外：交付物/目标文档已由用户点名或会话资源确定时，不因缺细节打断。
    if status == "ambiguous":
        if intent == "document_generate":
            notes.append("新文档生成请求交付物明确，按生成流程执行，不因缺细节打断")
            status = "clear"
        elif intent == "document_analyze" and (has_docs or has_word):
            notes.append("分析目标已由会话文档/附件确定，按分析流程执行")
            status = "clear"
        elif (intent == "document_edit" and has_docs
              and target in {"preview", "latest_draft"}):
            notes.append("编辑目标已由会话文档确定，按编辑流程执行")
            status = "clear"
        else:
            question = (decision.clarification_question or "").strip() or _DEFAULT_CLARIFY
            clean = decision.model_copy(update={
                "context_refs": valid_refs, "capability_suggestions": allowed_capabilities,
                "target": target,
            })
            return ValidatedDecision(
                decision=clean, intent=intent, route="clarify",
                allow_upgrade=False, degraded=False, notes=notes,
                clarification_question=question,
                valid_refs=valid_refs, allowed_capabilities=allowed_capabilities,
            )

    route = expected_route(intent, auto_team=auto_team)
    if decision.route != route:
        notes.append(f"route 与校验后的执行边界不一致，已校正为 {route}")
    if decision.route in {"team_graph", "team_project_graph"} and not auto_team:
        notes.append("自动模式未开启团队协作，已回到通用 Agent")

    allow_upgrade = intent == "document_generate" or decision.continuity in CONTINUATION_VALUES
    clean = decision.model_copy(update={
        "intent": intent,
        "route": route,
        "target": target,
        "deliverable_format": fmt,
        "decision_status": "clear" if status == "ambiguous" else status,
        "context_refs": valid_refs,
        "capability_suggestions": allowed_capabilities,
    })
    return ValidatedDecision(
        decision=clean, intent=intent, route=route, allow_upgrade=allow_upgrade,
        degraded=degraded, notes=notes, valid_refs=valid_refs,
        allowed_capabilities=allowed_capabilities,
    )
