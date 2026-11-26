"""开发迭代简报：客观执行数据的收集、整理与呈现。

设计原则：只汇总客观事实数据（Run 状态、工具账本、HITL 审批、指标、
版本趋势、变更审计），不包含任何 AI 主观判断或优化建议——
方向判断由阅读者（人或开发 AI）基于数据自行做出。

数据来源全部为平台事实表：
- agent_runs / agent_tool_executions / agent_approvals（执行事实）
- agent_evaluations（LLM 裁判评分为已采集的评估记录，原样呈现）
- evolution_change_log（配置变更审计）
"""
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent import (
    AgentApproval, AgentRun, AgentToolExecution, AgentVersion,
)
from app.models.evolution import AgentEvaluation, EvolutionChangeLog
from app.services.evolution.evaluator import production_run_condition

_TRUNC = 300          # 错误/摘要文本截断长度
_MAX_ROWS = 50        # 各明细列表上限


def _trunc(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    value = str(value)
    return value if len(value) <= _TRUNC else value[:_TRUNC] + "…"


def _avg(vals: List[float]) -> Optional[float]:
    vals = [v for v in vals if v is not None]
    return round(sum(vals) / len(vals), 3) if vals else None


def _iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.isoformat() if dt else None


async def build_briefing(
    db: AsyncSession,
    window_hours: int = 168,
    agent_id: Optional[int] = None,
) -> Dict[str, Any]:
    """汇总窗口内客观执行数据（不含任何 LLM 调用）。"""
    now = datetime.utcnow()
    window_start = now - timedelta(hours=window_hours)

    # 只取所需列（agent_runs 含大 JSON 列，全行 ORDER BY 会超出 MySQL sort buffer），
    # 排序放在 Python 侧完成。
    run_stmt = select(
        AgentRun.id, AgentRun.agent_id, AgentRun.agent_version_id,
        AgentRun.status, AgentRun.error,
        AgentRun.budget_steps, AgentRun.budget_model_calls,
        AgentRun.budget_tool_calls, AgentRun.budget_tokens,
        AgentRun.created_at, AgentRun.started_at, AgentRun.finished_at,
    ).where(AgentRun.created_at >= window_start, production_run_condition())
    if agent_id:
        run_stmt = run_stmt.where(AgentRun.agent_id == agent_id)
    runs = sorted(
        (await db.execute(run_stmt)).all(),
        key=lambda r: r.created_at or datetime.min, reverse=True)

    data: Dict[str, Any] = {
        "meta": {
            "generated_at": _iso(now),
            "window_start": _iso(window_start),
            "window_end": _iso(now),
            "window_hours": window_hours,
            "agent_id": agent_id,
        },
        "runs": _runs_overview(runs),
        "failed_runs": _failed_runs(runs),
        "tools": await _tool_stats(db, window_start, agent_id),
        "hitl": await _hitl_data(db, window_start, agent_id),
        "by_version": await _by_version(db, runs),
        "evaluations": await _eval_data(db, window_start, agent_id),
        "changes": await _change_log(db, window_start, agent_id),
    }
    return data


# ────────────────────────────── 各数据分区 ──────────────────────────────


def _runs_overview(runs: List[AgentRun]) -> Dict[str, Any]:
    by_status: Dict[str, int] = {}
    durations: List[float] = []
    for r in runs:
        by_status[r.status or ""] = by_status.get(r.status or "", 0) + 1
        if r.started_at and r.finished_at:
            try:
                durations.append((r.finished_at - r.started_at).total_seconds())
            except Exception:  # noqa: BLE001
                pass
    total = len(runs)
    completed = by_status.get("completed", 0)
    return {
        "total": total,
        "by_status": by_status,
        "completed": completed,
        "success_rate": round(completed / total, 4) if total else None,
        "with_error": sum(1 for r in runs if r.error),
        "duration_avg_s": _avg(durations),
        "duration_min_s": round(min(durations), 2) if durations else None,
        "duration_max_s": round(max(durations), 2) if durations else None,
        "steps_total": sum(int(r.budget_steps or 0) for r in runs),
        "steps_avg": _avg([int(r.budget_steps or 0) for r in runs]),
        "tokens_total": sum(int(r.budget_tokens or 0) for r in runs),
        "tokens_avg": _avg([int(r.budget_tokens or 0) for r in runs]),
        "tool_calls_total": sum(int(r.budget_tool_calls or 0) for r in runs),
        "model_calls_total": sum(int(r.budget_model_calls or 0) for r in runs),
    }


def _failed_runs(runs: List[AgentRun]) -> List[Dict[str, Any]]:
    out = []
    for r in runs:
        if r.status == "completed" and not r.error:
            continue
        out.append({
            "run_id": r.id,
            "agent_id": r.agent_id,
            "agent_version_id": r.agent_version_id,
            "status": r.status,
            "error": _trunc(r.error),
            "created_at": _iso(r.created_at),
            "finished_at": _iso(r.finished_at),
        })
        if len(out) >= _MAX_ROWS:
            break
    return out


async def _tool_stats(
    db: AsyncSession, window_start: datetime, agent_id: Optional[int],
) -> Dict[str, Any]:
    run_ids = select(AgentRun.id).where(
        AgentRun.created_at >= window_start, production_run_condition())
    if agent_id:
        run_ids = run_ids.where(AgentRun.agent_id == agent_id)
    rows = (await db.execute(
        select(AgentToolExecution.run_id, AgentToolExecution.tool_name,
               AgentToolExecution.status, AgentToolExecution.attempt_count,
               AgentToolExecution.args_hash, AgentToolExecution.error)
        .where(AgentToolExecution.run_id.in_(run_ids))
    )).all()

    per_tool: Dict[str, Dict[str, Any]] = {}
    repeat_seen: Dict[str, int] = {}
    failures: List[Dict[str, Any]] = []
    for r in rows:
        stat = per_tool.setdefault(r.tool_name, {
            "tool_name": r.tool_name, "calls": 0, "failed": 0,
            "retried": 0, "repeat_calls": 0,
        })
        stat["calls"] += 1
        if r.status in ("failed", "unknown", "needs_reconciliation"):
            stat["failed"] += 1
            if r.error and len(failures) < _MAX_ROWS:
                failures.append({
                    "run_id": r.run_id,
                    "tool_name": r.tool_name,
                    "status": r.status,
                    "error": _trunc(r.error),
                })
        if (r.attempt_count or 0) > 1:
            stat["retried"] += 1
        key = f"{r.run_id}:{r.tool_name}:{r.args_hash or ''}"
        repeat_seen[key] = repeat_seen.get(key, 0) + 1

    for key, cnt in repeat_seen.items():
        if cnt > 1:
            _, tool_name, _ = key.split(":", 2)
            per_tool[tool_name]["repeat_calls"] += cnt - 1

    tools = sorted(per_tool.values(), key=lambda t: t["failed"], reverse=True)
    for t in tools:
        t["error_rate"] = round(t["failed"] / t["calls"], 4) if t["calls"] else 0.0
    total = sum(t["calls"] for t in tools)
    failed = sum(t["failed"] for t in tools)
    return {
        "total_calls": total,
        "total_failed": failed,
        "error_rate": round(failed / total, 4) if total else None,
        "per_tool": tools,
        "failure_samples": failures,
    }


async def _hitl_data(
    db: AsyncSession, window_start: datetime, agent_id: Optional[int],
) -> Dict[str, Any]:
    run_ids = select(AgentRun.id).where(
        AgentRun.created_at >= window_start, production_run_condition())
    if agent_id:
        run_ids = run_ids.where(AgentRun.agent_id == agent_id)
    rows = sorted(
        (await db.execute(
            select(AgentApproval.run_id, AgentApproval.tool_name,
                   AgentApproval.decision, AgentApproval.action_summary,
                   AgentApproval.message, AgentApproval.created_at)
            .where(AgentApproval.run_id.in_(run_ids))
        )).all(),
        key=lambda a: a.created_at or datetime.min, reverse=True)

    by_decision: Dict[str, int] = {}
    rejected: List[Dict[str, Any]] = []
    for a in rows:
        d = a.decision or "pending"
        by_decision[d] = by_decision.get(d, 0) + 1
        if d in ("reject", "timeout") and len(rejected) < _MAX_ROWS:
            rejected.append({
                "run_id": a.run_id,
                "tool_name": a.tool_name,
                "decision": d,
                "action_summary": _trunc(a.action_summary),
                "message": _trunc(a.message),
                "created_at": _iso(a.created_at),
            })
    return {
        "total": len(rows),
        "by_decision": by_decision,
        "rejected_records": rejected,
    }


async def _by_version(
    db: AsyncSession, runs: List[AgentRun],
) -> List[Dict[str, Any]]:
    ver_rows = (await db.execute(
        select(AgentVersion.id, AgentVersion.version_no))).all()
    ver_map = {vid: vno for vid, vno in ver_rows}

    grouped: Dict[int, List[AgentRun]] = {}
    for r in runs:
        grouped.setdefault(r.agent_version_id or 0, []).append(r)

    out = []
    for vid, vruns in sorted(grouped.items(),
                             key=lambda kv: ver_map.get(kv[0]) or 0):
        durations = [
            (r.finished_at - r.started_at).total_seconds()
            for r in vruns if r.started_at and r.finished_at
        ]
        out.append({
            "agent_version_id": vid or None,
            "version_no": ver_map.get(vid),
            "runs": len(vruns),
            "completed": sum(1 for r in vruns if r.status == "completed"),
            "failed": sum(1 for r in vruns if r.status != "completed"),
            "tokens_avg": _avg([int(r.budget_tokens or 0) for r in vruns]),
            "duration_avg_s": _avg(durations),
        })
    return out


async def _eval_data(
    db: AsyncSession, window_start: datetime, agent_id: Optional[int],
) -> Dict[str, Any]:
    stmt = select(AgentEvaluation).where(
        AgentEvaluation.created_at >= window_start,
        AgentEvaluation.status == "computed",
    )
    if agent_id:
        stmt = stmt.where(AgentEvaluation.agent_id == agent_id)
    evals = (await db.execute(stmt)).scalars().all()

    dims: Dict[str, List[float]] = {}
    verdicts: Dict[str, int] = {}
    issues: List[Dict[str, Any]] = []
    overalls: List[float] = []
    for ev in evals:
        if ev.overall is not None:
            overalls.append(ev.overall)
        scores = ev.judge_scores or {}
        for k, v in (scores.get("dimensions") or {}).items():
            if isinstance(v, (int, float)):
                dims.setdefault(k, []).append(float(v))
        verdict = scores.get("verdict")
        if verdict:
            verdicts[verdict] = verdicts.get(verdict, 0) + 1
        for issue in (scores.get("issues") or [])[:5]:
            if len(issues) >= _MAX_ROWS:
                break
            issues.append({
                "run_id": ev.run_id,
                "dimension": issue.get("dimension"),
                "evidence": _trunc(issue.get("evidence")),
                "suggestion": _trunc(issue.get("suggestion")),
            })
    return {
        "count": len(evals),
        "avg_overall": _avg(overalls),
        "avg_dimensions": {k: _avg(v) for k, v in dims.items()},
        "verdicts": verdicts,
        "issue_records": issues,
    }


async def _change_log(
    db: AsyncSession, window_start: datetime, agent_id: Optional[int],
) -> List[Dict[str, Any]]:
    stmt = select(EvolutionChangeLog).where(
        EvolutionChangeLog.applied_at >= window_start)
    if agent_id:
        stmt = stmt.where(EvolutionChangeLog.agent_id == agent_id)
    rows = sorted(
        (await db.execute(stmt)).scalars().all(),
        key=lambda r: r.applied_at or datetime.min, reverse=True,
    )[:_MAX_ROWS]
    return [
        {
            "id": r.id,
            "agent_id": r.agent_id,
            "change_type": r.change_type,
            "target": r.target,
            "before": r.before,
            "after": r.after,
            "status": r.status,
            "validation": r.validation,
            "applied_at": _iso(r.applied_at),
        }
        for r in rows
    ]


# ────────────────────────────── Markdown 渲染 ──────────────────────────────


def briefing_to_markdown(data: Dict[str, Any]) -> str:
    """把简报数据渲染为 Markdown（同样只呈现事实，不含建议）。"""
    meta = data.get("meta", {})
    runs = data.get("runs", {})
    tools = data.get("tools", {})
    hitl = data.get("hitl", {})
    ev = data.get("evaluations", {})

    lines: List[str] = []
    lines.append("# Agent 执行数据简报（客观数据）")
    lines.append("")
    lines.append(
        f"> 窗口：{meta.get('window_start')} ~ {meta.get('window_end')}"
        f"（{meta.get('window_hours')} 小时） | Agent：{meta.get('agent_id') or '全部'}"
        f" | 生成时间：{meta.get('generated_at')}")
    lines.append(">")
    lines.append("> 本简报仅包含客观执行数据，不含主观优化建议，供开发迭代决策参考。")
    lines.append("")

    # 1. 执行概览
    lines.append("## 1. 执行概览")
    lines.append("")
    lines.append("| 指标 | 数值 |")
    lines.append("| --- | --- |")
    status_text = ", ".join(f"{k}={v}" for k, v in (runs.get("by_status") or {}).items())
    for label, value in [
        ("Run 总数", runs.get("total")),
        ("状态分布", status_text or "-"),
        ("成功率", runs.get("success_rate")),
        ("含错误信息的 Run", runs.get("with_error")),
        ("耗时 avg/min/max (s)",
         f"{runs.get('duration_avg_s')} / {runs.get('duration_min_s')} / {runs.get('duration_max_s')}"),
        ("步数 总/均", f"{runs.get('steps_total')} / {runs.get('steps_avg')}"),
        ("Tokens 总/均", f"{runs.get('tokens_total')} / {runs.get('tokens_avg')}"),
        ("工具调用总数", runs.get("tool_calls_total")),
        ("模型调用总数", runs.get("model_calls_total")),
    ]:
        lines.append(f"| {label} | {value if value is not None else '-'} |")
    lines.append("")

    # 2. 失败 Run 记录
    lines.append(f"## 2. 失败 Run 记录（最多 {_MAX_ROWS} 条）")
    lines.append("")
    failed = data.get("failed_runs") or []
    if failed:
        lines.append("| Run | Agent | 状态 | 错误 | 创建时间 |")
        lines.append("| --- | --- | --- | --- | --- |")
        for f in failed:
            lines.append(
                f"| {f['run_id']} | {f['agent_id']} | {f['status']} "
                f"| {(f.get('error') or '-').replace('|', '/')} | {f.get('created_at')} |")
    else:
        lines.append("窗口内无失败 Run。")
    lines.append("")

    # 3. 工具调用统计
    lines.append("## 3. 工具调用统计")
    lines.append("")
    lines.append(
        f"总调用 {tools.get('total_calls')} | 失败 {tools.get('total_failed')}"
        f" | 错误率 {tools.get('error_rate')}")
    lines.append("")
    per_tool = tools.get("per_tool") or []
    if per_tool:
        lines.append("| 工具 | 调用 | 失败 | 重试 | 重复调用 | 错误率 |")
        lines.append("| --- | --- | --- | --- | --- | --- |")
        for t in per_tool:
            lines.append(
                f"| {t['tool_name']} | {t['calls']} | {t['failed']} "
                f"| {t['retried']} | {t['repeat_calls']} | {t['error_rate']} |")
    lines.append("")

    # 4. 工具失败记录
    lines.append(f"## 4. 工具失败记录（最多 {_MAX_ROWS} 条）")
    lines.append("")
    samples = tools.get("failure_samples") or []
    if samples:
        lines.append("| Run | 工具 | 状态 | 错误 |")
        lines.append("| --- | --- | --- | --- |")
        for s in samples:
            lines.append(
                f"| {s['run_id']} | {s['tool_name']} | {s['status']} "
                f"| {(s.get('error') or '-').replace('|', '/')} |")
    else:
        lines.append("窗口内无工具失败记录。")
    lines.append("")

    # 5. HITL 审批数据
    lines.append("## 5. HITL 审批数据")
    lines.append("")
    decision_text = ", ".join(
        f"{k}={v}" for k, v in (hitl.get("by_decision") or {}).items())
    lines.append(f"审批总数 {hitl.get('total')} | 决策分布：{decision_text or '-'}")
    lines.append("")
    rejected = hitl.get("rejected_records") or []
    if rejected:
        lines.append("| Run | 工具 | 决策 | 动作摘要 | 备注 | 时间 |")
        lines.append("| --- | --- | --- | --- | --- | --- |")
        for a in rejected:
            lines.append(
                f"| {a['run_id']} | {a['tool_name']} | {a['decision']} "
                f"| {(a.get('action_summary') or '-').replace('|', '/')} "
                f"| {(a.get('message') or '-').replace('|', '/')} | {a.get('created_at')} |")
    lines.append("")

    # 6. 分版本趋势
    lines.append("## 6. 分版本趋势")
    lines.append("")
    by_version = data.get("by_version") or []
    if by_version:
        lines.append("| 版本 | Run 数 | 完成 | 失败 | 平均 Tokens | 平均耗时(s) |")
        lines.append("| --- | --- | --- | --- | --- | --- |")
        for v in by_version:
            label = v.get("version_no") if v.get("version_no") is not None else "(无版本)"
            lines.append(
                f"| {label} | {v['runs']} | {v['completed']} | {v['failed']} "
                f"| {v.get('tokens_avg') or '-'} | {v.get('duration_avg_s') or '-'} |")
    else:
        lines.append("窗口内无 Run 数据。")
    lines.append("")

    # 7. 评估数据
    lines.append("## 7. 评估数据（LLM 裁判记录）")
    lines.append("")
    lines.append(
        f"评估样本 {ev.get('count')} | 平均综合分 {ev.get('avg_overall')}"
        f" | 裁决分布：{', '.join(f'{k}={v}' for k, v in (ev.get('verdicts') or {}).items()) or '-'}")
    lines.append("")
    avg_dims = ev.get("avg_dimensions") or {}
    if avg_dims:
        lines.append("| 维度 | 平均分 |")
        lines.append("| --- | --- |")
        for k, v in avg_dims.items():
            lines.append(f"| {k} | {v if v is not None else '-'} |")
        lines.append("")
    issues = ev.get("issue_records") or []
    if issues:
        lines.append(f"裁判问题记录（原始记录，最多 {_MAX_ROWS} 条）：")
        lines.append("")
        lines.append("| Run | 维度 | 证据 | 记录建议 |")
        lines.append("| --- | --- | --- | --- |")
        for i in issues:
            lines.append(
                f"| {i['run_id']} | {i.get('dimension') or '-'} "
                f"| {(i.get('evidence') or '-').replace('|', '/')} "
                f"| {(i.get('suggestion') or '-').replace('|', '/')} |")
    lines.append("")

    # 8. 配置变更审计
    lines.append("## 8. 配置变更审计")
    lines.append("")
    changes = data.get("changes") or []
    if changes:
        lines.append("| ID | 类型 | 目标 | 状态 | 验证结论 | 时间 |")
        lines.append("| --- | --- | --- | --- | --- | --- |")
        for c in changes:
            verdict = (c.get("validation") or {}).get("verdict", "-")
            lines.append(
                f"| {c['id']} | {c['change_type']} | {c['target']} "
                f"| {c['status']} | {verdict} | {c.get('applied_at')} |")
    else:
        lines.append("窗口内无配置变更。")
    lines.append("")

    return "\n".join(lines)
