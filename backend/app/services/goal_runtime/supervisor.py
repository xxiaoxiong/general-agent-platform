"""Goal Supervisor：外层目标监督循环（任务书 Phase 2）。

单轮 Agent 结束不等于整个 Goal 结束：
- 每轮收集结构化验收结果（GoalIteration），未通过项进入下一轮；
- 已通过项保留证据，避免无意义重复执行；
- 全局安全预算（轮次/模型/工具/时长）与停滞检测始终生效；
- 明确区分 completed / blocked / paused / failed，禁止把阻塞伪装成成功。

本模块只负责“目标是否达成”和目标状态持久化，不执行具体任务；执行仍由
现有 DeepAgents / Team / Document / Work Orchestrator 承担。
"""
from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy import select

from app.core.config import settings
from app.core.database import AsyncSessionLocal
from app.models.goal import GoalContract, GoalIteration

from .contracts import (
    PROCESS_CHECKS, blocking_criteria, evaluate_criteria, progress_of,
)

logger = logging.getLogger(__name__)

ACTION_COMPLETE = "complete"
ACTION_CONTINUE = "continue"
ACTION_PAUSED = "paused"
ACTION_BLOCKED = "blocked"


@dataclass
class GoalDecision:
    """一次目标验收的结构化结论（供 CompletionMiddleware 决定是否继续）。"""

    action: str = ACTION_COMPLETE
    reason: str = ""
    stop_reason: str = ""
    feedback: str = ""
    iteration: int = 0
    stall_count: int = 0
    goal: Dict[str, Any] = field(default_factory=dict)

    @property
    def blocking(self) -> bool:
        return self.action in (ACTION_CONTINUE, ACTION_PAUSED, ACTION_BLOCKED)


def _signature(failed_ids: List[str], criteria: List[Dict[str, Any]]) -> str:
    payload = {
        "failed": sorted(set(failed_ids)),
        "criteria": [
            [item.get("id"), item.get("status")] for item in criteria
        ],
    }
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False).encode()
    ).hexdigest()[:64]


def _blocking_feedback(criteria: List[Dict[str, Any]], process: List[Dict[str, Any]]) -> str:
    lines = ["目标契约验收仍未通过："]
    for item in criteria[:8]:
        reason = str(item.get("failure_reason") or "").strip()
        lines.append(
            f"- [{item.get('id')}] {item.get('description')}"
            + (f"：{reason}" if reason else "")
        )
    for item in process[:6]:
        lines.append(f"- [{item.get('id')}] {item.get('detail')}")
    lines.append("请先处理上述缺口再交付；已通过的验收项保留证据，不要重复无效操作。")
    return "\n".join(lines)


