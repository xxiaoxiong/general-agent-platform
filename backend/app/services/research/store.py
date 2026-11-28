"""Deep Research 持久化：研究计划、来源、证据与主张的服务端写入。"""
from __future__ import annotations

import hashlib
import re
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.core.config import settings
from app.core.database import AsyncSessionLocal
from app.models.research import (
    ResearchClaim, ResearchEvidence, ResearchPlan, ResearchSource,
)

_WHITESPACE = re.compile(r"\s+")
_SOURCE_TYPES = {
    "official", "docs", "source_code", "news", "blog", "paper", "other",
}
_QUESTION_STATUSES = {"pending", "researching", "answered", "blocked", "cancelled"}


def normalize_text(text: str) -> str:
    return _WHITESPACE.sub(" ", str(text or "")).strip()


def content_hash(text: str) -> str:
    return hashlib.sha256(str(text or "").encode("utf-8")).hexdigest()


def _next_id(prefix: str, existing: Iterable[str]) -> str:
    max_no = 0
    for item in existing:
        match = re.search(r"(\d+)$", str(item or ""))
        if match:
            max_no = max(max_no, int(match.group(1)))
    return f"{prefix}-{max_no + 1:03d}"


async def get_plan(
    *, run_id: Optional[str] = None, goal_id: Optional[str] = None,
    user_id: Optional[int] = None,
) -> Optional[ResearchPlan]:
    async with AsyncSessionLocal() as db:
        query = select(ResearchPlan)
        if goal_id:
            query = query.where(ResearchPlan.goal_id == goal_id)
        elif run_id:
            query = query.where(ResearchPlan.run_id == run_id)
        else:
            return None
        if user_id is not None:
            query = query.where(ResearchPlan.user_id == user_id)
        return (await db.execute(
            query.order_by(ResearchPlan.id.desc()).limit(1)
        )).scalar_one_or_none()


async def list_plan_questions(plan: ResearchPlan) -> List[Dict[str, Any]]:
    return [dict(item) for item in (plan.questions or []) if isinstance(item, dict)]


async def update_plan(
    *,
    research_id: str,
    user_id: int,
    topic: Optional[str] = None,
    scope: Optional[Dict[str, Any]] = None,
    questions: Optional[List[Any]] = None,
    report_outline: Optional[List[Any]] = None,
    status: Optional[str] = None,
    reason: str = "",
) -> Dict[str, Any]:
    """追加式更新研究计划：保留已有问题状态，新增项分配 RQ-xxx，revision 递增。"""
    async with AsyncSessionLocal() as db:
        plan = (await db.execute(
            select(ResearchPlan).where(
                ResearchPlan.research_id == research_id,
                ResearchPlan.user_id == user_id,
            ).with_for_update()
        )).scalar_one_or_none()
        if plan is None:
            raise LookupError("研究计划不存在")

        changes: List[Dict[str, Any]] = []
        current = [dict(item) for item in (plan.questions or []) if isinstance(item, dict)]
        if topic is not None and topic.strip() and topic.strip() != plan.topic:
            changes.append({"field": "topic", "before": plan.topic[:300], "after": topic.strip()[:300]})
            plan.topic = topic.strip()
        if scope is not None:
            merged_scope = {**dict(plan.scope or {}), **{
                key: value for key, value in scope.items()
                if key in {"time_range", "geography", "source_preferences", "excluded_sources"}
            }}
            changes.append({"field": "scope"})
            plan.scope = merged_scope
        if questions is not None:
            by_id = {str(item.get("id")): item for item in current}
            by_question = {normalize_text(item.get("question")): item for item in current}
            ids = list(by_id)
            for raw in questions:
                if isinstance(raw, str):
                    raw = {"question": raw}
                if not isinstance(raw, dict):
                    continue
                question = normalize_text(raw.get("question"))
                if not question:
                    continue
                question_id = str(raw.get("id") or "").strip()
                existing = by_id.get(question_id) if question_id else by_question.get(question)
                if existing is not None:
                    # 只允许更新状态/优先级/查询词，不覆盖已收集的证据引用
                    if raw.get("status") in _QUESTION_STATUSES:
                        existing["status"] = raw["status"]
                    if raw.get("priority") in {"high", "medium", "low"}:
                        existing["priority"] = raw["priority"]
                    if isinstance(raw.get("queries"), list):
                        existing["queries"] = [str(item)[:300] for item in raw["queries"][:8]]
                    if isinstance(raw.get("gaps"), list):
                        existing["gaps"] = [str(item)[:300] for item in raw["gaps"][:8]]
                    continue
                new_id = question_id or _next_id("RQ", ids)
                ids.append(new_id)
                current.append({
                    "id": new_id,
                    "question": question[:500],
                    "priority": raw.get("priority") if raw.get("priority") in {
                        "high", "medium", "low"} else "medium",
                    "status": raw.get("status") if raw.get("status") in _QUESTION_STATUSES else "pending",
                    "queries": [str(item)[:300] for item in (raw.get("queries") or [])][:8],
                    "claim_refs": [],
                    "gaps": [str(item)[:300] for item in (raw.get("gaps") or [])][:8],
                })
            changes.append({"field": "questions", "count": len(current)})
            plan.questions = current
        if report_outline is not None:
            plan.report_outline = [
                str(item)[:300] if not isinstance(item, dict) else {
                    str(key): str(value)[:500] for key, value in item.items()
                }
                for item in report_outline[:24]
            ]
            changes.append({"field": "report_outline"})
        if status and status in {"planning", "researching", "synthesizing", "completed", "blocked"}:
            plan.status = status
        if not changes:
            return _plan_dict(plan)
        plan.revision = int(plan.revision or 1) + 1
        plan.revisions = list(plan.revisions or []) + [{
            "revision": plan.revision, "at": datetime.utcnow().isoformat(),
            "reason": reason[:500] or "研究计划更新", "changes": changes,
        }]
        await db.commit()
        return _plan_dict(plan)


