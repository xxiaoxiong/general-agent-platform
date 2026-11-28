"""复合任务判定（确定性、零模型调用）。

任务书 §5.2：不是每条消息都生成 WorkPlan；简单问答、单文件读取、单工具操作
直接走原有执行路径，不能因为新增编排层多一次 Planner 模型调用。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

_RESEARCH = re.compile(
    r"调研|研究|竞品|行业(?:分析|研究|报告)|市场(?:分析|研究|调研|报告)|文献|资料(?:检索|收集|整理)"
    r"|趋势|态势|现状|核实|核验|查证|search|research|survey|benchmark",
    re.I,
)
_DATA = re.compile(
    r"数据分析|统计分析|分析|统计|指标|汇总|计算|测算|Excel|表格|数据表|数据集|CSV|TSV|数据"
    r"|报表|可视化|图表|回归|同比|环比|database|SQL|数据库查询|查询数据库"
    r"|query (?:the |a )?database",
    re.I,
)
_CODING = re.compile(
    r"代码|编程|脚本|程序|爬虫|接口实现|函数实现|重构|调试|报错|Bug|API 实现"
    r"|\b(?:coding|script|refactor|debug|implement)\b",
    re.I,
)
_BUSINESS = re.compile(
    r"发送?邮件|发邮件|Webhook|通知|工单|审批单|下单|外部系统|自动化流程"
    r"|\b(?:send (?:an? )?e-?mail|webhook|ticket)\b",
    re.I,
)
_DOCUMENT = re.compile(
    r"Word|DOCX|文档|报告|白皮书|方案书|说明书|手册|简报|调研报告|分析报告|PPT|PPTX|演示文稿"
    r"|\b(?:word|docx|report|whitepaper|proposal|deck)\b",
    re.I,
)
_SEQUENCE = re.compile(
    r"先.{0,80}(?:再|然后|接着|最后|之后)|首先.{0,120}(?:其次|然后|最后)"
    r"|第[一二三四五六七八九十\d]+步|分[两三四五]个阶段|多阶段"
    r"|(?:并|同时|然后|再)(?:据此|基于|根据)?.{0,20}(?:生成|整理|输出|写入|做成|导出)"
    r"|(?:基于|根据|结合).{0,40}(?:结果|结论|资料|数据).{0,20}(?:生成|整理|输出|写成|做成)",
    re.I,
)
_NUMBERED = re.compile(r"(?m)^\s*(?:\d{1,2}[.、)）]|[一二三四五六七八九十]+、)\s*\S")
_DOC_INTENTS = frozenset({
    "document_generate", "document_edit", "document_analyze", "profile_extract",
})
_EXCLUDED_INTENTS = frozenset({"software_project", "profile_extract"})


@dataclass
class ComplexityVerdict:
    composite: bool
    reason: str = ""
    domains: list[str] = field(default_factory=list)
    signals: list[str] = field(default_factory=list)


def _matched(pattern: re.Pattern[str], text: str) -> bool:
    return bool(pattern.search(text or ""))


def is_composite(
    *,
    message: str,
    decision: dict[str, Any] | None = None,
    execution_context: dict[str, Any] | None = None,
    goal_mode: bool = False,
) -> ComplexityVerdict:
    """判断当前请求是否需要跨执行引擎的 WorkPlan。

    判定依据是用户消息中的**能力域组合**与**先后关系**，而非模型自报。
    """
    text = str(message or "").strip()
    route = execution_context or {}
    decision = decision or {}
    intent = str(decision.get("intent") or route.get("intent") or "")
    task_type = str(route.get("task_type") or "")
    fmt = str(route.get("deliverable_format") or decision.get("deliverable_format") or "none")

    if not text:
        return ComplexityVerdict(False, "空请求")
    if goal_mode:
        # Goal 负责目标是否达成，Work Orchestrator 只负责跨引擎工作包拆分；
        # 由配置决定是否叠加，研究型目标由 Deep Research Profile 自行管理调查循环。
        from app.core.config import settings as _settings

        if not bool(getattr(_settings, "GOAL_ENABLE_WORK_ORCHESTRATOR", True)):
            return ComplexityVerdict(False, "目标模式未启用 WorkPlan（配置关闭）")
        intent_hint = str((execution_context or {}).get("intent") or intent or "")
        if intent_hint == "research":
            return ComplexityVerdict(False, "研究型目标由 Deep Research 管理调查循环")
    if route.get("completion_resume"):
        return ComplexityVerdict(False, "续做任务沿用原执行上下文")
    if route.get("team_mode") or intent == "software_project" or task_type == "software_project":
        return ComplexityVerdict(False, "软件项目/团队任务由专用执行引擎直接承接")
    if intent in _EXCLUDED_INTENTS and intent not in _DOC_INTENTS:
        return ComplexityVerdict(False, f"单一专业意图 {intent} 不需要跨引擎编排")

    domains: list[str] = []
    signals: list[str] = []
    if _matched(_RESEARCH, text):
        domains.append("research")
        signals.append("research")
    if _matched(_DATA, text):
        domains.append("data_analysis")
        signals.append("data_analysis")
    if _matched(_CODING, text):
        domains.append("coding")
        signals.append("coding")
    if _matched(_BUSINESS, text):
        domains.append("business_action")
        signals.append("business_action")
    has_document = _matched(_DOCUMENT, text) or fmt in {"docx", "pptx", "pdf"} or intent in _DOC_INTENTS
    if has_document:
        signals.append("document")

    non_doc = [domain for domain in domains if domain != "document"]
    sequenced = _matched(_SEQUENCE, text)
    numbered = len(_NUMBERED.findall(text)) >= 3

    # 1) 两个以上专业能力域：天然需要先后的结果传递。
    if len(set(non_doc)) >= 2:
        return ComplexityVerdict(
            True,
            "请求包含两个以上专业能力域，需要按依赖组织步骤",
            sorted(set(non_doc)), signals,
        )
    # 2) 一个专业域 + 文档交付：研究/数据/数据库/业务之后生成 Word/报告。
    if len(set(non_doc)) == 1 and has_document:
        if intent in _DOC_INTENTS or fmt in {"docx", "pptx", "pdf"} or sequenced:
            return ComplexityVerdict(
                True,
                f"{non_doc[0]} 结果需要进入文档交付，形成跨执行引擎步骤",
                sorted(set(non_doc)), signals,
            )
    # 3) 明确的多阶段叙述（先…再…最后）且至少一个专业域。
    if (sequenced or numbered) and non_doc:
        return ComplexityVerdict(
            True,
            "用户明确描述了多个有先后关系的工作阶段",
            sorted(set(non_doc)), signals,
        )
    # 4) 多个不同交付物（如报告 + 表格），且至少一个专业域。
    if len([item for item in (decision.get("deliverables") or []) if item]) >= 2 and non_doc:
        return ComplexityVerdict(
            True,
            "目标包含多个需要独立处理与验证的交付物",
            sorted(set(non_doc)), signals,
        )
    return ComplexityVerdict(False, "单一执行引擎即可完成，无需 Planner")