async def supervise_goal(
    *,
    goal_id: str,
    run_id: str,
    text: str,
    checks: Dict[str, Dict[str, Any]],
    failed_ids: List[str],
    previous_review: Optional[Dict[str, Any]] = None,
    plan: Optional[List[Dict[str, Any]]] = None,
    executor: str = "deep_agent",
    budget_reason: str = "",
) -> Optional[GoalDecision]:
    """评估当前轮目标达成情况，写入契约/迭代表并返回下一步动作。

    返回 ``None`` 表示目标契约不存在或持久化不可用：调用方降级到原有完成检查，
    不因目标运行时故障阻断正常任务。
    """
    previous_review = previous_review or {}
    try:
        async with AsyncSessionLocal() as db:
            row = (await db.execute(
                select(GoalContract).where(GoalContract.goal_id == goal_id)
                .with_for_update()
            )).scalar_one_or_none()
            if row is None:
                return None

            criteria = evaluate_criteria(row.acceptance_criteria or [], checks, text)
            blocking = blocking_criteria(criteria)
            # 过程检查（计划关闭/目标核对/正文完整/研究证据）不进入验收条件，
            # 但未通过时同样不能宣布目标完成。
            process_blocking = [
                {"id": check_id, "detail": str(checks[check_id].get("detail") or check_id)}
                for check_id in checks
                if check_id in PROCESS_CHECKS and checks[check_id].get("passed") is not True
            ]
            progress = progress_of(criteria)
            iteration_no = int(row.iterations or 0) + 1
            signature = _signature(failed_ids, criteria)
            previous_signature = str(previous_review.get("goal_signature") or "")
            stall_count = int(previous_review.get("goal_stall") or 0)
            stall_count = stall_count + 1 if signature and signature == previous_signature else 0
            max_iterations = max(1, int(getattr(settings, "GOAL_MAX_ITERATIONS", 12)))
            stall_limit = max(1, int(getattr(settings, "GOAL_STALL_LIMIT", 2)))

            decision = GoalDecision(iteration=iteration_no, stall_count=stall_count)
            if not blocking and not process_blocking:
                decision.action = ACTION_COMPLETE
                decision.reason = "全部必需验收条件通过"
                row.status = "completed"
                row.finished_at = datetime.utcnow()
            elif budget_reason:
                decision.action = ACTION_PAUSED
                decision.reason = budget_reason
                decision.stop_reason = "goal_budget"
                row.status = "paused"
            elif iteration_no >= max_iterations:
                decision.action = ACTION_PAUSED
                decision.reason = f"达到目标最大迭代轮次({max_iterations})"
                decision.stop_reason = "goal_rounds"
                row.status = "paused"
            elif stall_count >= stall_limit:
                decision.action = ACTION_BLOCKED
                decision.reason = (
                    f"连续 {stall_count} 轮没有实质进展，已暂停以避免空转"
                )
                decision.stop_reason = "goal_blocked"
                row.status = "blocked"
            else:
                decision.action = ACTION_CONTINUE
                decision.reason = "仍有未通过的必需验收条件或完成检查，进入下一轮"
                row.status = "active"
                decision.feedback = _blocking_feedback(blocking, process_blocking)

            row.acceptance_criteria = criteria
            row.progress = progress
            row.iterations = iteration_no
            row.last_iteration_at = datetime.utcnow()
            if row.status == "completed":
                row.finished_at = datetime.utcnow()

            iteration_status = {
                ACTION_COMPLETE: "completed",
                ACTION_CONTINUE: "continue",
                ACTION_PAUSED: "paused",
                ACTION_BLOCKED: "blocked",
            }[decision.action]
            iteration = (await db.execute(
                select(GoalIteration).where(
                    GoalIteration.goal_id == goal_id,
                    GoalIteration.iteration_no == iteration_no,
                )
            )).scalar_one_or_none()
            evidence_refs = [
                ref for item in criteria for ref in (item.get("evidence_refs") or [])
            ][:100]
            result = {
                "action": decision.action,
                "reason": decision.reason,
                "stop_reason": decision.stop_reason,
                "progress": progress,
                "blocking": [
                    {
                        "id": item.get("id"),
                        "description": item.get("description"),
                        "failure_reason": item.get("failure_reason"),
                    }
                    for item in blocking
                ] + [
                    {
                        "id": item.get("id"),
                        "description": item.get("detail"),
                        "failure_reason": item.get("detail"),
                    }
                    for item in process_blocking
                ],
            }
            if iteration is None:
                iteration = GoalIteration(
                    goal_id=goal_id, run_id=run_id, iteration_no=iteration_no,
                    status=iteration_status, executor=executor,
                    plan=list(plan or []), result=result,
                    evidence_refs=evidence_refs, stall_signature=signature,
                    started_at=row.created_at or datetime.utcnow(),
                    finished_at=datetime.utcnow(),
                )
                db.add(iteration)
            else:
                iteration.status = iteration_status
                iteration.run_id = run_id
                iteration.plan = list(plan or [])
                iteration.result = result
                iteration.evidence_refs = evidence_refs
                iteration.stall_signature = signature
                iteration.finished_at = datetime.utcnow()
            await db.commit()

            decision.goal = {
                "goal_id": row.goal_id,
                "revision": int(row.revision or 1),
                "iteration": iteration_no,
                "max_iterations": max_iterations,
                "status": {
                    ACTION_COMPLETE: "completed",
                    ACTION_CONTINUE: "active",
                    ACTION_PAUSED: "paused",
                    ACTION_BLOCKED: "blocked",
                }[decision.action],
                "action": decision.action,
                "reason": decision.reason,
                "progress": progress,
                "criteria": criteria,
                "blocking": result["blocking"],
                "signature": signature,
            }
            return decision
    except Exception:  # noqa: BLE001 目标运行时故障不得阻断正常任务
        logger.warning("goal %s 目标监督评估失败，降级到原完成检查", goal_id, exc_info=True)
        return None