async def ensure_plan(
    *,
    run_id: str,
    goal_id: Optional[str],
    user_id: int,
    session_id: Optional[int],
    topic: str,
    questions: List[Dict[str, Any]],
    scope: Optional[Dict[str, Any]] = None,
    previous_run_id: Optional[str] = None,
) -> Dict[str, Any]:
    """创建或复用研究计划（续做时 research_id 不变，run_id 更新）。"""
    async with AsyncSessionLocal() as db:
        existing = None
        if goal_id:
            existing = (await db.execute(
                select(ResearchPlan).where(
                    ResearchPlan.goal_id == goal_id, ResearchPlan.user_id == user_id,
                ).order_by(ResearchPlan.id.desc()).limit(1)
            )).scalar_one_or_none()
        if existing is None and previous_run_id:
            existing = (await db.execute(
                select(ResearchPlan).where(
                    ResearchPlan.run_id == str(previous_run_id),
                    ResearchPlan.user_id == user_id,
                ).order_by(ResearchPlan.id.desc()).limit(1)
            )).scalar_one_or_none()
        if existing is not None:
            existing.run_id = run_id
            existing.session_id = session_id or existing.session_id
            existing.status = "researching" if existing.questions else "planning"
            await db.commit()
            return _plan_dict(existing)

        import uuid

        plan = ResearchPlan(
            research_id=uuid.uuid4().hex,
            goal_id=goal_id,
            run_id=run_id,
            session_id=session_id,
            user_id=user_id,
            revision=1,
            topic=topic[:1000],
            scope=scope or {
                "time_range": "", "geography": [],
                "source_preferences": [], "excluded_sources": [],
            },
            questions=questions,
            report_outline=[],
            status="researching" if questions else "planning",
            revisions=[{
                "revision": 1, "at": datetime.utcnow().isoformat(),
                "reason": "创建研究计划", "changes": [],
            }],
        )
        db.add(plan)
        try:
            await db.commit()
        except IntegrityError:
            await db.rollback()
            raise
        return _plan_dict(plan)


