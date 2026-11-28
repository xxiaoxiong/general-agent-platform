"""研究计划生成：确定性初始问题与范围（不调用模型）。

模型可以在执行中通过 ``research_update_plan`` 动态增删/调整问题；
初始计划只保证“有任何可跟踪问题”，不假装已经覆盖全部研究范围。
"""
from __future__ import annotations

import re
from typing import Any, Dict, List

_NUMBERED = re.compile(r"(?m)^\s*(?:\d{1,2}[.、)）]|[一二三四五六七八九十]+、)\s*(.+)$")


def _clean(text: str, limit: int = 240) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()[:limit]


def build_initial_questions(
    objective: str,
    decision: Dict[str, Any] | None = None,
    *,
    max_questions: int = 5,
) -> List[Dict[str, Any]]:
    decision = decision or {}
    candidates: List[tuple[str, str]] = []
    steps = [item for item in _NUMBERED.findall(objective or "") if _clean(item)]
    if steps:
        for step in steps[:3]:
            candidates.append((_clean(step), "high"))
    base = _clean((objective or "").split("\n", 1)[0], 300)
    if base:
        candidates.append((f"围绕目标形成可核验结论：{base}", "high"))
    for item in (decision.get("acceptance_criteria") or [])[:4]:
        text = _clean(item)
        if text:
            candidates.append((f"满足验收要求：{text}", "medium"))
    if not candidates:
        candidates.append(("明确研究主题、关键结论与证据来源", "high"))

    questions: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for index, (text, priority) in enumerate(candidates, 1):
        if text in seen:
            continue
        seen.add(text)
        questions.append({
            "id": f"RQ-{index:03d}",
            "question": text,
            "priority": priority,
            "status": "pending",
            "queries": [],
            "claim_refs": [],
            "gaps": [],
        })
        if len(questions) >= max_questions:
            break
    return questions


def build_scope(decision: Dict[str, Any] | None = None) -> Dict[str, Any]:
    decision = decision or {}
    return {
        "time_range": "",
        "geography": [],
        "source_preferences": [str(item)[:120] for item in (decision.get("constraints") or [])][:8],
        "excluded_sources": [],
    }