async def complete_goal_contract(
    goal_id: str, *, evidence: str = "work_plan", run_id: str = "",
    plan: Optional[List[Dict[str, Any]]] = None,
    evidence_refs: Optional[List[str]] = None,
) -> None:
    """WorkPlan 等执行器已完成全部步骤验收时，同步目标契约为完成。

    同时记录执行器级迭代（executor=work_plan），保证“每轮有独立 iteration”
    与团队/DeepAgents 路径一致；已完成项保留执行器证据引用。
    """
    if not goal_id:
        return
    try:
        async with AsyncSessionLocal() as db:
            row = (await db.execute(
                select(GoalContract).where(GoalContract.goal_id == goal_id)
                .with_for_update()
            )).scalar_one_or_none()
            if row is None:
                return
            criteria = []
            for item in (row.acceptance_criteria or []):
                if not isinstance(item, dict):
                    continue
                entry = dict(item)
                entry["status"] = "passed"
                entry["last_checked_at"] = datetime.utcnow().isoformat()
                entry["failure_reason"] = ""
                entry["evidence_refs"] = list(dict.fromkeys(
                    [*(entry.get("evidence_refs") or []), f"executor:{evidence}"]
                ))
                criteria.append(entry)
            row.acceptance_criteria = criteria
            row.progress = progress_of(criteria)
            row.status = "completed"
            row.finished_at = datetime.utcnow()
            if run_id:
                iteration_no = int(row.iterations or 0) + 1
                row.iterations = iteration_no
                row.last_iteration_at = datetime.utcnow()
                db.add(GoalIteration(
                    goal_id=goal_id, run_id=run_id, iteration_no=iteration_no,
                    status="completed", executor=evidence,
                    plan=list(plan or []),
                    result={
                        "action": "complete",
                        "reason": "执行器已完成全部步骤验收",
                        "stop_reason": "",
                        "progress": row.progress,
                        "executor": evidence,
                        "evidence_refs": list(evidence_refs or [])[:100],
                    },
                    evidence_refs=list(evidence_refs or [])[:100],
                    started_at=row.created_at or datetime.utcnow(),
                    finished_at=datetime.utcnow(),
                ))
            await db.commit()
    except Exception:  # noqa: BLE001 收尾同步失败不影响 Run 终态
        logger.warning("goal %s 完成态同步失败（忽略）", goal_id, exc_info=True)


async def record_goal_iteration(
    *, goal_id: str, run_id: str, status: str, executor: str,
    plan: Optional[List[Dict[str, Any]]] = None,
    result: Optional[Dict[str, Any]] = None,
    evidence_refs: Optional[List[str]] = None,
) -> int:
    """记录执行器级目标迭代（WorkPlan 等非 CompletionMiddleware 路径）。"""
    if not goal_id:
        return 0
    try:
        async with AsyncSessionLocal() as db:
            row = (await db.execute(
                select(GoalContract).where(GoalContract.goal_id == goal_id)
                .with_for_update()
            )).scalar_one_or_none()
            if row is None:
                return 0
            iteration_no = int(row.iterations or 0) + 1
            row.iterations = iteration_no
            row.last_iteration_at = datetime.utcnow()
            db.add(GoalIteration(
                goal_id=goal_id, run_id=run_id, iteration_no=iteration_no,
                status=status, executor=executor,
                plan=list(plan or []),
                result=dict(result or {}),
                evidence_refs=list(evidence_refs or [])[:100],
                started_at=row.created_at or datetime.utcnow(),
                finished_at=datetime.utcnow(),
            ))
            await db.commit()
            return iteration_no
    except Exception:  # noqa: BLE001 迭代记录失败不影响 Run 终态
        logger.warning("goal %s 迭代记录失败（忽略）", goal_id, exc_info=True)
        return 0


async def mark_goal_status(
    goal_id: str, status: str, *, reason: str = "", executor: str = "",
    only_if_active: bool = False,
) -> None:
    """Run 收尾时同步目标状态（完成/取消/失败/暂停）。"""
    if not goal_id:
        return
    try:
        async with AsyncSessionLocal() as db:
            row = (await db.execute(
                select(GoalContract).where(GoalContract.goal_id == goal_id)
            )).scalar_one_or_none()
            if row is None:
                return
            if only_if_active and row.status != "active":
                return
            row.status = status
            if status in {"completed", "cancelled", "failed"}:
                row.finished_at = datetime.utcnow()
            if executor:
                row.last_iteration_at = datetime.utcnow()
            await db.commit()
    except Exception:  # noqa: BLE001 收尾状态同步失败不影响 Run 终态
        logger.warning("goal %s 收尾状态同步失败（忽略）", goal_id, exc_info=True)


def goal_payload_summary(decision: Optional[GoalDecision]) -> Dict[str, Any]:
    return decision.goal if decision is not None else {}