async def add_source(
    *,
    research_id: str,
    user_id: int,
    url: str,
    title: str,
    source_type: str = "other",
    author: Optional[str] = None,
    published_at: Optional[str] = None,
    http_status: Optional[int] = None,
    content: str = "",
    content_location: Optional[str] = None,
) -> Dict[str, Any]:
    """登记来源；只有真实读取到正文（content 非空）才标记 read。"""
    from urllib.parse import urlsplit

    normalized_url = str(url or "").strip()
    if not normalized_url.startswith(("http://", "https://")):
        raise ValueError("URL 必须以 http:// 或 https:// 开头")
    domain = urlsplit(normalized_url).hostname or ""
    excerpt = str(content or "")[:200_000]
    read = bool(excerpt.strip())
    async with AsyncSessionLocal() as db:
        plan = (await db.execute(
            select(ResearchPlan).where(
                ResearchPlan.research_id == research_id,
                ResearchPlan.user_id == user_id,
            )
        )).scalar_one_or_none()
        if plan is None:
            raise LookupError("研究计划不存在")
        rows = (await db.execute(
            select(ResearchSource).where(ResearchSource.research_id == research_id)
        )).scalars().all()
        existing = next((row for row in rows if row.url == normalized_url), None)
        if existing is not None:
            if read:
                existing.status = "read"
                existing.title = existing.title or title[:1000]
                existing.http_status = http_status
                existing.content_hash = content_hash(content)
                existing.content_excerpt = excerpt
                existing.content_location = content_location
                existing.retrieved_at = datetime.utcnow()
                existing.source_type = (
                    source_type if source_type in _SOURCE_TYPES else existing.source_type
                )
            await db.commit()
            return _source_dict(existing)
        max_sources = max(1, int(getattr(settings, "GOAL_RESEARCH_MAX_SOURCES", 60)))
        if len(rows) >= max_sources:
            raise ValueError(f"来源数量已达到上限 {max_sources}，请聚焦核心来源")
        row = ResearchSource(
            research_id=research_id,
            source_id=_next_id("SRC", [item.source_id for item in rows]),
            url=normalized_url,
            title=str(title or "")[:1000],
            source_type=source_type if source_type in _SOURCE_TYPES else "other",
            author=(str(author)[:200] if author else None),
            published_at=(str(published_at)[:64] if published_at else None),
            domain=domain[:255],
            http_status=http_status,
            content_hash=content_hash(content) if content else None,
            content_location=(str(content_location)[:500] if content_location else None),
            content_excerpt=excerpt or None,
            status="read" if read else "discovered",
        )
        db.add(row)
        await db.commit()
        return _source_dict(row)


async def add_evidence(
    *,
    research_id: str,
    user_id: int,
    source_id: str,
    quote: str,
    locator: str = "",
    question_id: str = "",
) -> Dict[str, Any]:
    """登记证据片段：必须来自已读取来源，且原文片段可在正文中定位。"""
    normalized_quote = normalize_text(quote)
    if len(normalized_quote) < 8:
        raise ValueError("证据片段过短，无法支持结论")
    async with AsyncSessionLocal() as db:
        plan = (await db.execute(
            select(ResearchPlan).where(
                ResearchPlan.research_id == research_id,
                ResearchPlan.user_id == user_id,
            )
        )).scalar_one_or_none()
        if plan is None:
            raise LookupError("研究计划不存在")
        source = (await db.execute(
            select(ResearchSource).where(
                ResearchSource.research_id == research_id,
                ResearchSource.source_id == source_id,
            )
        )).scalar_one_or_none()
        if source is None or source.status != "read":
            raise ValueError("来源尚未读取，不能直接作为证据；请先读取来源正文")
        excerpt = normalize_text(source.content_excerpt or "")
        if excerpt and normalized_quote not in excerpt:
            raise ValueError(
                "证据片段未在已读取的来源正文中定位到；请引用原文片段，或重新读取完整来源"
            )
        question_ids = {
            str(item.get("id")) for item in (plan.questions or [])
            if isinstance(item, dict) and item.get("status") != "cancelled"
        }
        if not question_id and len(question_ids) == 1:
            # 单问题研究自动归属，避免模型漏传 question_id 导致覆盖检查永远失败
            question_id = next(iter(question_ids))
        if question_id and question_id not in question_ids:
            raise ValueError(f"研究问题 {question_id} 不存在")
        rows = (await db.execute(
            select(ResearchEvidence).where(ResearchEvidence.research_id == research_id)
        )).scalars().all()
        max_items = max(1, int(getattr(settings, "GOAL_RESEARCH_MAX_EVIDENCE", 300)))
        if len(rows) >= max_items:
            raise ValueError(f"证据数量已达到上限 {max_items}")
        existing = next(
            (row for row in rows
             if row.source_id == source_id and normalize_text(row.quote) == normalized_quote),
            None,
        )
        if existing is not None:
            if question_id:
                existing.question_id = question_id
            existing.locator = str(locator)[:500] or existing.locator
            await db.commit()
            return _evidence_dict(existing)
        row = ResearchEvidence(
            research_id=research_id,
            evidence_id=_next_id("EV", [item.evidence_id for item in rows]),
            source_id=source_id,
            question_id=(question_id or None),
            quote=normalized_quote[:4000],
            locator=(str(locator)[:500] or None),
            status="extracted",
        )
        db.add(row)
        await db.commit()
        return _evidence_dict(row)


