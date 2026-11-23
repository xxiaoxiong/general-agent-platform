"""自进化闭环 API（v2，仅超级管理员）。

评估概览 / 评估列表 / 手动触发评估 / 进化报告生成与查看 /
建议一键应用 / 变更回滚 / Markdown 导出 / 变更审计。
"""
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_superuser
from app.core.database import get_db
from app.models.agent import Agent
from app.models.evolution import AgentEvaluation, EvolutionChangeLog, EvolutionReport

router = APIRouter(prefix="/evolution", tags=["evolution"],
                   dependencies=[Depends(get_current_superuser)])


# ────────────────────────────── 概览 / 评估 ──────────────────────────────


@router.get("/overview")
async def overview(
    agent_id: Optional[int] = None,
    hours: int = Query(168, ge=1, le=24 * 90),
    db: AsyncSession = Depends(get_db),
) -> Dict[str, Any]:
    from app.services.evolution.metrics import aggregate_eval_rows

    since = datetime.utcnow() - timedelta(hours=hours)
    stmt = select(AgentEvaluation).where(
        AgentEvaluation.created_at >= since,
        AgentEvaluation.status == "computed",
    )
    if agent_id:
        stmt = stmt.where(AgentEvaluation.agent_id == agent_id)
    evals = (await db.execute(stmt)).scalars().all()

    from app.models.agent import AgentVersion
    ver_rows = (await db.execute(
        select(AgentVersion.id, AgentVersion.version_no)
        .where(AgentVersion.agent_id == (agent_id or -1)))).all() if agent_id else []
    ver_map = {vid: vno for vid, vno in ver_rows}
    grouped: Dict[int, List[AgentEvaluation]] = {}
    for ev in evals:
        grouped.setdefault(ev.agent_version_id or 0, []).append(ev)
    by_version = [
        {"version_no": ver_map.get(vid), "agent_version_id": vid or None,
         **aggregate_eval_rows(rows)}
        for vid, rows in sorted(grouped.items(), key=lambda kv: ver_map.get(kv[0]) or 0)
    ]
    return {
        "window_hours": hours,
        "overall": aggregate_eval_rows(evals),
        "by_version": by_version,
    }


