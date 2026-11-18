"""确定性运行指标：从平台事实表计算，不依赖 LLM / Langfuse。

主流 Agent 评测指标映射：
- 任务成败 / 健壮性：agent_runs.status、agent_run_events（run.retrying 等）
- 工具准确率：agent_tool_executions（失败/重试/重复调用）
- 效率：agent_runs.budget_* 累计 + 时长
- HITL：agent_approvals（决策分布）
"""
import logging
from datetime import datetime
from typing import Any, Dict

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent import Agent, AgentApproval, AgentRun, AgentRunEvent, AgentToolExecution

logger = logging.getLogger(__name__)


async def compute_deterministic(db: AsyncSession, run: AgentRun) -> Dict[str, Any]:
    """计算 Run 的确定性指标字典。"""
    m: Dict[str, Any] = {
        "run_status": run.status or "",
        "success": (run.status == "completed"),
        "limit_exceeded": (run.status == "limit_exceeded"),
        "error_present": bool(run.error),
        "duration_s": None,
        "steps": int(run.budget_steps or 0),
        "model_calls": int(run.budget_model_calls or 0),
        "tool_calls": int(run.budget_tool_calls or 0),
        "tokens": int(run.budget_tokens or 0),
    }
    if run.started_at and run.finished_at:
        try:
            m["duration_s"] = round(
                (run.finished_at - run.started_at).total_seconds(), 2)
        except Exception:  # noqa: BLE001
            m["duration_s"] = None

    # 工具执行账本
    tool_rows = (await db.execute(
        select(
            AgentToolExecution.tool_name,
            AgentToolExecution.status,
            AgentToolExecution.attempt_count,
            AgentToolExecution.args_hash,
        ).where(AgentToolExecution.run_id == run.id)
    )).all()
    tool_total = len(tool_rows)
    tool_failed = sum(1 for r in tool_rows if r.status in ("failed", "unknown", "needs_reconciliation"))
    tool_retried = sum(1 for r in tool_rows if (r.attempt_count or 0) > 1)
    # 重复调用：同 tool_name + args_hash 出现多次
    seen: Dict[str, int] = {}
    for r in tool_rows:
        key = f"{r.tool_name}:{r.args_hash or ''}"
        seen[key] = seen.get(key, 0) + 1
    repeated = sum(c - 1 for c in seen.values() if c > 1)
    m.update({
        "tool_total": tool_total,
        "tool_failed": tool_failed,
        "tool_error_rate": round(tool_failed / tool_total, 4) if tool_total else 0.0,
        "tool_retried": tool_retried,
        "tool_repeat_calls": repeated,
        "unique_tools": len({r.tool_name for r in tool_rows}),
    })

    # 健壮性：重试类事件
    retry_events = (await db.execute(
        select(func.count()).select_from(AgentRunEvent).where(
            AgentRunEvent.run_id == run.id,
            AgentRunEvent.event_type.in_(("run.retrying", "run.retry", "run.recovered")),
        )
    )).scalar() or 0
    m["recovery_events"] = int(retry_events)

    # HITL：审批决策分布
    approval_rows = (await db.execute(
        select(AgentApproval.decision).where(AgentApproval.run_id == run.id)
    )).all()
    hitl_total = len(approval_rows)
    hitl_rejected = sum(
        1 for r in approval_rows if (r.decision or "") in ("reject", "timeout"))
    m.update({
        "hitl_total": hitl_total,
        "hitl_rejected": hitl_rejected,
    })

    # 综合确定性分（0~10）：成败为主，扣工具错误 / 重复调用 / 超限
    score = 10.0 if m["success"] else 2.0
    score -= min(3.0, m["tool_error_rate"] * 5.0)
    score -= min(2.0, m["tool_repeat_calls"] * 0.3)
    if m["limit_exceeded"]:
        score -= 1.5
    m["deterministic_score"] = round(max(0.0, min(10.0, score)), 2)
    return m


async def aggregate_metrics(db: AsyncSession, agent_id: int,
                            window_start: datetime) -> Dict[str, Any]:
    """聚合窗口内该 Agent 的评估结果 → 总体指标（报告基线/验证对比用）。"""
    from app.models.evolution import AgentEvaluation

    rows = (await db.execute(
        select(AgentEvaluation).where(
            AgentEvaluation.agent_id == agent_id,
            AgentEvaluation.created_at >= window_start,
            AgentEvaluation.status == "computed",
        )
    )).scalars().all()
    return aggregate_eval_rows(rows)


def aggregate_eval_rows(rows) -> Dict[str, Any]:
    """把一组 AgentEvaluation 聚合为总体指标。"""
    if not rows:
        return {"sample_count": 0}

    def _avg(vals):
        vals = [v for v in vals if v is not None]
        return round(sum(vals) / len(vals), 3) if vals else None

    det = [r.deterministic or {} for r in rows]
    return {
        "sample_count": len(rows),
        "success_rate": round(sum(1 for r in rows if r.success) / len(rows), 4),
        "avg_overall": _avg([r.overall for r in rows]),
        "avg_deterministic_score": _avg([d.get("deterministic_score") for d in det]),
        "avg_steps": _avg([d.get("steps") for d in det]),
        "avg_tool_calls": _avg([d.get("tool_calls") for d in det]),
        "avg_tokens": _avg([d.get("tokens") for d in det]),
        "avg_duration_s": _avg([d.get("duration_s") for d in det]),
        "tool_error_rate": _avg([d.get("tool_error_rate") for d in det]),
        "avg_tool_repeat_calls": _avg([d.get("tool_repeat_calls") for d in det]),
        "limit_exceeded_rate": round(
            sum(1 for d in det if d.get("limit_exceeded")) / len(det), 4),
        "hitl_total": sum(d.get("hitl_total", 0) for d in det),
        "hitl_rejected": sum(d.get("hitl_rejected", 0) for d in det),
    }
