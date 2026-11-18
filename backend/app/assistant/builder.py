"""自然语言 Assistant Builder（P3-02，UX-02/UX-03）。

一段自然语言描述 → 可用助手草稿（名称/描述/角色指令/能力包/资源提示），
用户预览确认后一键创建；普通用户不需要填 System Prompt、Temperature、
Max Iterations、Tool List 等底层参数（UX-03）。

生成策略：
1. 规则提取（确定性、离线可用）：关键词 → 能力包映射 + 名称/指令模板；
2. 模型增强（可选）：配置了可用模型时可后续接入，规则结果永远作为
   fail-safe 基线，生成失败不阻塞草稿。

模型只能"建议"能力包，最终授权仍在创建/运行时经 Capability Registry
resolve_capabilities 校验（§4.2 fail-closed）。
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from app.assistant.capability import CAPABILITY_PACKS

# 关键词 → 能力包（规则映射；命中任意关键词即建议该能力包）
_KEYWORD_CAPABILITIES = [
    (r"搜索|检索|查资料|调研|研究|行业|新闻|竞品|市场|事实核验|来源", "web_research"),
    (r"文档|报告|PDF|Word|Excel|PPT|ppt|表格文件|读取文件|提取文本|生成文档", "doc_processing"),
    (r"数据分析|分析数据|数据表|SQL|数据库|查询数据|统计|图表|报表分析|指标", "data_analysis"),
    (r"知识库|知识管理|企业知识|内部文档|问答库", "knowledge"),
    (r"代码|编程|脚本|开发|程序|测试用例|重构|debug|报错", "coding"),
    (r"自动化|邮件|webhook|HTTP 接口|定时任务|审批流|外部系统", "business_automation"),
]

# 名称提取：「创建一个 XX 助手」「XX 助手」「帮我做一个 XX」
_NAME_PATTERNS = [
    re.compile(r"(?:创建|新建|做一个|做一款|打造|设计)(?:一个|一款|个)?(.{1,20}?)(?:助手|智能体|机器人|agent|Agent)"),
    re.compile(r"(.{1,20}?)(?:助手|智能体|机器人)"),
]


def infer_capability_ids(description: str) -> List[str]:
    """从描述推断能力包（general 恒为基础包）。"""
    ids: List[str] = ["general"]
    for pattern, cid in _KEYWORD_CAPABILITIES:
        if re.search(pattern, description) and cid in CAPABILITY_PACKS:
            if cid not in ids:
                ids.append(cid)
    return ids


def infer_name(description: str, explicit: Optional[str] = None) -> str:
    if explicit:
        return explicit.strip()[:50]
    for pat in _NAME_PATTERNS:
        m = pat.search(description)
        if m:
            name = m.group(1).strip("的 ，,、")
            if 2 <= len(name) <= 30:
                return name
    # 兜底：取描述前段
    head = description.strip().split("，")[0].split(",")[0]
    return (head[:20] or "新助手")


def _capability_names(ids: List[str]) -> str:
    return "、".join(CAPABILITY_PACKS[c].name for c in ids if c in CAPABILITY_PACKS)


def _resource_hints(ids: List[str]) -> List[Dict[str, str]]:
    hints = []
    for cid in ids:
        cap = CAPABILITY_PACKS.get(cid)
        if cap is None:
            continue
        for res in cap.required_resources:
            hints.append({"capability": cap.name, "resource": res})
    return hints


def build_draft(description: str, name: Optional[str] = None) -> Dict[str, Any]:
    """自然语言 → 助手草稿（不落库；用户确认后经 POST /assistants 创建）。"""
    description = (description or "").strip()
    if not description:
        raise ValueError("描述不能为空")

    capability_ids = infer_capability_ids(description)
    draft_name = infer_name(description, name)

    # 角色指令模板：能力包名称 + 用户描述 + 行为约束
    system_prompt = (
        f"你是「{draft_name}」，一个专用智能助手。\n"
        f"职责：{description}\n"
        f"可用能力：{_capability_names(capability_ids)}。\n"
        "要求：直接、准确地完成任务；需要时使用工具获取真实数据；"
        "不确定时明确说明，不编造事实；输出结构清晰、结论先行。"
    )

    risk_notes: List[str] = []
    for cid in capability_ids:
        cap = CAPABILITY_PACKS.get(cid)
        if cap and cap.policy_note:
            risk_notes.append(f"{cap.name}：{cap.policy_note}")

    return {
        "name": draft_name,
        "description": description[:200],
        "system_prompt": system_prompt,
        "capability_ids": capability_ids,
        "tools": [],
        "resource_hints": _resource_hints(capability_ids),
        "risk_notes": risk_notes,
        "requires_confirmation": True,
    }
