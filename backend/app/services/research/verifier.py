"""Evidence Verifier：主张与证据的确定性核验（任务书 §6.4）。

不返回笼统的“验证通过”：逐条检查证据是否存在、来源是否真实读取、
片段是否可在正文中定位，并区分 supported / conflicting / insufficient。
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional

from sqlalchemy import select

from app.core.database import AsyncSessionLocal
from app.models.research import (
    ResearchClaim, ResearchEvidence, ResearchPlan, ResearchSource,
)

from .store import normalize_text

_YEAR = re.compile(r"(20\d{2}|19\d{2})")


def _source_map(sources: Iterable[ResearchSource]) -> Dict[str, ResearchSource]:
    return {str(item.source_id): item for item in sources}


def _year_range(time_range: str) -> tuple[int, int] | None:
    years = [int(value) for value in _YEAR.findall(str(time_range or ""))]
    if not years:
        return None
    return min(years), max(years)


def _out_of_range(published_at: Optional[str], time_range: str) -> bool:
    window = _year_range(time_range)
    if window is None or not published_at:
        return False
    years = [int(value) for value in _YEAR.findall(str(published_at))]
    if not years:
        return False
    return any(year < window[0] or year > window[1] for year in years)


def _evidence_check(
    evidence: ResearchEvidence, sources: Dict[str, ResearchSource],
    time_range: str = "",
) -> Dict[str, Any]:
    source = sources.get(str(evidence.source_id))
    if source is None:
        return {"evidence_id": evidence.evidence_id, "valid": False,
                "reason": "来源不存在"}
    if source.status != "read":
        return {"evidence_id": evidence.evidence_id, "valid": False,
                "reason": "来源未真实读取"}
    quote = normalize_text(evidence.quote)
    excerpt = normalize_text(source.content_excerpt or "")
    if not quote:
        return {"evidence_id": evidence.evidence_id, "valid": False,
                "reason": "证据片段为空"}
    if excerpt and quote not in excerpt:
        return {"evidence_id": evidence.evidence_id, "valid": False,
                "reason": "片段未在来源正文中定位到"}
    if _out_of_range(source.published_at, time_range):
        return {
            "evidence_id": evidence.evidence_id, "valid": False,
            "reason": f"来源发布时间 {source.published_at} 不在研究时间范围 {time_range} 内",
        }
    return {
        "evidence_id": evidence.evidence_id,
        "source_id": evidence.source_id,
        "quote": quote[:300],
        "locator": evidence.locator,
        "valid": True,
        "reason": "",
    }


async def verify_claim(
    *, research_id: str, claim_id: str, user_id: int,
) -> Dict[str, Any]:
    """核验一条主张并持久化结构化结果。"""
    async with AsyncSessionLocal() as db:
        claim = (await db.execute(
            select(ResearchClaim).where(
                ResearchClaim.research_id == research_id,
                ResearchClaim.claim_id == claim_id,
            )
        )).scalar_one_or_none()
        if claim is None:
            raise LookupError("主张不存在")
        plan = (await db.execute(
            select(ResearchPlan).where(ResearchPlan.research_id == research_id)
        )).scalar_one_or_none()
        time_range = str((plan.scope or {}).get("time_range") or "") if plan else ""
        sources = _source_map((await db.execute(
            select(ResearchSource).where(ResearchSource.research_id == research_id)
        )).scalars().all())
        evidence_rows = (await db.execute(
            select(ResearchEvidence).where(ResearchEvidence.research_id == research_id)
        )).scalars().all()
        evidence_map = {str(item.evidence_id): item for item in evidence_rows}

    support = [
        _evidence_check(evidence_map[evidence_id], sources, time_range)
        for evidence_id in (claim.support_evidence or [])
        if evidence_id in evidence_map
    ]
    contradict = [
        _evidence_check(evidence_map[evidence_id], sources, time_range)
        for evidence_id in (claim.contradict_evidence or [])
        if evidence_id in evidence_map
    ]
    valid_support = [item for item in support if item["valid"]]
    valid_contradict = [item for item in contradict if item["valid"]]
    if not valid_support:
        status = "insufficient"
    elif valid_contradict:
        status = "conflicting"
    else:
        status = "supported"
    verification = {
        "verified_at": datetime.utcnow().isoformat(),
        "status": status,
        "support": support,
        "contradict": contradict,
        "notes": (
            "缺少可定位的有效支持证据；可用证据："
            + "、".join(sorted(evidence_map)[:20])
            if status == "insufficient"
            else "存在有效反驳证据，报告中必须披露冲突"
            if status == "conflicting"
            else "支持证据有效"
        ),
    }
    from .store import save_claim_verification

    saved = await save_claim_verification(
        research_id=research_id, claim_id=claim_id, user_id=user_id,
        status=status, verification=verification,
    )
    return saved


def _coverage_tokens(text: str) -> List[str]:
    """中文按二元词片段、英文按词生成覆盖 token（避免整句匹配过严）。"""
    raw_parts = re.split(r"[^0-9a-zA-Z\u4e00-\u9fff]+", str(text or ""))
    stop = {
        "交付", "完成", "用户", "要求", "必须", "需要", "最终", "结果", "结论",
        "内容", "正文", "提供", "包含", "进行", "相关", "以及", "并且", "the",
        "and", "with",
    }
    tokens: List[str] = []
    for part in raw_parts:
        value = part.casefold()
        if len(value) < 2:
            continue
        if re.search(r"[\u4e00-\u9fff]", value) and len(value) > 4:
            tokens.extend(value[index:index + 2] for index in range(len(value) - 1))
        else:
            tokens.append(value)
    return [token for token in dict.fromkeys(tokens) if token not in stop][:40]


async def research_checks(*, run_id: str, user_id: int) -> List[Dict[str, Any]]:
    """研究完成检查（供 CompletionMiddleware 与 Goal Evaluator 使用）。"""
    from .store import get_plan, snapshot

    plan = await get_plan(run_id=run_id, user_id=user_id)
    if plan is None:
        return [Check("research_plan_present", False, "研究计划尚未创建")]
    state = await snapshot(research_id=plan.research_id, user_id=user_id)
    checks: List[Dict[str, Any]] = []
    questions = [
        item for item in state.get("questions") or []
        if item.get("status") != "cancelled"
    ]
    checks.append(Check(
        "research_plan_present", bool(questions),
        "研究计划为空，请先拆解研究问题" if not questions else "",
    ))
    missing_evidence = [
        item for item in questions
        if item.get("priority") == "high" and int(item.get("evidence_count") or 0) == 0
    ]
    checks.append(Check(
        "research_evidence_present", not missing_evidence,
        "以下高优先级问题尚无证据：" + "、".join(
            str(item.get("id")) for item in missing_evidence[:6]
        ) if missing_evidence else "",
    ))
    claims = state.get("counts") or {}
    gaps = [
        item for item in state.get("gaps") or []
        if item.get("type") in {"unverified_claim", "missing_claim"}
    ]
    checks.append(Check(
        "research_claims_verified",
        bool(claims.get("claims")) and not gaps,
        (
            "尚无可核验主张，请为关键结论登记 Claim 并核验证据"
            if not claims.get("claims")
            else "以下主张尚未核验或证据不足：" + "、".join(
                str(item.get("claim_id") or item.get("question_id")) for item in gaps[:6]
            )
        ) if (not claims.get("claims") or gaps) else "",
    ))
    checks.append(await _objective_coverage_check(plan, questions))
    return checks


async def _objective_coverage_check(plan, questions: List[Dict[str, Any]]) -> Dict[str, Any]:
    """研究问题必须覆盖目标契约的必需验收项，不能生成计划就认为覆盖完整。"""
    from app.models.goal import GoalContract

    if not plan.goal_id:
        return Check("research_objective_coverage", True, "")
    async with AsyncSessionLocal() as db:
        contract = (await db.execute(
            select(GoalContract).where(GoalContract.goal_id == plan.goal_id)
            .order_by(GoalContract.id.desc()).limit(1)
        )).scalar_one_or_none()
    if contract is None:
        return Check("research_objective_coverage", True, "")
    criteria = [
        item for item in (contract.acceptance_criteria or [])
        if isinstance(item, dict) and item.get("required", True)
        # 只检查用户/决策提出的研究目标；文件交付、检索证据等任务义务
        # 由各自的确定性检查负责，不应强制变成研究问题。
        and item.get("source") in {"decision", "user_revision", "steering"}
    ]
    if not criteria:
        return Check("research_objective_coverage", True, "")
    question_text = " ".join(
        str(item.get("question") or "") for item in questions
    )
    missing = []
    for item in criteria:
        tokens = _coverage_tokens(str(item.get("description") or ""))
        if tokens and not any(token in question_text.casefold() for token in tokens):
            missing.append(str(item.get("description") or ""))
    return Check(
        "research_objective_coverage", not missing,
        "" if not missing else "以下目标验收项未被研究问题覆盖：" + "、".join(missing[:4]),
    )


def Check(check_id: str, passed: bool, detail: str = "") -> Dict[str, Any]:
    return {"id": check_id, "passed": bool(passed), "detail": detail}
