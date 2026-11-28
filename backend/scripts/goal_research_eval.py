"""Goal Runtime / Deep Research 离线评测（任务书 Phase 7）。

只读取数据库中的目标契约、迭代、研究证据与 Run 账本，计算可重复的质量/成本指标；
不调用模型、不修改业务数据。指标口径与任务书 §10 对齐：

- Goal Completion Rate / False Completion Rate / Goal Coverage
- Recovery Success
- Research Coverage / Claim Support Rate / Citation Accuracy
- Source Read Rate / Conflict Detection Rate / Conflict Disclosure Rate
- Cost per Successful Task / Time to Completion

用法（backend/ 目录）：
    python -m scripts.goal_research_eval --report ../docs/goal-research-eval.md
"""
from __future__ import annotations

import argparse
import json
import re
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterable

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.agent import AgentRun
from app.models.goal import GoalContract, GoalIteration
from app.models.research import (
    ResearchClaim, ResearchEvidence, ResearchPlan, ResearchSource,
)

_URL = re.compile(r"https?://[^\s)）\"'<>，。；]+", re.I)
_CONFLICT_WORDS = ("冲突", "矛盾", "不一致", "分歧", "conflicting", "contradict")


def _json(value: Any, default: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return default
    return default


def _rate(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 4) if denominator else None


def citation_accuracy(texts: Iterable[str], read_urls: set[str]) -> dict[str, int]:
    """引用准确率：正文 URL 中确实登记为已读来源的比例。"""
    cited: set[str] = set()
    for text in texts:
        cited.update(match.rstrip(".,;]}`") for match in _URL.findall(text or ""))
    matched = {url for url in cited if url in read_urls}
    return {"cited": len(cited), "matched": len(matched)}


def _summary_cost(summary: Any) -> dict[str, int]:
    data = _json(summary, {}) or {}
    return {
        "model_calls": int(data.get("model_calls") or 0),
        "tool_calls": int(data.get("tool_calls") or 0),
        "tokens": int(data.get("tokens_generated") or 0),
    }


def build_metrics(
    *,
    goals: list[GoalContract],
    iterations: list[GoalIteration],
    runs: list[Any],
    plans: list[ResearchPlan],
    sources: list[ResearchSource],
    evidence: list[ResearchEvidence],
    claims: list[ResearchClaim],
    goal_texts: dict[str, list[str]] | None = None,
) -> dict[str, Any]:
    """由当前数据库状态计算指标（纯函数，便于单元测试）。"""
    goal_texts = goal_texts or {}
    runs_by_id = {row.id: row for row in runs}
    iteration_by_goal: dict[str, list[GoalIteration]] = defaultdict(list)
    for row in iterations:
        iteration_by_goal[row.goal_id].append(row)

    completed = [row for row in goals if row.status == "completed"]
    false_completed = []
    for row in completed:
        required = [
            item for item in _json(row.acceptance_criteria, [])
            if isinstance(item, dict) and item.get("required", True)
        ]
        if not required or any(item.get("status") != "passed" for item in required):
            false_completed.append(row.goal_id)

    goals_with_objective = [row for row in goals if (row.objective or "").strip()]
    goals_with_criteria = [
        row for row in goals
        if any(isinstance(item, dict) for item in _json(row.acceptance_criteria, []))
    ]

    recovery_runs = [row for row in runs if int(row.attempt or 0) > 1]
    recovery_success = [
        row for row in recovery_runs if row.status == "completed"
    ]
    # 跨 Run 续做（暂停/中断后“继续”）同样计入恢复成功率：goal_id 不变、
    # origin_run_id 与当前 run_id 不同即视为一次真实恢复。
    resumed_goals = [
        row for row in goals
        if (row.origin_run_id or "") and (row.origin_run_id or "") != (row.run_id or "")
    ]
    resumed_success = [row for row in resumed_goals if row.status == "completed"]
    recovery_attempts = len(recovery_runs) + len(resumed_goals)
    recovery_ok = len(recovery_success) + len(resumed_success)

    plan_ids = {row.research_id for row in plans}
    sources_by_plan: dict[str, list[ResearchSource]] = defaultdict(list)
    for row in sources:
        if row.research_id in plan_ids:
            sources_by_plan[row.research_id].append(row)
    evidence_by_plan: dict[str, list[ResearchEvidence]] = defaultdict(list)
    for row in evidence:
        if row.research_id in plan_ids:
            evidence_by_plan[row.research_id].append(row)
    claims_by_plan: dict[str, list[ResearchClaim]] = defaultdict(list)
    for row in claims:
        if row.research_id in plan_ids:
            claims_by_plan[row.research_id].append(row)

    high_questions = 0
    high_with_evidence = 0
    all_questions = 0
    all_with_evidence = 0
    for plan in plans:
        question_ids = {
            str(item.get("id")) for item in _json(plan.questions, [])
            if isinstance(item, dict) and item.get("status") != "cancelled"
        }
        evidence_questions = {
            str(row.question_id) for row in evidence_by_plan.get(plan.research_id, [])
            if row.question_id
        }
        for item in _json(plan.questions, []):
            if not isinstance(item, dict) or item.get("status") == "cancelled":
                continue
            all_questions += 1
            if item.get("priority") == "high":
                high_questions += 1
                if str(item.get("id")) in evidence_questions:
                    high_with_evidence += 1
            if str(item.get("id")) in evidence_questions and str(item.get("id")) in question_ids:
                all_with_evidence += 1

    total_sources = len(sources)
    read_sources = sum(1 for row in sources if row.status == "read")
    supported = sum(
        1 for row in claims if row.status in {"supported", "verified"}
    )
    conflicting = [row for row in claims if row.status == "conflicting"]
    contradictory_claims = [
        row for row in claims if _json(row.contradict_evidence, [])
    ]

    cited_total = 0
    cited_matched = 0
    for plan in plans:
        read_urls = {
            row.url for row in sources_by_plan.get(plan.research_id, [])
            if row.status == "read"
        }
        texts = goal_texts.get(plan.goal_id or "", [])
        result = citation_accuracy(texts, read_urls)
        cited_total += result["cited"]
        cited_matched += result["matched"]

    conflict_goals = {
        plan.goal_id for plan in plans
        if any(row.status == "conflicting" for row in claims_by_plan.get(plan.research_id, []))
    }
    disclosed = {
        goal_id for goal_id in conflict_goals
        if any(word in text for text in goal_texts.get(goal_id, [])
               for word in _CONFLICT_WORDS)
    }

    successful_run_ids: set[str] = set()
    successful_goal_ids = {row.goal_id for row in completed}
    for goal_id in successful_goal_ids:
        ids = {row.run_id for row in iteration_by_goal.get(goal_id, [])}
        ids.add(next((row.run_id for row in goals if row.goal_id == goal_id), ""))
        successful_run_ids.update(run_id for run_id in ids if run_id in runs_by_id)
    cost = {"model_calls": 0, "tool_calls": 0, "tokens": 0}
    for run_id in successful_run_ids:
        for key, value in _summary_cost(runs_by_id[run_id].summary).items():
            cost[key] += value

    durations = [
        (row.finished_at - row.created_at).total_seconds()
        for row in completed if row.finished_at and row.created_at
    ]

    return {
        "goals": {
            "total": len(goals),
            "completed": len(completed),
            "completion_rate": _rate(len(completed), len(goals)),
            "false_completions": len(false_completed),
            "false_completion_rate": _rate(len(false_completed), len(completed)),
            "objective_persisted_rate": _rate(len(goals_with_objective), len(goals)),
            "criteria_present_rate": _rate(len(goals_with_criteria), len(goals)),
            "avg_required_criteria": round(
                sum(
                    len([item for item in _json(row.acceptance_criteria, [])
                         if isinstance(item, dict) and item.get("required", True)])
                    for row in goals
                ) / len(goals), 2,
            ) if goals else 0,
        },
        "recovery": {
            "runs_with_retry": recovery_attempts,
            "retry_runs": len(recovery_runs),
            "resumed_goals": len(resumed_goals),
            "recovered_success": recovery_ok,
            "recovery_success_rate": _rate(recovery_ok, recovery_attempts),
        },
        "research": {
            "plans": len(plans),
            "questions": all_questions,
            "questions_with_evidence": all_with_evidence,
            "research_coverage": _rate(all_with_evidence, all_questions),
            "high_priority_questions": high_questions,
            "high_priority_with_evidence": high_with_evidence,
            "high_priority_coverage": _rate(high_with_evidence, high_questions),
            "sources": total_sources,
            "sources_read": read_sources,
            "source_read_rate": _rate(read_sources, total_sources),
            "claims": len(claims),
            "claims_supported": supported,
            "claim_support_rate": _rate(supported, len(claims)),
            "conflicting_claims": len(conflicting),
            "claims_with_contradiction": len(contradictory_claims),
            "conflict_detection_rate": _rate(
                len(conflicting), len(contradictory_claims)
            ),
            "conflict_goals": len(conflict_goals),
            "conflict_disclosed_goals": len(disclosed),
            "conflict_disclosure_rate": _rate(len(disclosed), len(conflict_goals)),
            "citations": cited_total,
            "citations_matched": cited_matched,
            "citation_accuracy": _rate(cited_matched, cited_total),
        },
        "cost": {
            "completed_goals": len(completed),
            **{key: value for key, value in cost.items()},
            "model_calls_per_success": _rate(cost["model_calls"], len(completed)),
            "tool_calls_per_success": _rate(cost["tool_calls"], len(completed)),
            "tokens_per_success": _rate(cost["tokens"], len(completed)),
            "avg_completion_seconds": round(sum(durations) / len(durations), 2)
            if durations else None,
        },
    }


def collect(session: Session, *, limit: int, user_id: int | None = None) -> dict[str, Any]:
    goal_query = select(GoalContract)
    if user_id is not None:
        goal_query = goal_query.where(GoalContract.user_id == user_id)
    goals = session.execute(
        goal_query.order_by(GoalContract.id.desc()).limit(limit)
    ).scalars().all()
    goal_ids = [row.goal_id for row in goals]

    iterations = session.execute(
        select(GoalIteration).where(GoalIteration.goal_id.in_(goal_ids))
    ).scalars().all() if goal_ids else []
    run_ids = {row.run_id for row in iterations} | {
        run_id for goal in goals for run_id in (goal.run_id, goal.origin_run_id) if run_id
    }
    run_rows = [
        SimpleNamespace(
            id=row.id, status=row.status, attempt=row.attempt, summary=row.summary,
            final_text=row.final_text, created_at=row.created_at,
            finished_at=row.finished_at,
        )
        for row in (session.execute(
            select(
                AgentRun.id, AgentRun.status, AgentRun.attempt, AgentRun.summary,
                AgentRun.final_text, AgentRun.created_at, AgentRun.finished_at,
            ).where(AgentRun.id.in_(run_ids))
        ).all() if run_ids else [])
    ]

    plans = session.execute(
        select(ResearchPlan).where(ResearchPlan.goal_id.in_(goal_ids))
    ).scalars().all() if goal_ids else []
    research_ids = [row.research_id for row in plans]
    sources = session.execute(
        select(ResearchSource).where(ResearchSource.research_id.in_(research_ids))
    ).scalars().all() if research_ids else []
    evidence = session.execute(
        select(ResearchEvidence).where(ResearchEvidence.research_id.in_(research_ids))
    ).scalars().all() if research_ids else []
    claims = session.execute(
        select(ResearchClaim).where(ResearchClaim.research_id.in_(research_ids))
    ).scalars().all() if research_ids else []

    runs_by_id = {row.id: row for row in run_rows}
    goal_texts: dict[str, list[str]] = defaultdict(list)
    for goal in goals:
        for iteration in iterations:
            if iteration.goal_id != goal.goal_id:
                continue
            row = runs_by_id.get(iteration.run_id)
            if row is not None and row.final_text:
                goal_texts[goal.goal_id].append(row.final_text)
        row = runs_by_id.get(goal.run_id)
        if row is not None and row.final_text:
            goal_texts[goal.goal_id].append(row.final_text)
    return build_metrics(
        goals=list(goals), iterations=list(iterations), runs=run_rows,
        plans=list(plans), sources=list(sources), evidence=list(evidence),
        claims=list(claims), goal_texts=goal_texts,
    )


def render_markdown(metrics: dict[str, Any]) -> str:
    goals = metrics["goals"]
    research = metrics["research"]
    cost = metrics["cost"]
    recovery = metrics["recovery"]
    lines = [
        "# Goal Runtime / Deep Research 评测报告",
        "",
        f"生成时间：{datetime.utcnow().isoformat()}Z",
        "",
        "## Goal",
        "",
        "| 指标 | 值 |",
        "| --- | --- |",
        f"| 目标总数 | {goals['total']} |",
        f"| Goal Completion Rate | {goals['completion_rate']} |",
        f"| False Completion Rate | {goals['false_completion_rate']}（{goals['false_completions']} 个） |",
        f"| 原始目标持久化率 | {goals['objective_persisted_rate']} |",
        f"| 验收条件存在率 | {goals['criteria_present_rate']} |",
        f"| 平均必需验收项 | {goals['avg_required_criteria']} |",
        f"| Recovery Success | {recovery['recovery_success_rate']}"
        f"（{recovery['recovered_success']}/{recovery['runs_with_retry']}；"
        f"重试 {recovery.get('retry_runs', 0)} / 跨 Run 续做 {recovery.get('resumed_goals', 0)}） |",
        "",
        "## Deep Research",
        "",
        "| 指标 | 值 |",
        "| --- | --- |",
        f"| Research Coverage（全部问题） | {research['research_coverage']} |",
        f"| Research Coverage（高优先级） | {research['high_priority_coverage']} |",
        f"| Source Read Rate | {research['source_read_rate']}（{research['sources_read']}/{research['sources']}） |",
        f"| Claim Support Rate | {research['claim_support_rate']}（{research['claims_supported']}/{research['claims']}） |",
        f"| Citation Accuracy | {research['citation_accuracy']}（{research['citations_matched']}/{research['citations']}） |",
        f"| Conflict Detection Rate | {research['conflict_detection_rate']}（{research['conflicting_claims']} 条冲突） |",
        f"| Conflict Disclosure Rate | {research['conflict_disclosure_rate']}（{research['conflict_disclosed_goals']}/{research['conflict_goals']}） |",
        "",
        "## 成本与耗时",
        "",
        "| 指标 | 值 |",
        "| --- | --- |",
        f"| 成功目标数 | {cost['completed_goals']} |",
        f"| 模型调用 / 成功目标 | {cost['model_calls_per_success']} |",
        f"| 工具调用 / 成功目标 | {cost['tool_calls_per_success']} |",
        f"| Token / 成功目标 | {cost['tokens_per_success']} |",
        f"| 平均完成耗时（秒） | {cost['avg_completion_seconds']} |",
        "",
        "> 指标由程序从数据库确定性计算，不采用执行 Agent 自评。",
    ]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Goal Runtime / Deep Research 离线评测")
    parser.add_argument("--limit", type=int, default=200, help="最近 N 个目标")
    parser.add_argument("--user-id", type=int, default=0, help="仅统计指定用户（0=全部）")
    parser.add_argument("--report", default="", help="Markdown 报告输出路径")
    parser.add_argument("--json", default="", help="JSON 指标输出路径")
    args = parser.parse_args()

    engine = create_engine(settings.DATABASE_SYNC_URL)
    try:
        with Session(engine) as session:
            metrics = collect(
                session, limit=max(1, args.limit),
                user_id=args.user_id or None,
            )
    finally:
        engine.dispose()

    print(json.dumps(metrics, ensure_ascii=False, indent=2, default=str))
    if args.report:
        path = Path(args.report)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(render_markdown(metrics), encoding="utf-8")
        print(f"报告已写入 {path}")
    if args.json:
        path = Path(args.json)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(metrics, ensure_ascii=False, indent=2, default=str),
                        encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
