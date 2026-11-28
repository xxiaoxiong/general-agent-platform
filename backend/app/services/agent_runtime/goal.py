"""目标模式（/goal）执行协议：目标契约、计划强制与完成核对。

目标模式不只是解除预算限制：长程任务需要显式的可验证计划、持续执行、
交付前的逐项核对与诚实缺口说明。协议通过系统提示注入，完成侧约束由
CompletionMiddleware 的 goal_* 检查执行。
"""
from __future__ import annotations

from .context import RuntimeContext

# 最终回答必须包含的完成核对标题（模型侧格式要求，检查只看该小节是否存在）
GOAL_AUDIT_HEADING = "目标核对"
# 目标原文注入上限：超长请求保留开头足够让模型理解目标
_OBJECTIVE_LIMIT = 6000


def goal_objective(ctx: RuntimeContext) -> str:
    return str(ctx.agent_config.get("_goal_objective") or "").strip()[:_OBJECTIVE_LIMIT]


def goal_protocol_prompt(ctx: RuntimeContext) -> str:
    objective = goal_objective(ctx)
    lines = [
        "## 目标模式执行协议",
        "本次运行的唯一目的是完成用户目标并交付可核对的结果，不是给出建议、提纲或半成品。",
        "1. 开始执行前必须调用 write_todos 制定可验证计划：每一项都对应可核对的输出"
        "（交付物、文件、数据、来源或结论），不要写“继续研究/进一步分析”这类无法验收的待办。",
        "2. 持续执行直到计划项全部完成；每完成一项立即用 write_todos 标记 completed，"
        "发现新的缺口时追加待办。不要因为耗时、轮次多或已经给出部分内容而提前收尾。",
        "3. 交付前逐项核对：用户原始目标、计划项、验收要求逐条对照，核对证据"
        "（文件路径与内容、工具回执、检索来源链接、版本号等）。",
        f"4. 最终回答末尾必须包含「## {GOAL_AUDIT_HEADING}」表，逐条列出"
        "目标项 / 计划项 → 完成情况（已完成 / 未完成 / 受阻）→ 证据"
        "（文件路径、来源链接或工具结果）。",
        "5. 不得虚报：没有证据支撑的结论标注未完成或不确定；无法完成的项如实列出原因和缺口，"
        "禁止用“已完成 / 已交付”掩盖未完成的工作。",
        "6. 用户在执行过程中补充或纠正的要求优先于原计划，必须落实到后续执行与最终交付。",
        "证据充分后应停止继续检索并交付完整结果；目标未完成时不要以“暂无更多信息”结束。",
    ]
    if objective:
        lines.extend(["", "本次目标（用户原始请求）：", objective])
    if ctx.goal_contract:
        from app.services.goal_runtime.contracts import contract_prompt

        lines.append(contract_prompt(ctx.goal_contract))
    return "\n".join(lines)
