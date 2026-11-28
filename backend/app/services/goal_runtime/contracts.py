"""Goal Contract：目标契约的确定性构建、版本与验收状态（任务书 Phase 1）。

设计约束：
- 复用 ``TaskContract`` 的完成检查项作为验收义务来源，不另造一套任务分类；
- 契约只描述“要验证什么”，具体执行仍由 DeepAgents / Team / Document 等执行器负责；
- 验收条件 ID 稳定（AC-001...），用户修改目标时递增 revision，不静默删除必需项；
- 进度由必需验收项确定性计算，模型不能直接写入。
"""
from __future__ import annotations

import re
import uuid
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Sequence

from sqlalchemy import select

from app.core.database import AsyncSessionLocal
from app.models.agent import AgentRunSteering
from app.models.goal import GoalContract

CRITERIA_STATUSES = ("pending", "in_progress", "passed", "failed", "blocked")
REQUIRED_STATUS_DONE = "passed"

# 完成检查项 → (验收描述, 验证方式)。只描述可验证义务，不复制执行细节。
_CHECK_CRITERIA: Dict[str, tuple[str, str]] = {
    "answer_present": ("交付完整的最终结论与正文", "semantic"),
    "citations_present": ("引用真实可核验的来源", "evidence"),
    "web_evidence": ("完成真实网络检索并留下检索证据", "evidence"),
    "artifact_produced": ("产出的交付文件已真实写入工作区", "artifact"),
    "db_evidence": ("通过数据库查询取得可核对证据", "evidence"),
    "side_effect_evidence": ("外部操作返回可核对回执", "evidence"),
    "action_evidence": ("实际操作留下执行证据", "evidence"),
    "delivery_path": ("交付文件位于用户可访问的工作区", "artifact"),
    "minimum_chars": ("最终正文达到用户要求的篇幅", "semantic"),
    "json_answer": ("最终回答是可解析的 JSON", "semantic"),
    "table_facts": ("表格统计结果与原始数据复算一致", "test"),
    "computation_evidence": ("完成必要的计算/统计过程", "test"),
    "exec_evidence": ("代码执行成功并留下结果", "test"),
}
_PROJECT_CRITERIA: Dict[str, tuple[str, str]] = {
    "runtime_start": ("项目可真实启动并正常响应", "browser"),
    "runtime_build": ("生产构建通过", "test"),
    "runtime_tests": ("随项目的测试套件通过", "test"),
    "runtime_browser": ("浏览器端验收通过", "browser"),
    "openapi": ("提供可用的 OpenAPI 文档", "artifact"),
    "archive": ("交付源码压缩包", "artifact"),
    "project_errors": ("工程验收无错误", "test"),
}
_PROCESS_CHECKS = frozenset({
    "plan_finished", "goal_plan_present", "goal_audit_present", "answer_complete",
    "tool_request_valid", "artifact_saved", "execution_interrupted",
    "research_plan_present", "research_evidence_present", "research_claims_verified",
    "research_conflicts_disclosed", "research_objective_coverage",
})
# 公开名称：这些检查不进入验收条件列表，但未通过时同样阻止目标完成
PROCESS_CHECKS = _PROCESS_CHECKS
_DEFAULT_STOP_CONDITIONS = (
    "达到全局安全预算（轮次/模型调用/工具调用/时长）时暂停并保留部分成果",
    "连续多轮无实质进展时暂停，避免空转",
    "需要用户提供关键输入或确认高风险操作时等待用户",
)
_TOKEN_SPLIT = re.compile(r"[^0-9a-zA-Z\u4e00-\u9fff]+")
_NEGATIVE_ROW = re.compile(r"未完成|未达成|未通过|未交付|受阻|失败|blocked|incomplete|failed", re.I)


def _now() -> str:
    return datetime.utcnow().isoformat()


def _new_criterion(
    description: str,
    verification_type: str = "semantic",
    *,
    required: bool = True,
    check_ids: Optional[Sequence[str]] = None,
    source: str = "task_contract",
) -> Dict[str, Any]:
    return {
        "id": "",  # 由 _assign_ids 统一分配
        "description": str(description).strip()[:500],
        "verification_type": verification_type if verification_type in {
            "test", "artifact", "browser", "semantic", "evidence", "manual",
        } else "semantic",
        "required": bool(required),
        "status": "pending",
        "check_ids": [str(item) for item in (check_ids or [])],
        "evidence_refs": [],
        "last_checked_at": None,
        "failure_reason": "",
        "next_step": "",
        "source": source,
    }


