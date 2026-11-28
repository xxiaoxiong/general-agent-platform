"""Goal / Deep Research API（/api/v2/goals）。

目标契约与目标是服务端持久化事实：本模块只提供只读查询与用户发起的修订，
不允许客户端写入验收状态、进度或证据核验结论。
"""
from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.core.database import get_db
from app.models.goal import GoalContract, GoalIteration
from app.models.research import (
    ResearchClaim, ResearchEvidence, ResearchPlan, ResearchSource,
)
from app.models.user import User
from app.services.goal_runtime.contracts import contract_to_dict, revise_goal_contract

router = APIRouter(prefix="/goals", tags=["goals-v2"])

_MAX_ITERATIONS = 50
_MAX_RESEARCH_ROWS = 200


class GoalRevisionRequest(BaseModel):
    objective: Optional[str] = Field(default=None, max_length=20000)
    acceptance_criteria: Optional[list[Any]] = None
    note: str = Field(default="", max_length=500)


class ResearchRevisionRequest(BaseModel):
    topic: Optional[str] = Field(default=None, max_length=1000)
    scope: Optional[dict[str, Any]] = None
    questions: Optional[list[Any]] = None
    report_outline: Optional[list[Any]] = None
    reason: str = Field(default="", max_length=500)


def _iteration_payload(row: GoalIteration) -> dict[str, Any]:
    return {
        "iteration_no": row.iteration_no,
        "run_id": row.run_id,
        "status": row.status,
        "executor": row.executor,
        "plan": list(row.plan or []),
        "result": dict(row.result or {}),
        "evidence_refs": list(row.evidence_refs or []),
        "stall_signature": row.stall_signature,
        "started_at": row.started_at.isoformat() if row.started_at else None,
        "finished_at": row.finished_at.isoformat() if row.finished_at else None,
    }


async def _owned_contract(
    db: AsyncSession, *, goal_id: Optional[str] = None,
    run_id: Optional[str] = None, user_id: int,
) -> GoalContract:
    query = select(GoalContract).where(GoalContract.user_id == user_id)
    query = query.where(GoalContract.goal_id == goal_id) if goal_id else query.where(
        GoalContract.run_id == run_id
    )
    row = (await db.execute(
        query.order_by(GoalContract.id.desc()).limit(1)
    )).scalar_one_or_none()
    if row is None:
        raise HTTPException(404, "目标不存在")
    return row


async def _goal_detail(db: AsyncSession, contract: GoalContract) -> dict[str, Any]:
    iterations = (await db.execute(
        select(GoalIteration).where(GoalIteration.goal_id == contract.goal_id)
        .order_by(GoalIteration.iteration_no.desc()).limit(_MAX_ITERATIONS)
    )).scalars().all()
    payload = contract_to_dict(contract)
    payload["iterations_detail"] = [_iteration_payload(row) for row in iterations]
    return payload