async def add_claim(
    *,
    research_id: str,
    user_id: int,
    statement: str,
    question_id: str = "",
    support_evidence_ids: Optional[List[str]] = None,
    contradict_evidence_ids: Optional[List[str]] = None,
) -> Dict[str, Any]:
    text = normalize_text(statement)
    if len(text) < 8:
        raise ValueError("主张内容过短")
    async with AsyncSessionLocal() as db:
        plan = (await db.execute(
            select(ResearchPlan).where(
                ResearchPlan.research_id == research_id,
                ResearchPlan.user_id == user_id,
            )
        )).scalar_one_or_none()
        if plan is None:
            raise LookupError("研究计划不存在")
        questions_active = [
            str(item.get("id")) for item in (plan.questions or [])
            if isinstance(item, dict) and item.get("status") != "cancelled" and item.get("id")
        ]
        if not question_id and len(questions_active) == 1:
            question_id = questions_active[0]
        if question_id and question_id not in questions_active:
            raise ValueError(f"研究问题 {question_id} 不存在")
        evidence_ids = {
            str(item.evidence_id) for item in (await db.execute(
                select(ResearchEvidence).where(ResearchEvidence.research_id == research_id)
            )).scalars().all()
        }
        support = [item for item in (support_evidence_ids or []) if item in evidence_ids]
        contradict = [item for item in (contradict_evidence_ids or []) if item in evidence_ids]
        if len(support) != len(set(support_evidence_ids or [])):
            raise ValueError("部分支持证据不存在，请先登记证据")
        if not support and not contradict:
            available = "、".join(sorted(evidence_ids)[:20]) or "（尚无证据，请先 research_add_evidence）"
            raise ValueError(
                "主张必须关联至少一条已登记证据后再核验；当前可用证据：" + available
            )
        rows = (await db.execute(
            select(ResearchClaim).where(ResearchClaim.research_id == research_id)
        )).scalars().all()
        existing = next(
            (row for row in rows
             if normalize_text(row.statement) == text
             and (row.question_id or "") == (question_id or "")),
            None,
        )
        if existing is not None:
            # 同一主张重试：合并证据引用，不重复创建
            existing.support_evidence = list(dict.fromkeys(
                [*(existing.support_evidence or []), *support]
            ))
            existing.contradict_evidence = list(dict.fromkeys(
                [*(existing.contradict_evidence or []), *contradict]
            ))
            await db.commit()
            return _claim_dict(existing)
        row = ResearchClaim(
            research_id=research_id,
            claim_id=_next_id("CLM", [item.claim_id for item in rows]),
            question_id=(question_id or None),
            statement=text[:4000],
            status="pending",
            support_evidence=list(dict.fromkeys(support)),
            contradict_evidence=list(dict.fromkeys(contradict)),
            verification={},
        )
        db.add(row)
        # 同步问题上的主张引用
        questions = []
        for item in (plan.questions or []):
            if not isinstance(item, dict):
                continue
            entry = dict(item)
            if question_id and entry.get("id") == question_id:
                entry["claim_refs"] = list(dict.fromkeys(
                    [*(entry.get("claim_refs") or []), row.claim_id]
                ))
            questions.append(entry)
        if questions:
            plan.questions = questions
        await db.commit()
        return _claim_dict(row)


async def get_claim(*, research_id: str, claim_id: str, user_id: int) -> Optional[ResearchClaim]:
    async with AsyncSessionLocal() as db:
        plan = (await db.execute(
            select(ResearchPlan).where(
                ResearchPlan.research_id == research_id, ResearchPlan.user_id == user_id,
            )
        )).scalar_one_or_none()
        if plan is None:
            return None
        return (await db.execute(
            select(ResearchClaim).where(
                ResearchClaim.research_id == research_id,
                ResearchClaim.claim_id == claim_id,
            )
        )).scalar_one_or_none()


async def save_claim_verification(
    *, research_id: str, claim_id: str, user_id: int,
    status: str, verification: Dict[str, Any],
) -> Dict[str, Any]:
    async with AsyncSessionLocal() as db:
        plan = (await db.execute(
            select(ResearchPlan).where(
                ResearchPlan.research_id == research_id, ResearchPlan.user_id == user_id,
            )
        )).scalar_one_or_none()
        if plan is None:
            raise LookupError("研究计划不存在")
        row = (await db.execute(
            select(ResearchClaim).where(
                ResearchClaim.research_id == research_id,
                ResearchClaim.claim_id == claim_id,
            )
        )).scalar_one_or_none()
        if row is None:
            raise LookupError("主张不存在")
        row.status = status
        row.verification = verification
        await db.commit()
        return _claim_dict(row)