def _assign_ids(criteria: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    for index, item in enumerate(criteria, 1):
        item["id"] = f"AC-{index:03d}"
    return criteria


def _dedup(criteria: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for item in criteria:
        key = str(item.get("description") or "").strip()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def build_criteria(
    task_contract: Any = None,
    decision: Optional[Dict[str, Any]] = None,
    *,
    objective: str = "",
) -> List[Dict[str, Any]]:
    """用户目标 + TaskContract + 决策验收标准 → 稳定 ID 的验收条件。"""
    decision = decision or {}
    criteria: List[Dict[str, Any]] = []
    checks = list(getattr(task_contract, "completion_checks", None) or ["answer_present"])
    for check in checks:
        if check in _PROCESS_CHECKS:
            continue
        meta = _CHECK_CRITERIA.get(check)
        if meta:
            criteria.append(_new_criterion(meta[0], meta[1], check_ids=[check]))
    for check in getattr(task_contract, "project_checks", ()) or ():
        meta = _PROJECT_CRITERIA.get(check)
        if meta:
            criteria.append(_new_criterion(meta[0], meta[1], check_ids=[check]))
    artifact_paths = list(getattr(task_contract, "artifact_paths", ()) or ())
    artifact_formats = list(getattr(task_contract, "artifact_formats", ()) or ())
    if artifact_paths or artifact_formats:
        detail = "、".join(artifact_paths + [f".{item} 文件" for item in artifact_formats])
        criteria.append(_new_criterion(
            f"交付用户要求的文件（{detail[:300]}）", "artifact", check_ids=["artifact_produced"],
        ))
    for item in decision.get("acceptance_criteria") or []:
        text = str(item.get("description") if isinstance(item, dict) else item).strip()
        if text:
            verification = (
                str(item.get("verification_type") or "semantic")
                if isinstance(item, dict) else "semantic"
            )
            criteria.append(_new_criterion(
                text, verification,
                required=bool(item.get("required", True)) if isinstance(item, dict) else True,
                check_ids=(item.get("check_ids") or []) if isinstance(item, dict) else [],
                source="decision",
            ))
    if not criteria:
        criteria.append(_new_criterion("交付完整的最终结论与正文", "semantic",
                                       check_ids=["answer_present"]))
    # 用户目标的篇幅/格式等弱信号只在没有更强义务时兜底，避免给语义验收强加主观项
    _ = objective
    return _assign_ids(_dedup(criteria))


def _tokens(text: str) -> List[str]:
    raw = [part.casefold() for part in _TOKEN_SPLIT.split(str(text or "")) if len(part) >= 2]
    # 常见虚词/动词不作为覆盖证据（避免“完成/交付”这类词让任意验收项都算命中）
    stop = {"交付", "完成", "用户", "要求", "必须", "需要", "最终", "结果", "the", "and", "with"}
    return [part for part in raw if part not in stop][:24]


def semantic_covered(text: str, description: str) -> tuple[bool, str]:
    """用模型自己写的「目标核对」表做确定性覆盖检查（不额外调用模型）。

    只证明“交付说明覆盖了该验收项且未标注未完成”，不能替代确定性证据；
    有确定性检查项的验收条件仍以工具账本/产物检查为准。
    """
    from app.services.agent_runtime.goal import GOAL_AUDIT_HEADING

    body = str(text or "")
    if GOAL_AUDIT_HEADING not in body:
        return False, "最终回答缺少「目标核对」清单"
    audit = body.split(GOAL_AUDIT_HEADING, 1)[1]
    tokens = _tokens(description)
    if not tokens:
        return False, "验收项描述无法匹配目标核对"
    rows = [line for line in audit.splitlines() if line.strip()]
    for row in rows:
        if any(token in row.casefold() for token in tokens):
            if _NEGATIVE_ROW.search(row):
                return False, "目标核对中该验收项标注未完成/受阻"
            return True, ""
    return False, "目标核对未覆盖该验收项"


def evaluate_criteria(
    criteria: Sequence[Dict[str, Any]],
    checks: Dict[str, Dict[str, Any]],
    text: str,
) -> List[Dict[str, Any]]:
    """按验收检查结果与目标核对更新验收条件状态（确定性，不调用模型）。"""
    updated: List[Dict[str, Any]] = []
    now = _now()
    for raw in criteria:
        item = dict(raw) if isinstance(raw, dict) else {}
        if not item:
            continue
        check_ids = [str(cid) for cid in (item.get("check_ids") or [])]
        failure = ""
        passed = True
        evidence_refs: List[str] = []
        if check_ids:
            missing = [cid for cid in check_ids if cid not in checks]
            failed = [cid for cid in check_ids if cid in checks and checks[cid].get("passed") is not True]
            if missing:
                passed = False
                failure = "缺少验收检查结果：" + "、".join(missing[:4])
            elif failed:
                passed = False
                failure = "；".join(
                    str(checks[cid].get("detail") or cid) for cid in failed[:4]
                )
            for cid in check_ids:
                if checks.get(cid, {}).get("passed") is True:
                    evidence_refs.append(f"check:{cid}")
        elif item.get("verification_type") == "semantic":
            passed, failure = semantic_covered(text, str(item.get("description") or ""))
        else:
            # manual / 无确定性检查项：必须由人工或后续证据确认，不能自行判定通过
            passed = False
            failure = "该验收项需要人工确认或补充确定性证据"
        item["status"] = "passed" if passed else "failed"
        item["last_checked_at"] = now
        item["failure_reason"] = "" if passed else failure[:500]
        item["evidence_refs"] = evidence_refs
        updated.append(item)
    return updated


def progress_of(criteria: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    required = [item for item in criteria if item.get("required", True)]
    passed = [item for item in required if item.get("status") == "passed"]
    counts = {status: 0 for status in CRITERIA_STATUSES}
    for item in criteria:
        status = str(item.get("status") or "pending")
        counts[status] = counts.get(status, 0) + 1
    total = len(required)
    return {
        "percent": round(100 * len(passed) / total) if total else 100,
        "required_total": total,
        "required_passed": len(passed),
        "required_failed": total - len(passed),
        "optional_total": len(criteria) - total,
        "counts": counts,
        "updated_at": _now(),
    }


def blocking_criteria(criteria: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [
        item for item in criteria
        if item.get("required", True) and item.get("status") != "passed"
    ]


def contract_to_dict(row: GoalContract) -> Dict[str, Any]:
    return {
        "goal_id": row.goal_id,
        "run_id": row.run_id,
        "origin_run_id": row.origin_run_id,
        "session_id": row.session_id,
        "user_id": row.user_id,
        "agent_id": row.agent_id,
        "revision": int(row.revision or 1),
        "objective": row.objective or "",
        "acceptance_criteria": list(row.acceptance_criteria or []),
        "constraints": list(row.constraints or []),
        "deliverables": list(row.deliverables or []),
        "stop_conditions": list(row.stop_conditions or []),
        "revisions": list(row.revisions or []),
        "status": row.status or "active",
        "iterations": int(row.iterations or 0),
        "progress": dict(row.progress or {}),
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        "last_iteration_at": (
            row.last_iteration_at.isoformat() if row.last_iteration_at else None
        ),
        "finished_at": row.finished_at.isoformat() if row.finished_at else None,
    }


def _deliverables(task_contract: Any, decision: Dict[str, Any]) -> List[str]:
    items: List[str] = []
    items.extend(str(item) for item in (getattr(task_contract, "artifact_paths", ()) or ()))
    items.extend(f".{item}" for item in (getattr(task_contract, "artifact_formats", ()) or ()))
    items.extend(str(item) for item in (decision.get("deliverables") or []))
    return list(dict.fromkeys(item for item in items if item))[:24]


async def ensure_goal_contract(
    *,
    run_id: str,
    user_id: int,
    agent_id: Optional[int],
    session_id: Optional[int],
    objective: str,
    task_contract: Any = None,
    decision: Optional[Dict[str, Any]] = None,
    previous_run_id: Optional[str] = None,
) -> Dict[str, Any]:
    """创建目标契约；续做时复用上一 Run 的契约（goal_id 不变，run_id 更新）。"""
    decision = decision or {}
    async with AsyncSessionLocal() as db:
        # 崩溃恢复/HITL 恢复：同一 Run 重新进入执行时复用已持久化契约，
        # 不能把它标记为 superseded 后新建第二份目标。
        existing_run = (await db.execute(
            select(GoalContract).where(
                GoalContract.run_id == run_id,
                GoalContract.user_id == user_id,
            ).order_by(GoalContract.id.desc()).limit(1)
        )).scalar_one_or_none()
        if existing_run is not None:
            existing_run.session_id = session_id or existing_run.session_id
            existing_run.agent_id = agent_id or existing_run.agent_id
            existing_run.status = "active"
            existing_run.finished_at = None
            await db.commit()
            return contract_to_dict(existing_run)

        if previous_run_id:
            row = (await db.execute(
                select(GoalContract).where(
                    GoalContract.run_id == str(previous_run_id),
                    GoalContract.user_id == user_id,
                ).order_by(GoalContract.id.desc()).limit(1)
            )).scalar_one_or_none()
            if row is not None:
                row.run_id = run_id
                row.session_id = session_id or row.session_id
                row.agent_id = agent_id or row.agent_id
                row.status = "active"
                row.finished_at = None
                # 续做请求（如“继续”）不是新的目标文本：保留原始目标，
                # 用户要改目标必须走 revision / steering。
                if not (row.objective or "").strip() and objective.strip():
                    row.objective = objective.strip()
                await db.commit()
                return contract_to_dict(row)

        # 同一会话的新目标：旧的活动契约标记为已被取代，避免前端误认为仍在执行
        if session_id:
            actives = (await db.execute(
                select(GoalContract).where(
                    GoalContract.session_id == session_id,
                    GoalContract.user_id == user_id,
                    GoalContract.status == "active",
                )
            )).scalars().all()
            for active in actives:
                active.status = "superseded"
                active.finished_at = datetime.utcnow()

        now = datetime.utcnow()
        criteria = build_criteria(task_contract, decision, objective=objective)
        # 目标契约创建前已受理的补充要求（execution 尚未进入模型边界）：
        # 不能只注入消息而不进入契约，否则该要求永远不参与目标验收。
        early_steering = (await db.execute(
            select(AgentRunSteering.content)
            .where(AgentRunSteering.run_id == run_id)
            .order_by(AgentRunSteering.id)
        )).scalars().all()
        revisions = [{
            "revision": 1, "at": now.isoformat(), "source": "run_created",
            "summary": "创建目标契约", "changes": [],
        }]
        revision = 1
        for content in early_steering:
            text = str(content or "").strip()
            if not text:
                continue
            criteria.append(_new_criterion(
                f"执行中补充要求：{text[:400]}", "semantic",
                required=True, source="steering",
            ))
            revision += 1
            revisions.append({
                "revision": revision, "at": now.isoformat(), "source": "steering",
                "summary": "目标契约创建前受理的补充要求",
                "changes": [{"field": "steering", "content": text[:500], "impact": "新增语义验收项"}],
            })
        criteria = _assign_ids(_dedup(criteria))
        row = GoalContract(
            goal_id=uuid.uuid4().hex,
            run_id=run_id,
            origin_run_id=run_id,
            session_id=session_id,
            user_id=user_id,
            agent_id=agent_id,
            revision=revision,
            objective=(objective or "").strip(),
            acceptance_criteria=criteria,
            constraints=[str(item) for item in (decision.get("constraints") or [])][:20],
            deliverables=_deliverables(task_contract, decision),
            stop_conditions=list(_DEFAULT_STOP_CONDITIONS),
            revisions=revisions,
            status="active",
            iterations=0,
            progress=progress_of(criteria),
        )
        db.add(row)
        await db.commit()
        return contract_to_dict(row)


async def load_goal_contract(
    *, goal_id: Optional[str] = None, run_id: Optional[str] = None, user_id: int,
) -> Optional[Dict[str, Any]]:
    async with AsyncSessionLocal() as db:
        query = select(GoalContract).where(GoalContract.user_id == user_id)
        query = query.where(GoalContract.goal_id == goal_id) if goal_id else query.where(
            GoalContract.run_id == run_id
        )
        row = (await db.execute(query.order_by(GoalContract.id.desc()).limit(1))).scalar_one_or_none()
        return contract_to_dict(row) if row else None


def _merge_criteria(
    existing: Sequence[Dict[str, Any]], updates: Sequence[Any],
) -> List[Dict[str, Any]]:
    merged = [dict(item) for item in existing if isinstance(item, dict)]
    by_description = {str(item.get("description") or "").strip(): item for item in merged}
    for raw in updates:
        if isinstance(raw, dict):
            description = str(raw.get("description") or "").strip()
            verification = str(raw.get("verification_type") or "semantic")
            required = bool(raw.get("required", True))
            check_ids = [str(item) for item in (raw.get("check_ids") or [])]
        else:
            description = str(raw or "").strip()
            verification = "semantic"
            required = True
            check_ids = []
        if not description:
            continue
        if description in by_description:
            item = by_description[description]
            # 用户重新提交同一验收项：只重置状态，不重复建项
            item["status"] = "pending"
            item["last_checked_at"] = None
            item["failure_reason"] = ""
            item["required"] = required
            continue
        merged.append(_new_criterion(
            description, verification, required=required,
            check_ids=check_ids, source="user_revision",
        ))
    return _assign_ids(_dedup(merged))


async def revise_goal_contract(
    *,
    goal_id: str,
    user_id: int,
    objective: Optional[str] = None,
    acceptance_criteria: Optional[Sequence[Any]] = None,
    source: str = "api",
    note: str = "",
) -> Optional[Dict[str, Any]]:
    """用户修改目标/验收条件：revision 递增并重置受影响项状态。"""
    async with AsyncSessionLocal() as db:
        row = (await db.execute(
            select(GoalContract).where(
                GoalContract.goal_id == goal_id, GoalContract.user_id == user_id,
            ).with_for_update()
        )).scalar_one_or_none()
        if row is None:
            return None
        changes: List[Dict[str, Any]] = []
        if objective is not None and objective.strip() and objective.strip() != row.objective:
            changes.append({"field": "objective", "before": row.objective[:500],
                            "after": objective.strip()[:500]})
            row.objective = objective.strip()
        if acceptance_criteria is not None:
            before = {str(item.get("id")): item.get("status") for item in (row.acceptance_criteria or [])
                      if isinstance(item, dict)}
            merged = _merge_criteria(row.acceptance_criteria or [], acceptance_criteria)
            after = {str(item.get("id")): item.get("status") for item in merged}
            changes.append({
                "field": "acceptance_criteria",
                "before_count": len(before), "after_count": len(after),
            })
            row.acceptance_criteria = merged
        if not changes:
            return contract_to_dict(row)
        row.revision = int(row.revision or 1) + 1
        row.revisions = list(row.revisions or []) + [{
            "revision": row.revision, "at": _now(), "source": source,
            "summary": note or "用户修改目标", "changes": changes,
        }]
        row.progress = progress_of(row.acceptance_criteria or [])
        if row.status in {"completed", "blocked", "paused", "failed"}:
            row.status = "active"
            row.finished_at = None
        await db.commit()
        return contract_to_dict(row)


async def record_steering_revision(db, run_id: str, content: str, message_id: str) -> Optional[int]:
    """执行中补充要求 → 目标契约新 revision（在同一事务内调用）。

    不尝试用关键词猜测“补充/修改/取消”：任何执行中补充都可能影响已完成结果，
    因此统一递增 revision、把已通过项标记为待复核，并新增一条语义验收项，
    由下一轮目标核对证明该要求已被落实。
    """
    result = await db.execute(
        select(GoalContract).where(
            GoalContract.run_id == run_id, GoalContract.status == "active",
        ).order_by(GoalContract.id.desc()).limit(1).with_for_update()
    )
    contract = result.scalar_one_or_none()
    if contract is None:
        return None
    criteria = []
    for item in (contract.acceptance_criteria or []):
        if not isinstance(item, dict):
            continue
        entry = dict(item)
        if entry.get("status") == "passed":
            entry["status"] = "pending"
            entry["last_checked_at"] = None
        criteria.append(entry)
    description = f"执行中补充要求：{str(content).strip()[:400]}"
    if not any(str(item.get("description") or "") == description for item in criteria):
        criteria.append(_new_criterion(
            description, "semantic", required=True, source="steering",
        ))
    contract.acceptance_criteria = _assign_ids(criteria)
    contract.revision = int(contract.revision or 1) + 1
    contract.revisions = list(contract.revisions or []) + [{
        "revision": contract.revision, "at": _now(), "source": "steering",
        "summary": "执行中补充要求", "changes": [{
            "field": "steering", "message_id": message_id,
            "content": str(content)[:500], "impact": "已通过项重新验证",
        }],
    }]
    contract.progress = progress_of(contract.acceptance_criteria)
    return contract.revision


def contract_prompt(contract: Dict[str, Any]) -> str:
    """把持久化目标契约注入系统提示（供每轮执行与交付核对）。"""
    if not contract:
        return ""
    criteria = [item for item in (contract.get("acceptance_criteria") or [])
                if isinstance(item, dict)]
    lines = [
        "",
        "## 目标契约（平台持久化，修订版 v%s）" % contract.get("revision", 1),
        "以下验收条件由平台记录，最终交付必须逐项满足；未通过项会被退回下一轮。",
    ]
    for item in criteria:
        mark = "必需" if item.get("required", True) else "可选"
        lines.append(
            f"- [{item.get('id')}][{mark}][{item.get('verification_type')}] "
            f"{item.get('description')}"
        )
    if contract.get("stop_conditions"):
        lines.append("安全停止条件：")
        lines.extend(f"- {item}" for item in contract["stop_conditions"])
    return "\n".join(lines)