@router.get("")
async def list_goals(
    session_id: Optional[int] = Query(default=None),
    limit: int = Query(default=20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    query = select(GoalContract).where(GoalContract.user_id == current_user.id)
    if session_id is not None:
        query = query.where(GoalContract.session_id == session_id)
    rows = (await db.execute(
        query.order_by(GoalContract.id.desc()).limit(limit)
    )).scalars().all()
    return {"items": [contract_to_dict(row) for row in rows]}


@router.get("/by-run/{run_id}")
async def goal_by_run(
    run_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    contract = await _owned_contract(db, run_id=run_id, user_id=current_user.id)
    return await _goal_detail(db, contract)


@router.get("/{goal_id}")
async def goal_detail(
    goal_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    contract = await _owned_contract(db, goal_id=goal_id, user_id=current_user.id)
    return await _goal_detail(db, contract)


@router.post("/{goal_id}/revision")
async def revise_goal(
    goal_id: str,
    body: GoalRevisionRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """用户修改目标或验收条件：生成新 revision，并把受影响项标记为待复核。"""
    contract = await _owned_contract(db, goal_id=goal_id, user_id=current_user.id)
    if contract.status == "superseded":
        raise HTTPException(409, "目标已被新目标取代，不能继续修改")
    updated = await revise_goal_contract(
        goal_id=goal_id,
        user_id=current_user.id,
        objective=body.objective,
        acceptance_criteria=body.acceptance_criteria,
        source="api",
        note=body.note or "用户修改目标",
    )
    if updated is None:
        raise HTTPException(404, "目标不存在")
    return updated


@router.get("/{goal_id}/research")
async def goal_research(
    goal_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    await _owned_contract(db, goal_id=goal_id, user_id=current_user.id)
    plan = (await db.execute(
        select(ResearchPlan).where(
            ResearchPlan.goal_id == goal_id, ResearchPlan.user_id == current_user.id,
        ).order_by(ResearchPlan.id.desc()).limit(1)
    )).scalar_one_or_none()
    if plan is None:
        return {"plan": None, "sources": [], "evidence": [], "claims": []}

    sources = (await db.execute(
        select(ResearchSource).where(ResearchSource.research_id == plan.research_id)
        .order_by(ResearchSource.id).limit(_MAX_RESEARCH_ROWS)
    )).scalars().all()
    evidence = (await db.execute(
        select(ResearchEvidence).where(ResearchEvidence.research_id == plan.research_id)
        .order_by(ResearchEvidence.id).limit(_MAX_RESEARCH_ROWS)
    )).scalars().all()
    claims = (await db.execute(
        select(ResearchClaim).where(ResearchClaim.research_id == plan.research_id)
        .order_by(ResearchClaim.id).limit(_MAX_RESEARCH_ROWS)
    )).scalars().all()
    return {
        "plan": {
            "research_id": plan.research_id,
            "goal_id": plan.goal_id,
            "run_id": plan.run_id,
            "revision": plan.revision,
            "topic": plan.topic,
            "scope": dict(plan.scope or {}),
            "questions": list(plan.questions or []),
            "report_outline": list(plan.report_outline or []),
            "status": plan.status,
            "revisions": list(plan.revisions or []),
        },
        "sources": [{
            "source_id": row.source_id, "url": row.url, "title": row.title,
            "source_type": row.source_type, "author": row.author,
            "published_at": row.published_at, "domain": row.domain,
            "http_status": row.http_status, "content_hash": row.content_hash,
            "content_excerpt": (row.content_excerpt or "")[:2000],
            "status": row.status,
            "retrieved_at": row.retrieved_at.isoformat() if row.retrieved_at else None,
        } for row in sources],
        "evidence": [{
            "evidence_id": row.evidence_id, "source_id": row.source_id,
            "question_id": row.question_id, "quote": row.quote,
            "locator": row.locator, "status": row.status,
        } for row in evidence],
        "claims": [{
            "claim_id": row.claim_id, "question_id": row.question_id,
            "statement": row.statement, "status": row.status,
            "support_evidence": list(row.support_evidence or []),
            "contradict_evidence": list(row.contradict_evidence or []),
            "verification": dict(row.verification or {}),
        } for row in claims],
    }


@router.post("/{goal_id}/research/plan")
async def revise_research_plan(
    goal_id: str,
    body: ResearchRevisionRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """用户调整研究范围/研究问题：追加 revision，保留已收集证据与主张。"""
    from app.services.research.store import update_plan

    await _owned_contract(db, goal_id=goal_id, user_id=current_user.id)
    plan = (await db.execute(
        select(ResearchPlan).where(
            ResearchPlan.goal_id == goal_id, ResearchPlan.user_id == current_user.id,
        ).order_by(ResearchPlan.id.desc()).limit(1)
    )).scalar_one_or_none()
    if plan is None:
        raise HTTPException(404, "研究计划不存在")
    updated = await update_plan(
        research_id=plan.research_id,
        user_id=current_user.id,
        topic=body.topic,
        scope=body.scope,
        questions=body.questions,
        report_outline=body.report_outline,
        reason=body.reason or "用户调整研究计划",
    )
    return updated
