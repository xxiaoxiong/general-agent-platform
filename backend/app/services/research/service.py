"""Deep Research Profile：研究计划激活与状态查询。

研究能力是 Goal Runtime 下的专业研究策略，不新建第二套 Agent Runtime：
- 计划/证据持久化在 MySQL；
- 单轮执行仍由现有 DeepAgents Agent + 研究工具完成；
- 证据核验由服务端确定性执行，模型不能自评通过。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Optional

from sqlalchemy import select

from app.core.config import settings
from app.core.database import AsyncSessionLocal
from app.models.research import ResearchPlan

from .planner import build_initial_questions, build_scope
from .store import ensure_plan, get_plan, snapshot


def should_activate_research(
    *,
    goal_mode: bool,
    task_contract: Any = None,
    execution_context: Optional[Dict[str, Any]] = None,
    capability_ids: Optional[list[str]] = None,
) -> bool:
    """研究类目标启用 Deep Research Profile（非目标模式保持原研究路径）。"""
    if not goal_mode or not bool(getattr(settings, "GOAL_RESEARCH_ENABLED", True)):
        return False
    route = execution_context or {}
    decision = route.get("decision") or {}
    task_type = str((getattr(task_contract, "task_type", "") or ""))
    candidates = {
        str(route.get("task_type") or ""),
        str(route.get("intent") or ""),
        str(decision.get("intent") or ""),
        task_type,
    }
    if "research" in candidates:
        return True
    return "web_research" in set(capability_ids or [])


async def activate_research_plan(
    *,
    run_id: str,
    goal_id: Optional[str],
    user_id: int,
    session_id: Optional[int],
    objective: str,
    execution_context: Optional[Dict[str, Any]] = None,
    previous_run_id: Optional[str] = None,
) -> Dict[str, Any]:
    decision = (execution_context or {}).get("decision") or {}
    previous = (
        await get_plan(run_id=str(previous_run_id), user_id=user_id)
        if previous_run_id else None
    )
    questions = (
        [dict(item) for item in (previous.questions or [])]
        if previous is not None else build_initial_questions(objective, decision)
    )
    return await ensure_plan(
        run_id=run_id,
        goal_id=goal_id,
        user_id=user_id,
        session_id=session_id,
        topic=(decision.get("goal") or objective or "")[:1000],
        questions=questions,
        scope=build_scope(decision),
        previous_run_id=previous_run_id,
    )


async def research_state(*, run_id: str, user_id: int) -> Dict[str, Any]:
    plan = await get_plan(run_id=run_id, user_id=user_id)
    if plan is None:
        return {}
    return await snapshot(research_id=plan.research_id, user_id=user_id)


async def research_state_for_goal(*, goal_id: str, user_id: int) -> Dict[str, Any]:
    plan = await get_plan(goal_id=goal_id, user_id=user_id)
    if plan is None:
        return {}
    return await snapshot(research_id=plan.research_id, user_id=user_id)


async def finish_research_plan(*, run_id: str, user_id: int, status: str) -> None:
    """Run 终态同步研究计划状态（completed / blocked / failed / cancelled）。"""
    if status not in {"completed", "blocked", "failed", "cancelled"}:
        return
    try:
        async with AsyncSessionLocal() as db:
            plan = (await db.execute(
                select(ResearchPlan).where(
                    ResearchPlan.run_id == run_id,
                    ResearchPlan.user_id == user_id,
                ).order_by(ResearchPlan.id.desc()).limit(1)
            )).scalar_one_or_none()
            if plan is None:
                return
            plan.status = "completed" if status == "completed" else status
            if status == "completed":
                plan.finished_at = datetime.utcnow()
            await db.commit()
    except Exception:  # noqa: BLE001 研究状态同步失败不影响 Run 终态
        return