@router.get("/evaluations")
async def list_evaluations(
    agent_id: Optional[int] = None,
    version_no: Optional[int] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
) -> Dict[str, Any]:
    from app.observability.langfuse_tracing import run_trace_id
    from app.services.evolution.langfuse_api import trace_web_url

    stmt = select(AgentEvaluation)
    count_stmt = select(func.count()).select_from(AgentEvaluation)
    if agent_id:
        stmt = stmt.where(AgentEvaluation.agent_id == agent_id)
        count_stmt = count_stmt.where(AgentEvaluation.agent_id == agent_id)
    if version_no is not None:
        from app.models.agent import AgentVersion
        vids = [vid for (vid,) in (await db.execute(
            select(AgentVersion.id).where(AgentVersion.version_no == version_no))
        ).all()]
        stmt = stmt.where(AgentEvaluation.agent_version_id.in_(vids or [-1]))
        count_stmt = count_stmt.where(AgentEvaluation.agent_version_id.in_(vids or [-1]))
    total = (await db.execute(count_stmt)).scalar() or 0
    rows = (await db.execute(
        stmt.order_by(AgentEvaluation.created_at.desc())
        .offset((page - 1) * page_size).limit(page_size)
    )).scalars().all()
    return {
        "items": [
            {
                "id": r.id,
                "run_id": r.run_id,
                "agent_id": r.agent_id,
                "agent_version_id": r.agent_version_id,
                "status": r.status,
                "success": r.success,
                "overall": r.overall,
                "deterministic": r.deterministic,
                "judge_scores": r.judge_scores,
                "error": r.error,
                "evaluated_at": r.evaluated_at.isoformat() if r.evaluated_at else None,
                "langfuse_trace_id": r.langfuse_trace_id or run_trace_id(r.run_id),
                "langfuse_trace_url": trace_web_url(
                    r.langfuse_trace_id or run_trace_id(r.run_id)),
            }
            for r in rows
        ],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


class TriggerRequest(BaseModel):
    run_ids: Optional[List[str]] = None
    agent_id: Optional[int] = None
    limit: int = 50


@router.post("/evaluations/trigger")
async def trigger_evaluations(
    body: TriggerRequest,
    db: AsyncSession = Depends(get_db),
) -> Dict[str, Any]:
    """手动触发评估（逐 Run 立即评估，UNIQUE(run_id) 保证幂等）。"""
    from app.models.agent import AgentRun
    from app.services.evolution.evaluator import TERMINAL_STATUSES, evaluate_run

    stmt = select(AgentRun).where(AgentRun.status.in_(TERMINAL_STATUSES))
    if body.run_ids:
        stmt = stmt.where(AgentRun.id.in_(body.run_ids))
    elif body.agent_id:
        stmt = stmt.where(AgentRun.agent_id == body.agent_id)
    else:
        raise HTTPException(422, "run_ids 或 agent_id 至少提供一个")
    runs = (await db.execute(stmt.limit(body.limit))).scalars().all()
    queued = 0
    for run in runs:
        result = await evaluate_run(db, run)
        if result is not None:
            queued += 1
    return {"queued": queued}


# ────────────────────────────── 报告 / 建议 ──────────────────────────────


class ReportGenerateRequest(BaseModel):
    agent_id: int
    window_hours: int = 168


@router.post("/reports/generate")
async def generate_report(
    body: ReportGenerateRequest,
    db: AsyncSession = Depends(get_db),
) -> Dict[str, Any]:
    from app.services.evolution.evolution_advisor import SuggestionError, generate_report

    try:
        report = await generate_report(db, body.agent_id, body.window_hours,
                                       created_by="admin")
    except SuggestionError as e:
        raise HTTPException(422, str(e))
    return _report_dict(report)


@router.get("/reports")
async def list_reports(
    agent_id: Optional[int] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
) -> Dict[str, Any]:
    stmt = select(EvolutionReport)
    count_stmt = select(func.count()).select_from(EvolutionReport)
    if agent_id:
        stmt = stmt.where(EvolutionReport.agent_id == agent_id)
        count_stmt = count_stmt.where(EvolutionReport.agent_id == agent_id)
    total = (await db.execute(count_stmt)).scalar() or 0
    rows = (await db.execute(
        stmt.order_by(EvolutionReport.created_at.desc())
        .offset((page - 1) * page_size).limit(page_size)
    )).scalars().all()
    return {
        "items": [_report_dict(r) for r in rows],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@router.get("/reports/{report_id}")
async def get_report(
    report_id: int,
    db: AsyncSession = Depends(get_db),
) -> Dict[str, Any]:
    report = await _get_report(db, report_id)
    return _report_dict(report)


@router.get("/reports/{report_id}/markdown")
async def export_report_markdown(
    report_id: int,
    db: AsyncSession = Depends(get_db),
) -> Dict[str, Any]:
    from app.services.evolution.evolution_advisor import report_to_markdown

    report = await _get_report(db, report_id)
    return {"report_id": report.id, "markdown": report_to_markdown(report)}


@router.post("/reports/{report_id}/suggestions/{index}/apply")
async def apply_suggestion(
    report_id: int,
    index: int,
    db: AsyncSession = Depends(get_db),
) -> Dict[str, Any]:
    from app.services.evolution.evolution_advisor import (
        SuggestionError, apply_suggestion,
    )

    try:
        result = await apply_suggestion(db, report_id, index, applied_by="admin")
    except SuggestionError as e:
        raise HTTPException(422, str(e))
    return result


# ────────────────────────────── 开发简报（客观数据） ──────────────────────────────


@router.get("/briefing")
async def get_dev_briefing(
    agent_id: Optional[int] = None,
    hours: int = Query(168, ge=1, le=24 * 90),
    db: AsyncSession = Depends(get_db),
) -> Dict[str, Any]:
    """开发迭代简报：窗口内客观执行数据汇总（不含 AI 主观建议）。"""
    from app.services.evolution.dev_briefing import build_briefing

    return await build_briefing(db, window_hours=hours, agent_id=agent_id)


@router.get("/briefing/markdown")
async def export_dev_briefing_markdown(
    agent_id: Optional[int] = None,
    hours: int = Query(168, ge=1, le=24 * 90),
    db: AsyncSession = Depends(get_db),
) -> Dict[str, Any]:
    """简报 Markdown 导出（可直接喂给开发 AI 作为迭代输入）。"""
    from app.services.evolution.dev_briefing import briefing_to_markdown, build_briefing

    data = await build_briefing(db, window_hours=hours, agent_id=agent_id)
    return {"markdown": briefing_to_markdown(data)}


# ────────────────────────────── 变更 / 回滚 ──────────────────────────────


@router.get("/changes")
async def list_changes(
    agent_id: Optional[int] = None,
    db: AsyncSession = Depends(get_db),
) -> Dict[str, Any]:
    stmt = select(EvolutionChangeLog).order_by(EvolutionChangeLog.applied_at.desc())
    if agent_id:
        stmt = stmt.where(EvolutionChangeLog.agent_id == agent_id)
    rows = (await db.execute(stmt.limit(200))).scalars().all()
    return {
        "items": [
            {
                "id": r.id,
                "report_id": r.report_id,
                "agent_id": r.agent_id,
                "change_type": r.change_type,
                "target": r.target,
                "before": r.before,
                "after": r.after,
                "applied_version_no": r.applied_version_no,
                "status": r.status,
                "validation": r.validation,
                "revert_of_change_id": r.revert_of_change_id,
                "applied_by": r.applied_by,
                "applied_at": r.applied_at.isoformat() if r.applied_at else None,
                "reverted_at": r.reverted_at.isoformat() if r.reverted_at else None,
            }
            for r in rows
        ]
    }


@router.post("/changes/{change_id}/rollback")
async def rollback_change(
    change_id: int,
    db: AsyncSession = Depends(get_db),
) -> Dict[str, Any]:
    from app.services.evolution.evolution_advisor import (
        SuggestionError, rollback_change,
    )

    try:
        return await rollback_change(db, change_id, applied_by="admin")
    except SuggestionError as e:
        raise HTTPException(422, str(e))


# ────────────────────────────── 内部工具 ──────────────────────────────


async def _get_report(db: AsyncSession, report_id: int) -> EvolutionReport:
    report = (await db.execute(
        select(EvolutionReport).where(EvolutionReport.id == report_id)
    )).scalar_one_or_none()
    if report is None:
        raise HTTPException(404, f"报告 {report_id} 不存在")
    return report


def _report_dict(report: EvolutionReport) -> Dict[str, Any]:
    return {
        "id": report.id,
        "agent_id": report.agent_id,
        "window_start": report.window_start.isoformat() if report.window_start else None,
        "window_end": report.window_end.isoformat() if report.window_end else None,
        "baseline_version_no": report.baseline_version_no,
        "status": report.status,
        "summary_text": report.summary_text,
        "suggestions": report.suggestions or [],
        "metrics_before": report.metrics_before or {},
        "metrics_after": report.metrics_after or {},
        "created_by": report.created_by,
        "validated_at": report.validated_at.isoformat() if report.validated_at else None,
        "created_at": report.created_at.isoformat() if report.created_at else None,
    }