async def snapshot(*, research_id: str, user_id: int) -> Dict[str, Any]:
    """研究状态快照：问题覆盖、来源读取率、证据与主张核验缺口。"""
    async with AsyncSessionLocal() as db:
        plan = (await db.execute(
            select(ResearchPlan).where(
                ResearchPlan.research_id == research_id, ResearchPlan.user_id == user_id,
            )
        )).scalar_one_or_none()
        if plan is None:
            return {}
        sources = (await db.execute(
            select(ResearchSource).where(ResearchSource.research_id == research_id)
        )).scalars().all()
        evidence = (await db.execute(
            select(ResearchEvidence).where(ResearchEvidence.research_id == research_id)
        )).scalars().all()
        claims = (await db.execute(
            select(ResearchClaim).where(ResearchClaim.research_id == research_id)
        )).scalars().all()

    evidence_by_question: Dict[str, List[str]] = {}
    for item in evidence:
        if item.question_id:
            evidence_by_question.setdefault(item.question_id, []).append(item.evidence_id)
    claims_by_question: Dict[str, List[ResearchClaim]] = {}
    for item in claims:
        if item.question_id:
            claims_by_question.setdefault(item.question_id, []).append(item)

    gaps: List[Dict[str, Any]] = []
    questions = []
    for item in (plan.questions or []):
        if not isinstance(item, dict):
            continue
        question_id = str(item.get("id") or "")
        entry = dict(item)
        entry["evidence_count"] = len(evidence_by_question.get(question_id, []))
        entry["claim_count"] = len(claims_by_question.get(question_id, []))
        questions.append(entry)
        if entry["status"] != "cancelled" and entry.get("priority") == "high":
            if not entry["evidence_count"]:
                gaps.append({"type": "missing_evidence", "question_id": question_id,
                             "detail": "高优先级问题尚无证据"})
            elif not entry["claim_count"]:
                gaps.append({"type": "missing_claim", "question_id": question_id,
                             "detail": "高优先级问题已有证据但尚无主张"})
    for claim in claims:
        if claim.status in {"insufficient", "pending"}:
            gaps.append({"type": "unverified_claim", "claim_id": claim.claim_id,
                         "detail": "主张证据不足或尚未核验"})
    read_sources = [item for item in sources if item.status == "read"]
    return {
        "research_id": plan.research_id,
        "goal_id": plan.goal_id,
        "topic": plan.topic,
        "status": plan.status,
        "revision": plan.revision,
        "scope": dict(plan.scope or {}),
        "outline": list(plan.report_outline or []),
        "questions": questions,
        "counts": {
            "questions": len(questions),
            "sources": len(sources),
            "sources_read": len(read_sources),
            "evidence": len(evidence),
            "claims": len(claims),
            "claims_supported": sum(1 for item in claims if item.status in {"supported", "verified"}),
            "claims_conflicting": sum(1 for item in claims if item.status == "conflicting"),
        },
        "gaps": gaps[:50],
    }


def _plan_dict(row: ResearchPlan) -> Dict[str, Any]:
    return {
        "research_id": row.research_id,
        "goal_id": row.goal_id,
        "run_id": row.run_id,
        "session_id": row.session_id,
        "revision": int(row.revision or 1),
        "topic": row.topic,
        "scope": dict(row.scope or {}),
        "questions": list(row.questions or []),
        "report_outline": list(row.report_outline or []),
        "status": row.status,
        "revisions": list(row.revisions or []),
    }


def _source_dict(row: ResearchSource) -> Dict[str, Any]:
    return {
        "source_id": row.source_id,
        "url": row.url,
        "title": row.title,
        "source_type": row.source_type,
        "author": row.author,
        "published_at": row.published_at,
        "domain": row.domain,
        "http_status": row.http_status,
        "content_hash": row.content_hash,
        "status": row.status,
        "retrieved_at": row.retrieved_at.isoformat() if row.retrieved_at else None,
    }


def _evidence_dict(row: ResearchEvidence) -> Dict[str, Any]:
    return {
        "evidence_id": row.evidence_id,
        "source_id": row.source_id,
        "question_id": row.question_id,
        "quote": row.quote,
        "locator": row.locator,
        "status": row.status,
    }


def _claim_dict(row: ResearchClaim) -> Dict[str, Any]:
    return {
        "claim_id": row.claim_id,
        "question_id": row.question_id,
        "statement": row.statement,
        "status": row.status,
        "support_evidence": list(row.support_evidence or []),
        "contradict_evidence": list(row.contradict_evidence or []),
        "verification": dict(row.verification or {}),
    }
