"""Admin API v2（§7 /api/v2/admin/observability）：监控概览。

数据源是新 Runtime 的运行事实（agent_runs / agent_tool_executions /
agent_run_events），不是旧引擎 Trace（A11：监控读旧模块 → Phase 5 删除）。
仅超管可访问；详细链路跳转 Langfuse / Grafana（P1 交付）。
"""
from __future__ import annotations

from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_superuser
from app.core.database import get_db
from app.models.agent import AgentRun, AgentRunEvent, AgentToolExecution
from app.models.user import User
from app.observability.langfuse_tracing import build_trace_web_url

router = APIRouter(prefix="/admin", tags=["admin-v2"])


@router.get("/observability")
async def observability_overview(
    hours: int = Query(24, ge=1, le=24 * 30),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_superuser),
):
    """监控概览（新 Runtime 口径）：Run 状态分布 / 工具执行 / 事件量。"""
    since = datetime.utcnow() - timedelta(hours=hours)

    run_rows = (await db.execute(
        select(AgentRun.status, func.count(AgentRun.id))
        .where(AgentRun.created_at >= since)
        .group_by(AgentRun.status)
    )).all()
    tool_rows = (await db.execute(
        select(AgentToolExecution.status, func.count(AgentToolExecution.id))
        .where(AgentToolExecution.created_at >= since)
        .group_by(AgentToolExecution.status)
    )).all()
    event_count = (await db.execute(
        select(func.count(AgentRunEvent.id)).where(AgentRunEvent.created_at >= since)
    )).scalar() or 0

    # 运行中（未终结 + 租约有效）与待审批计数
    active = (await db.execute(
        select(func.count(AgentRun.id)).where(
            AgentRun.status.in_(("pending", "running")),
            AgentRun.created_at >= since,
        )
    )).scalar() or 0
    waiting = (await db.execute(
        select(func.count(AgentRun.id)).where(
            AgentRun.status == "waiting_approval",
        )
    )).scalar() or 0

    return {
        "window_hours": hours,
        "runs": {"by_status": {s: c for s, c in run_rows}, "active": active,
                 "waiting_approval": waiting},
        "tool_executions": {"by_status": {s: c for s, c in tool_rows}},
        "events": {"total": event_count},
        "links": {
            "langfuse": build_trace_web_url(),
            "grafana": "/grafana",
        },
    }
