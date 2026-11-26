"""测评运行器（Campaign / Trial）：执行、验证、评分与聚合。

执行模型（任务书 §4.1 / §4.3 / §4.8）：
- 每条 Trial 用独立 Session 起真实 AgentRun（start_run_with_session），消费旧契约
  事件流至 done；多轮用例在同 Session 内逐轮执行；每轮结束把进度写回 Trial，
  进程重启后可只重做未完成轮次，不重复已完成的 AgentRun；
- 执行 / 验证 / 评分 / 最终结果四类状态分离记录：
  execution_status / verification_status / grading_status / outcome；
- 确定性 Verifier 优先，LLM Judge 只补充主观质量；Verifier 硬失败不可被 Judge 覆盖；
  Judge 故障记为 inconclusive，Agent 结果保留；
- Campaig 使用数据库 Lease + Heartbeat 持久化执行，进程重启后由 Evaluation Worker
  重新领取；恢复时先检查已有 AgentRun 状态，终态直接评分，不重复执行；
- 评分聚合为纯客观数据（无 LLM、无主观判断），并输出 pass@k / pass^k、置信区间、
  能力维度分层与样本充分性信息。
"""
import asyncio
import hashlib
import json
import logging
import math
import uuid
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import AsyncSessionLocal
from app.models.agent import (
    Agent, AgentRun, AgentRunEvent, AgentToolExecution, AgentVersion,
)
from app.models.evaluation import (
    EvalDataset, EvalDatasetItem, EvalGrade, EvalRun, EvalRunItem,
    EvalSuite, EvalSuiteVersion,
)
from app.services.agent_runtime.events import TOOL_COMPLETED, TOOL_FAILED, TOOL_STARTED
from app.services.agent_runtime.lifecycle import STATUS_COMPLETED, TERMINAL_STATUSES

from .artifacts import collect_workspace_artifacts
from .capabilities import CAPABILITY_TEXT, resolve_capability
from .judge import judge_trial
from .scoring import ScoringError, resolve_method
from .suites import suite_dataset_ids
from .verifiers import VerifyContext, verify_output

logger = logging.getLogger(__name__)

# 用例执行默认超时（秒）：长程任务（长文档/工程构建/深度研究）需要更长执行时间
DEFAULT_ITEM_TIMEOUT_S = 300

# 复杂任务体系常量（与前端展示口径一致）
COMPLEXITY_LEVELS = ("L1", "L2", "L3", "L4")
COMPLEXITY_TEXT = {"L1": "简单", "L2": "中等", "L3": "复杂", "L4": "困难"}
TASK_TYPES = ("direct_qa", "multi_step_reasoning", "tool_use",
              "instruction_following", "multi_turn", "adversarial",
              "project_build", "long_document", "complex_math", "deep_research")

# 基础设施故障特征（仅用于把 AgentRun 失败区分为 infra_error；不参与评分）
_INFRA_ERROR_MARKERS = (
    "connection", "connect", "timeout", "timed out", "rate limit", "429",
    "500", "502", "503", "504", "sandbox", "network", "dns", "unavailable",
    "overloaded", "模型服务", "连接失败", "网络错误", "服务不可用",
)

# 交付物类工具：其成功调用的参数即 Agent 实际产出的交付物内容
_ARTIFACT_TOOLS = ("write_file", "edit_file", "python_exec", "execute")


def _now() -> datetime:
    return datetime.utcnow()


def _avg(vals: List[Any]) -> Optional[float]:
    vals = [v for v in vals if v is not None]
    return round(sum(vals) / len(vals), 3) if vals else None


def _percentile(values: List[float], ratio: float) -> Optional[float]:
    clean = sorted(float(v) for v in values if v is not None)
    if not clean:
        return None
    if len(clean) == 1:
        return round(clean[0], 2)
    pos = (len(clean) - 1) * ratio
    lower = math.floor(pos)
    upper = math.ceil(pos)
    if lower == upper:
        return round(clean[lower], 2)
    return round(clean[lower] + (clean[upper] - clean[lower]) * (pos - lower), 2)


def _wilson_interval(passed: int, total: int, z: float = 1.96) -> Optional[List[float]]:
    """通过率的 Wilson 置信区间（95%）；小样本不做过度推断。"""
    if total <= 0:
        return None
    p = passed / total
    denominator = 1 + z * z / total
    center = (p + z * z / (2 * total)) / denominator
    margin = z * math.sqrt((p * (1 - p) + z * z / (4 * total)) / total) / denominator
    return [round(max(0.0, center - margin), 4), round(min(1.0, center + margin), 4)]


# ────────────────────────────── 状态口径 ──────────────────────────────


def _outcome(item: EvalRunItem) -> str:
    """读取 Trial 最终结果；旧数据按 legacy status 兜底映射。

    旧数据无法区分 Judge 故障与 Agent 失败，统一归为 inconclusive（不计入
    Agent 成功率分母），避免把评分故障误算成 Agent 失败。
    """
    value = getattr(item, "outcome", None)
    if value and value != "pending":
        return str(value)
    status = (getattr(item, "status", None) or "pending").lower()
    return {
        "passed": "pass",
        "failed": "fail",
        "error": "inconclusive",
        "skipped": "cancelled",
        "unscored": "unscored",
    }.get(status, "pending")


def _legacy_status(outcome: str) -> str:
    return {
        "pass": "passed",
        "fail": "failed",
        "inconclusive": "error",
        "infra_error": "error",
        "cancelled": "skipped",
        "unscored": "unscored",
    }.get(outcome, "error")


def _execution_status_from_run(run_status: str, error_text: str = "") -> str:
    status = (run_status or "").lower()
    if status == STATUS_COMPLETED:
        return "succeeded"
    if status in ("failed", "limit_exceeded"):
        text = (error_text or "").lower()
        if any(marker in text for marker in _INFRA_ERROR_MARKERS):
            return "infra_error"
        return "agent_failed"
    if status in ("waiting_input", "waiting_approval"):
        # 测评环境不提供人工交互：Agent 未完成任务
        return "agent_failed"
    if status == "cancelled":
        return "cancelled"
    if status == "needs_reconciliation":
        return "infra_error"
    return "agent_failed"


def _outcome_from_execution(execution_status: str) -> str:
    return {
        "agent_failed": "fail",
        "timeout": "fail",
        "cancelled": "cancelled",
        "infra_error": "infra_error",
    }.get(execution_status, "inconclusive")


# ────────────────────────────── 聚合 ──────────────────────────────


def _layer_summary(rows: List[EvalRunItem]) -> Dict[str, Any]:
    """一组 Trial 的分层汇总（确定性计算）。"""
    outcomes = [_outcome(i) for i in rows]
    passed = outcomes.count("pass")
    failed = outcomes.count("fail")
    error = sum(1 for o in outcomes if o in ("inconclusive", "infra_error", "unscored"))
    skipped = outcomes.count("cancelled")
    completed = sum(1 for o in outcomes if o != "pending")
    graded = [i for i in rows if _outcome(i) in ("pass", "fail")]
    stats = [i.stats or {} for i in graded]
    durations = [s.get("duration_s") for s in stats if s.get("duration_s") is not None]
    return {
        "total": len(rows),
        "completed": completed,
        "passed": passed,
        "failed": failed,
        "error": error,
        "skipped": skipped,
        "pass_rate": round(passed / (passed + failed), 4) if (passed + failed) else None,
        "avg_score": _avg([i.score for i in graded]),
        "avg_steps": _avg([s.get("steps") for s in stats]),
        "avg_tool_calls": _avg([s.get("tool_calls") for s in stats]),
        "avg_duration_s": _avg([s.get("duration_s") for s in stats]),
        "avg_output_chars": _avg([s.get("output_chars") for s in stats]),
        "p50_duration_s": _percentile([float(d) for d in durations], 0.5),
        "p95_duration_s": _percentile([float(d) for d in durations], 0.95),
    }


def _case_key(item: EvalRunItem) -> Any:
    if item.dataset_item_id is not None:
        return item.dataset_item_id
    return ("pos", int(item.position or 0) - int(getattr(item, "trial_index", 0) or 0))


def _pass_k_metrics(items: List[EvalRunItem]) -> Dict[str, Any]:
    """pass@k / pass^k：同一 Case 重复 Trial 的成功与稳定性。"""
    groups: Dict[Any, List[EvalRunItem]] = {}
    for item in items:
        if _outcome(item) == "pending":
            continue
        groups.setdefault(_case_key(item), []).append(item)
    evaluated = {k: v for k, v in groups.items()
                 if any(_outcome(i) in ("pass", "fail") for i in v)}
    if not evaluated:
        return {"k": 0, "cases": 0, "pass_at_k": None, "pass_hat_k": None}
    k = max(len(v) for v in evaluated.values())
    any_pass = sum(1 for v in evaluated.values() if any(_outcome(i) == "pass" for i in v))
    all_pass = sum(1 for v in evaluated.values()
                   if v and all(_outcome(i) == "pass" for i in v))
    return {
        "k": k,
        "cases": len(evaluated),
        "pass_at_k": round(any_pass / len(evaluated), 4),
        "pass_hat_k": round(all_pass / len(evaluated), 4),
    }


def compute_aggregate(items: List[EvalRunItem]) -> Dict[str, Any]:
    """由 Trial 结果行聚合汇总（确定性计算，无 LLM、无主观判断）。

    除总体指标外，按复杂度 / 任务类型 / 能力维度分层，并汇总工具使用、
    稳定性（pass@k / pass^k）、置信区间与问题用例清单。
    """
    total = len(items)
    outcomes = [_outcome(i) for i in items]
    passed = outcomes.count("pass")
    failed = outcomes.count("fail")
    inconclusive = outcomes.count("inconclusive")
    infra_errors = outcomes.count("infra_error")
    unscored = outcomes.count("unscored")
    cancelled = outcomes.count("cancelled")
    completed = sum(1 for o in outcomes if o != "pending")
    graded = [i for i in items if _outcome(i) in ("pass", "fail")]
    stats = [i.stats or {} for i in graded]

    # 按复杂度 / 任务类型 / 能力维度分层
    by_complexity: Dict[str, Any] = {}
    for lvl in COMPLEXITY_LEVELS:
        rows = [i for i in items if (i.snap_complexity or "L2") == lvl]
        if rows:
            by_complexity[lvl] = _layer_summary(rows)
    by_task_type: Dict[str, Any] = {}
    for tt in sorted({(i.snap_task_type or "direct_qa") for i in items}):
        by_task_type[tt] = _layer_summary(
            [i for i in items if (i.snap_task_type or "direct_qa") == tt])
    by_capability: Dict[str, Any] = {}
    capabilities = {
        (getattr(i, "snap_capability", None)
         or resolve_capability(None, i.snap_task_type)) for i in items
    }
    for cap in sorted(capabilities):
        rows = [i for i in items if (
            getattr(i, "snap_capability", None)
            or resolve_capability(None, i.snap_task_type)) == cap]
        summary = _layer_summary(rows)
        summary["name"] = CAPABILITY_TEXT.get(cap, cap)
        # 证据不足时前端显示「尚未验证」而不是强行给能力等级
        summary["evidence_sufficient"] = summary["passed"] + summary["failed"] >= 3
        by_capability[cap] = summary

    # 工具使用统计（跨用例汇总）
    tool_usage: Dict[str, Dict[str, int]] = {}
    zero_tool = 0
    multi_step = 0
    limit_exceeded = 0
    missing_required = 0
    tool_total_all = 0
    tool_failed_all = 0
    cons_total = 0
    cons_passed = 0
    cons_items = 0
    tool_failure_trials = 0
    recovered_trials = 0
    for i in graded:
        s = i.stats or {}
        d = i.score_detail or {}
        if d.get("method") == "constraint":
            cons_items += 1
            cons_total += int(d.get("total_constraints") or 0)
            cons_passed += int(d.get("passed_constraints") or 0)
        if int(s.get("tool_failed") or 0) > 0 or any(
                str(c.get("status") or "") != "succeeded"
                for c in (s.get("tool_chain") or [])):
            tool_failure_trials += 1
            if _outcome(i) == "pass":
                recovered_trials += 1
        for chain in (s.get("tool_chain") or []):
            name = str(chain.get("tool_name") or "?")
            u = tool_usage.setdefault(name, {"calls": 0, "failed": 0})
            u["calls"] += 1
            tool_total_all += 1
            if chain.get("status") != "succeeded":
                u["failed"] += 1
                tool_failed_all += 1
        if not s.get("tool_calls"):
            zero_tool += 1
        if int(s.get("steps") or 0) >= 2:
            multi_step += 1
        if s.get("limit_exceeded"):
            limit_exceeded += 1
        if s.get("missing_tools"):
            missing_required += 1

    # 问题用例清单（纯客观事实）
    problem_items: List[Dict[str, Any]] = []
    for i in items:
        s = i.stats or {}
        problems: List[str] = []
        if i.status == "error":
            problems.append("error")
        elif i.status == "failed":
            problems.append("failed")
        elif i.status == "unscored":
            problems.append("unscored")
        if s.get("missing_tools"):
            problems.append("missing_required_tools")
        if (i.snap_meta or {}).get("expected_min_steps") and i.status in (
                "passed", "failed") and not s.get("steps_met", True):
            problems.append("below_expected_steps")
        if problems:
            problem_items.append({
                "item_id": i.id,
                "position": i.position + 1,
                "status": i.status,
                "outcome": _outcome(i),
                "execution_status": getattr(i, "execution_status", None),
                "verification_status": getattr(i, "verification_status", None),
                "grading_status": getattr(i, "grading_status", None),
                "complexity": i.snap_complexity or "L2",
                "task_type": i.snap_task_type or "direct_qa",
                "capability": (getattr(i, "snap_capability", None)
                               or resolve_capability(None, i.snap_task_type)),
                "problems": problems,
                "score": i.score,
                "steps": s.get("steps"),
                "tool_calls": s.get("tool_calls"),
                "output_chars": s.get("output_chars"),
                "used_tools": s.get("used_tools") or [],
                "missing_tools": s.get("missing_tools") or [],
                "error": (i.error or "")[:200] or None,
            })

    durations = [s.get("duration_s") for s in stats if s.get("duration_s") is not None]
    verification_executed = sum(
        1 for i in items
        if (getattr(i, "verification_status", None) or "") in ("passed", "failed", "error"))
    succeeded_executed = sum(
        1 for i in items if (getattr(i, "execution_status", None) or "") == "succeeded")
    effective = passed + failed
    pass_k = _pass_k_metrics(items)
    case_total = int(pass_k.get("cases") or 0)
    case_passed = round(float(pass_k.get("pass_at_k") or 0.0) * case_total)

    return {
        "total": total,
        "completed": completed,
        "passed": passed,
        "failed": failed,
        "error": inconclusive + infra_errors + unscored,
        "skipped": cancelled,
        "pending": total - completed,
        # Agent 成功率分母只包含 Agent 有明确表现的 Trial（排除评分/基建故障）
        "pass_rate": round(passed / effective, 4) if effective else None,
        # 独立 Case 口径（重复 Trial 不重复计为独立样本）
        "effective_sample": effective,
        "effective_cases": case_total,
        "case_pass_rate": pass_k.get("pass_at_k"),
        "avg_score": _avg([i.score for i in graded]),
        "avg_duration_s": _avg([s.get("duration_s") for s in stats]),
        "total_tokens": sum(int(s.get("tokens") or 0) for s in stats),
        "total_tool_calls": sum(int(s.get("tool_calls") or 0) for s in stats),
        # ── 结果口径分解（纯客观） ──
        "outcomes": {
            "pass": passed, "fail": failed, "inconclusive": inconclusive,
            "infra_error": infra_errors, "cancelled": cancelled,
            "unscored": unscored, "pending": total - completed,
        },
        "execution": {
            "succeeded": succeeded_executed,
            "agent_failed": sum(1 for i in items
                                if (getattr(i, "execution_status", None) or "") == "agent_failed"),
            "timeout": sum(1 for i in items
                           if (getattr(i, "execution_status", None) or "") == "timeout"),
            "infra_error": sum(1 for i in items
                               if (getattr(i, "execution_status", None) or "") == "infra_error"),
            "cancelled": sum(1 for i in items
                             if (getattr(i, "execution_status", None) or "") == "cancelled"),
        },
        "verification": {
            "executed": verification_executed,
            "coverage": round(verification_executed / succeeded_executed, 4)
            if succeeded_executed else None,
        },
        # 置信区间按独立 Case 计算（95% Wilson），避免重复 Trial 虚增样本量
        "confidence_interval": _wilson_interval(case_passed, case_total),
        "pass_k": pass_k,
        "p50_duration_s": _percentile([float(d) for d in durations], 0.5),
        "p95_duration_s": _percentile([float(d) for d in durations], 0.95),
        "cost_per_success_tokens": round(
            sum(int(s.get("tokens") or 0) for s in stats) / passed, 2) if passed else None,
        # ── Agent 能力画像（纯客观） ──
        "by_complexity": by_complexity,
        "by_task_type": by_task_type,
        "by_capability": by_capability,
        "tool_usage": tool_usage,
        "capability": {
            "avg_steps": _avg([s.get("steps") for s in stats]),
            "max_steps": max([int(s.get("steps") or 0) for s in stats], default=0),
            "multi_step_count": multi_step,
            "zero_tool_count": zero_tool,
            "tool_error_rate": round(tool_failed_all / tool_total_all, 4)
            if tool_total_all else 0.0,
            "missing_required_tools_count": missing_required,
            "limit_exceeded_count": limit_exceeded,
            "avg_output_chars": _avg([s.get("output_chars") for s in stats]),
            "max_output_chars": max(
                [int(s.get("output_chars") or 0) for s in stats], default=0),
            "constraint_items": cons_items,
            "constraint_total": cons_total,
            "constraint_passed": cons_passed,
            "constraint_ratio": round(cons_passed / cons_total, 4)
            if cons_total else None,
            # 异常恢复（客观）：出现工具失败但仍通过任务的 Trial 比例
            "tool_failure_trials": tool_failure_trials,
            "recovered_trials": recovered_trials,
            "recovery_rate": round(recovered_trials / tool_failure_trials, 4)
            if tool_failure_trials else None,
        },
        "problem_items": problem_items[:100],
    }


async def _refresh_aggregate(db: AsyncSession, eval_run_id: int) -> Dict[str, Any]:
    items = (await db.execute(
        select(EvalRunItem).where(EvalRunItem.eval_run_id == eval_run_id)
    )).scalars().all()
    agg = compute_aggregate(list(items))
    await db.execute(
        update(EvalRun).where(EvalRun.id == eval_run_id).values(aggregate=agg))
    await db.commit()
    return agg


# ────────────────────────────── 创建 Campaign ──────────────────────────────


def _snapshot_digest(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _environment_snapshot(
    agent: Agent,
    version: Optional[AgentVersion],
    *,
    suite_version: Optional[EvalSuiteVersion],
    dataset_ids: List[int],
    config: Dict[str, Any],
    model_info: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    snapshot = dict((version.snapshot if version is not None else {}) or {})
    return {
        "agent_id": agent.id,
        "agent_name": agent.name,
        "agent_version_id": version.id if version is not None else None,
        "agent_version_no": (version.version_no if version is not None else None),
        "agent_snapshot_digest": _snapshot_digest(snapshot),
        "model_policy": snapshot.get("model_policy") or {},
        "model": model_info or {},
        "tools": list(snapshot.get("tools") or agent.tools or []),
        "capability_ids": list(snapshot.get("capability_ids") or []),
        "skill_sources": list(snapshot.get("skill_sources") or []),
        "subagent_profiles": list(snapshot.get("subagent_profiles") or []),
        "memory_policy": snapshot.get("memory_policy") or {},
        "resource_bindings": snapshot.get("resource_bindings") or {},
        "security_policy": snapshot.get("security_policy") or {},
        "runtime": {
            "app_version": getattr(settings, "APP_VERSION", ""),
            "agent_framework": "deepagents+langgraph",
        },
        "suite": {
            "suite_id": suite_version.suite_id,
            "suite_version_id": suite_version.id,
            "version_no": suite_version.version_no,
            "digest": (suite_version.snapshot or {}).get("digest"),
        } if suite_version is not None else None,
        "dataset_ids": list(dataset_ids),
        "config": dict(config or {}),
        "pinned_at": _now().isoformat(),
    }


async def _model_snapshot(
    db: AsyncSession, model_config_id: Optional[int],
) -> Dict[str, Any]:
    """固定模型 Provider / 模型名称（可获取的模型版本信息）。"""
    if not model_config_id:
        return {}
    from app.models.model_provider import ModelConfig, ModelProvider

    mc = (await db.execute(
        select(ModelConfig).where(ModelConfig.id == int(model_config_id))
    )).scalar_one_or_none()
    if mc is None:
        return {"model_config_id": model_config_id, "resolved": False}
    provider = (await db.execute(
        select(ModelProvider).where(ModelProvider.id == mc.provider_id)
    )).scalar_one_or_none()
    return {
        "model_config_id": mc.id,
        "model_config_name": mc.name,
        "model_display_name": mc.display_name,
        "provider_id": mc.provider_id,
        "provider_name": provider.name if provider is not None else None,
        "provider_type": getattr(provider, "provider_type", None) if provider else None,
        "resolved": True,
    }


def _suite_version_items(
    suite_version: EvalSuiteVersion,
) -> List[Tuple[int, Dict[str, Any]]]:
    """展开套件版本快照为 [(dataset_id, item_snapshot)]。"""
    out: List[Tuple[int, Dict[str, Any]]] = []
    for dataset in (suite_version.snapshot or {}).get("datasets") or []:
        dataset_id = int(dataset.get("dataset_id"))
        for item in dataset.get("items") or []:
            out.append((dataset_id, dict(item)))
    return out


async def create_eval_run(
    db: AsyncSession,
    *,
    name: str,
    dataset_id: Optional[int] = None,
    agent_id: int,
    created_by: Optional[int],
    config: Optional[Dict[str, Any]] = None,
    suite_id: Optional[int] = None,
    suite_version_id: Optional[int] = None,
    agent_version_id: Optional[int] = None,
    trials_per_case: int = 1,
) -> EvalRun:
    """创建测评任务（Campaign）与 Trial 快照。

    - 数据集模式：dataset_id 必填，使用数据集当前内容；
    - 套件模式：suite_id / suite_version_id，使用固定版本快照（多数据集）；
    - trials_per_case：同一 Case 的重复 Trial 数（稳定性评估）；
    - agent_version_id：显式固定被测版本；未指定时固定当前最新版本（不再漂移）。
    """
    agent = (await db.execute(
        select(Agent).where(Agent.id == agent_id))).scalar_one_or_none()
    if agent is None:
        raise ValueError("Agent 不存在")

    config = dict(config or {})
    trials_per_case = max(1, min(10, int(trials_per_case or 1)))
    limit = int(config.get("limit") or 0)

    # 固定被测 Agent 版本（P0）：创建时解析一次，执行期只读快照
    from app.services.assistant.versions import ensure_version

    version: Optional[AgentVersion] = None
    if agent_version_id:
        version = (await db.execute(
            select(AgentVersion).where(
                AgentVersion.id == int(agent_version_id),
                AgentVersion.agent_id == agent_id,
            )
        )).scalar_one_or_none()
        if version is None:
            raise ValueError("指定的 AgentVersion 不存在或不属于该 Agent")
    else:
        version = await ensure_version(db, agent_id, created_by=created_by)

    cases: List[Tuple[int, Dict[str, Any]]] = []
    dataset_defaults: Dict[int, str] = {}
    suite_version: Optional[EvalSuiteVersion] = None
    dataset_ids: List[int] = []
    if suite_id or suite_version_id:
        if suite_version_id:
            suite_version = (await db.execute(
                select(EvalSuiteVersion).where(EvalSuiteVersion.id == int(suite_version_id))
            )).scalar_one_or_none()
            if suite_version is None:
                raise ValueError("套件版本不存在")
            if suite_id and suite_version.suite_id != int(suite_id):
                raise ValueError("套件版本不属于指定套件")
        else:
            suite = (await db.execute(
                select(EvalSuite).where(EvalSuite.id == int(suite_id))
            )).scalar_one_or_none()
            if suite is None:
                raise ValueError("套件不存在")
            from .suites import ensure_suite_version

            suite_version = await ensure_suite_version(
                db, suite.id, created_by=created_by)
        for dataset in (suite_version.snapshot or {}).get("datasets") or []:
            dataset_defaults[int(dataset.get("dataset_id"))] = str(
                dataset.get("scoring_method") or "llm_judge")
        cases = _suite_version_items(suite_version)
        dataset_ids = suite_dataset_ids(suite_version)
        if not dataset_ids:
            raise ValueError("套件没有数据集")
        dataset_id = dataset_ids[0]
    else:
        if not dataset_id:
            raise ValueError("dataset_id 或 suite_id 至少提供一个")
        dataset = (await db.execute(
            select(EvalDataset).where(EvalDataset.id == dataset_id))).scalar_one_or_none()
        if dataset is None:
            raise ValueError("数据集不存在")
        dataset_defaults[dataset.id] = dataset.scoring_method or "llm_judge"
        stmt = select(EvalDatasetItem).where(
            EvalDatasetItem.dataset_id == dataset_id
        ).order_by(EvalDatasetItem.position, EvalDatasetItem.id)
        items = (await db.execute(stmt)).scalars().all()
        dataset_ids = [dataset_id]
        cases = [(dataset_id, {
            "id": it.id,
            "position": int(it.position or 0),
            "input": it.input or "",
            "expected": it.expected or "",
            "criteria": it.criteria or "",
            "scoring_method": it.scoring_method,
            "scoring_config": dict(it.scoring_config or {}),
            "complexity": it.complexity or "L2",
            "task_type": it.task_type or "direct_qa",
            "capability": resolve_capability(it.capability, it.task_type),
            "required_tools": list(it.required_tools or []),
            "expected_min_steps": it.expected_min_steps,
            "turns": list(it.turns or []),
            "metadata": dict(it.item_metadata or {}),
        }) for it in items]

    if limit > 0:
        cases = cases[:limit]
    if not cases:
        raise ValueError("数据集/套件没有用例")

    model_config_id = None
    if version is not None:
        model_config_id = ((version.snapshot or {}).get("model_policy") or {}).get(
            "model_config_id")
    if not model_config_id:
        model_config_id = agent.model_config_id
    model_info = await _model_snapshot(db, model_config_id)

    eval_run = EvalRun(
        name=name,
        dataset_id=dataset_id,
        suite_id=suite_version.suite_id if suite_version is not None else None,
        suite_version_id=suite_version.id if suite_version is not None else None,
        agent_id=agent_id,
        agent_version_id=version.id if version is not None else None,
        status="pending",
        config=config,
        environment=_environment_snapshot(
            agent, version, suite_version=suite_version,
            dataset_ids=dataset_ids, config=config, model_info=model_info),
        aggregate={"total": len(cases) * trials_per_case, "completed": 0,
                   "passed": 0, "failed": 0, "error": 0, "skipped": 0,
                   "pending": len(cases) * trials_per_case,
                   "pass_rate": None, "avg_score": None, "avg_duration_s": None,
                   "total_tokens": 0, "total_tool_calls": 0},
        trials_per_case=trials_per_case,
        created_by=created_by,
    )
    db.add(eval_run)
    await db.flush()
    case_index = 0
    for case_dataset_id, snap in cases:
        default_method = dataset_defaults.get(case_dataset_id, "llm_judge")
        for trial_index in range(trials_per_case):
            db.add(EvalRunItem(
                eval_run_id=eval_run.id,
                dataset_item_id=snap.get("id"),
                dataset_id=case_dataset_id,
                position=case_index * trials_per_case + trial_index,
                trial_index=trial_index,
                snap_input=snap.get("input") or "",
                snap_expected=snap.get("expected") or "",
                snap_criteria=snap.get("criteria") or "",
                snap_scoring_method=resolve_method(
                    snap.get("scoring_method"), default_method),
                snap_scoring_config=dict(snap.get("scoring_config") or {}),
                snap_complexity=snap.get("complexity") or "L2",
                snap_task_type=snap.get("task_type") or "direct_qa",
                snap_capability=snap.get("capability") or resolve_capability(
                    None, snap.get("task_type")),
                snap_meta={
                    "required_tools": list(snap.get("required_tools") or []),
                    "expected_min_steps": snap.get("expected_min_steps"),
                    "turns": list(snap.get("turns") or []),
                    "metadata": dict(snap.get("metadata") or {}),
                },
                status="pending",
                execution_status="pending",
                verification_status="pending",
                grading_status="pending",
                outcome="pending",
            ))
        case_index += 1
    await db.commit()
    await db.refresh(eval_run)
    return eval_run


async def _build_agent_config(
    db: AsyncSession, agent: Agent, *, use_memory: bool,
    agent_version_id: Optional[int] = None,
) -> Dict[str, Any]:
    """由固定 AgentVersion 构建运行配置（版本固定，不回落到最新版本）。"""
    from app.services.assistant.versions import config_from_version, ensure_version

    version: Optional[AgentVersion] = None
    if agent_version_id:
        version = (await db.execute(
            select(AgentVersion).where(
                AgentVersion.id == int(agent_version_id),
                AgentVersion.agent_id == agent.id,
            )
        )).scalar_one_or_none()
        if version is None:
            # 固定版本缺失时失败，绝不静默回落到最新版本（否则版本漂移）
            raise RuntimeError(
                f"Campaign 固定的 AgentVersion 不存在: {agent_version_id}")
    if version is None:
        version = await ensure_version(db, agent.id)
    if version is not None:
        cfg = dict(config_from_version(version))
    else:
        cfg = {
            "id": agent.id,
            "system_prompt": agent.system_prompt or "",
            "model_config_id": agent.model_config_id,
            "temperature": agent.temperature,
            "max_iterations": agent.max_iterations,
            "max_tokens": agent.max_tokens,
            "tools": list(agent.tools or []),
            "capability_ids": list(getattr(agent, "capability_ids", None) or []),
            "knowledge_base_id": agent.knowledge_base_id,
            "database_connection_id": agent.database_connection_id,
        }
    cfg["name"] = agent.name
    cfg["memory_policy"] = {"use_memory_context": bool(use_memory)}
    if version is not None:
        cfg["agent_version_id"] = version.id
    return cfg


# ────────────────────────────── 事实采集（只读） ──────────────────────────────


async def _drain_stream(stream) -> None:
    """消费旧契约事件流至 done（执行由 Runtime Worker 驱动）。"""
    async for event in stream:
        if isinstance(event, dict) and event.get("type") == "done":
            break


async def _cancel_unfinished_run(runtime, run_id: str, user_id: int) -> str:
    """结束测评拥有的未完成 Run，避免离开事件流后 Worker 继续消耗预算。"""
    try:
        result = await runtime.cancel_run(run_id, user_id)
        if result.get("ok"):
            return str(result["status"])
    except Exception:
        logger.exception("测评取消运行失败 run_id=%s", run_id)
    return "unknown"


async def _collect_event_tool_calls(
    db: AsyncSession, run_ids: List[str],
) -> List[Dict[str, Any]]:
    """从运行事件日志采集工具调用事实（含不经账本的内部工具），按事件序号排序。

    deepagents 内部工具（write_file / write_todos / task 等）不经
    ToolAdapterFactory、不落 agent_tool_executions 账本，只在 agent_run_events
    有 TOOL 事件——工具链统计与交付物采集必须合并事件口径，否则内部工具调用
    整体漏报。返回元素（同 tool_call_id 的 started/completed/failed 合并一条，
    终态后者覆盖）：
    {run_id, tool_call_id, tool_name, status, internal, args, error, seq}，
    仅有 started 无终态的调用 status=unknown（结果未确认）。
    """
    if not run_ids:
        return []
    ev_rows = (await db.execute(
        select(AgentRunEvent.run_id, AgentRunEvent.sequence,
               AgentRunEvent.event_type, AgentRunEvent.payload)
        .where(AgentRunEvent.run_id.in_(run_ids))
        .where(AgentRunEvent.event_type.in_(
            (TOOL_STARTED, TOOL_COMPLETED, TOOL_FAILED)))
    )).all()
    merged: Dict[tuple, Dict[str, Any]] = {}
    order: List[tuple] = []
    for row_run_id, seq, etype, payload in sorted(
            ev_rows, key=lambda r: (r.run_id, int(r.sequence))):
        p = dict(payload or {})
        tcid = str(p.get("tool_call_id") or "")
        key = (row_run_id, tcid or f"ev:{seq}")
        call = merged.get(key)
        if call is None:
            call = {
                "run_id": row_run_id, "tool_call_id": tcid, "seq": int(seq),
                "tool_name": str(p.get("tool") or "?"),
                "status": "unknown",
                "internal": bool(p.get("internal")),
                "args": p.get("input"),
            }
            merged[key] = call
            order.append(key)
        if p.get("tool"):
            call["tool_name"] = str(p["tool"])
        if etype == TOOL_STARTED:
            if p.get("input") is not None:
                call["args"] = p.get("input")
        elif etype == TOOL_COMPLETED:
            call["status"] = "succeeded"
            if p.get("output") is not None:
                call["result"] = str(p.get("output"))[:1000]
        elif etype == TOOL_FAILED:
            call["status"] = "failed"
            if p.get("error"):
                call["error"] = str(p["error"])[:200]
    return [merged[k] for k in order]


async def _collect_run_facts(db: AsyncSession, run_id: str) -> Dict[str, Any]:
    """采集单 Run 的客观执行事实（工具链/步数/tokens 等，只读事实表）。

    工具链双口径合并：账本（agent_tool_executions，平台工具，权威状态/耗时/参数）
    + 事件日志（agent_run_events，含 write_file 等内部工具与未入账本的拒绝调用），
    同 tool_call_id 去重——否则内部工具调用整体漏报，工具链/used_tools/成功率失真。
    排序：有事件序号的调用按真实执行顺序（事件序号），无事件的账本行按开始时间殿后。
    """
    run = (await db.execute(
        select(AgentRun).where(AgentRun.id == run_id))).scalar_one_or_none()
    if run is None:
        raise RuntimeError(f"AgentRun 未找到: {run_id}")
    rows = (await db.execute(
        select(AgentToolExecution).where(AgentToolExecution.run_id == run_id)
    )).scalars().all()
    rows = sorted(rows, key=lambda r: (r.started_at or r.created_at or datetime.min))
    ev_calls = await _collect_event_tool_calls(db, [run_id])
    ev_by_id = {c["tool_call_id"]: c for c in ev_calls if c["tool_call_id"]}

    ordered: List[tuple] = []
    matched: set = set()
    for r in rows:
        entry: Dict[str, Any] = {
            "tool_name": r.tool_name,
            "status": r.status,
            "attempt_count": int(r.attempt_count or 0),
        }
        if r.started_at and r.finished_at:
            try:
                entry["duration_s"] = round(
                    (r.finished_at - r.started_at).total_seconds(), 2)
            except Exception:  # noqa: BLE001
                pass
        if r.error:
            entry["error"] = str(r.error)[:200]
        if r.args_snapshot:
            entry["args"] = str(r.args_snapshot)[:200]
        if r.result_payload is not None:
            entry["result"] = str(r.result_payload)[:1000]
        ev = ev_by_id.get(r.tool_call_id or "")
        if ev is not None:
            matched.add(r.tool_call_id)
            sort_key = (0, ev["seq"])
            if not entry.get("args") and ev.get("args") is not None:
                entry["args"] = str(ev["args"])[:200]
            if not entry.get("result") and ev.get("result"):
                entry["result"] = ev["result"]
            if ev.get("internal"):
                entry["internal"] = True
        else:
            sort_key = (1, r.started_at or r.created_at or datetime.min)
        ordered.append((sort_key, entry))
    for c in ev_calls:
        if c["tool_call_id"] and c["tool_call_id"] in matched:
            continue
        entry = {
            "tool_name": c["tool_name"],
            "status": c["status"],
            "attempt_count": 1,
        }
        if c.get("internal"):
            entry["internal"] = True
        if c.get("args") is not None:
            entry["args"] = str(c["args"])[:200]
        if c.get("result"):
            entry["result"] = str(c["result"])[:1000]
        if c.get("error"):
            entry["error"] = c["error"]
        ordered.append(((0, c["seq"]), entry))
    ordered.sort(key=lambda kv: kv[0])
    tool_chain = [e for _, e in ordered]

    duration_s = None
    if run.started_at and run.finished_at:
        try:
            duration_s = round((run.finished_at - run.started_at).total_seconds(), 2)
        except Exception:  # noqa: BLE001
            duration_s = None
    used_tools = sorted({e["tool_name"] for e in tool_chain})
    tool_failed = sum(1 for e in tool_chain if e["status"] != "succeeded")
    tool_total = len(tool_chain)
    return {
        "run_status": run.status or "",
        "duration_s": duration_s,
        "steps": int(run.budget_steps or 0),
        "model_calls": int(run.budget_model_calls or 0),
        "tool_calls": int(run.budget_tool_calls or 0),
        "tokens": int(run.budget_tokens or 0),
        "limit_exceeded": (run.status == "limit_exceeded"),
        "tool_total": tool_total,
        "tool_failed": tool_failed,
        "tool_success_rate": round((tool_total - tool_failed) / tool_total, 4)
        if tool_chain else None,
        "used_tools": used_tools,
        "tool_chain": tool_chain,
    }


def _merge_turn_facts(turn_stats: List[Dict[str, Any]]) -> Dict[str, Any]:
    """把各轮执行事实合并为用例级统计（求和/合并为主）。"""
    if not turn_stats:
        return {}
    if len(turn_stats) == 1:
        single = dict(turn_stats[0])
        for k in ("turn", "run_id", "input", "output", "status"):
            single.pop(k, None)
        return single
    chain: List[Dict[str, Any]] = []
    used: List[str] = []
    for t in turn_stats:
        for c in (t.get("tool_chain") or []):
            chain.append({**c, "turn": t.get("turn")})
        for n in (t.get("used_tools") or []):
            if n not in used:
                used.append(n)
    return {
        "run_status": turn_stats[-1].get("run_status"),
        "duration_s": round(sum(float(t.get("duration_s") or 0) for t in turn_stats), 2),
        "steps": sum(int(t.get("steps") or 0) for t in turn_stats),
        "model_calls": sum(int(t.get("model_calls") or 0) for t in turn_stats),
        "tool_calls": sum(int(t.get("tool_calls") or 0) for t in turn_stats),
        "tokens": sum(int(t.get("tokens") or 0) for t in turn_stats),
        "limit_exceeded": any(t.get("limit_exceeded") for t in turn_stats),
        "tool_total": sum(int(t.get("tool_total") or 0) for t in turn_stats),
        "tool_failed": sum(int(t.get("tool_failed") or 0) for t in turn_stats),
        "tool_success_rate": round(
            sum(int(t.get("tool_total") or 0) - int(t.get("tool_failed") or 0)
                for t in turn_stats) / max(1, sum(
                    int(t.get("tool_total") or 0) for t in turn_stats)), 4)
        if any(t.get("tool_total") for t in turn_stats) else None,
        "used_tools": used,
        "tool_chain": chain,
    }


async def _collect_artifact_text(db: AsyncSession, run_ids: List[str]) -> str:
    """采集用例交付物（纯客观）：文件工具写入内容 + 成功执行的代码脚本。

    工程构建类长程任务的交付物在工作区文件 / 代码执行里而非最终回复文本中，
    评分覆盖交付物才能客观反映构建能力。仅统计 succeeded 的调用（失败尝试
    不算交付物，防止「调了工具但没建成」误判为交付）。

    双口径合并（只读事实表）：
    - 账本口径：agent_tool_executions 中 succeeded 平台工具的参数快照；
    - 事件口径：agent_run_events 的 tool_started 输入——write_file/edit_file 等
      内部工具不经账本，交付物内容只在事件里。
    同一 tool_call_id 两份内容取更完整（更长）的一份，避免单一口径被截断时
    约束误判为未达成。
    """
    if not run_ids:
        return ""
    rows = (await db.execute(
        select(AgentToolExecution.tool_name, AgentToolExecution.args_snapshot,
               AgentToolExecution.tool_call_id)
        .where(AgentToolExecution.run_id.in_(run_ids))
        .where(AgentToolExecution.status == "succeeded")
        .where(AgentToolExecution.tool_name.in_(list(_ARTIFACT_TOOLS)))
    )).all()
    ev_calls = await _collect_event_tool_calls(db, run_ids)

    picked: Dict[str, tuple] = {}
    order: List[str] = []

    def _put(key: str, label: str, text: str) -> None:
        if not text.strip():
            return
        prev = picked.get(key)
        if prev is None:
            picked[key] = (label, text)
            order.append(key)
        elif len(text) > len(prev[1]):
            picked[key] = (label, text)

    for idx, (tool_name, args, tcid) in enumerate(rows):
        a = dict(args or {})
        text = str(a.get("content") or a.get("new_string") or a.get("code") or "")
        label = f"{tool_name} {str(a.get('path') or '').strip()}".strip()
        _put(tcid or f"ledger:{idx}", label, text)
    for c in ev_calls:
        if c["tool_name"] not in _ARTIFACT_TOOLS or c["status"] != "succeeded":
            continue
        a = dict(c.get("args") or {})
        text = str(a.get("content") or a.get("new_string") or a.get("code") or "")
        label = f"{c['tool_name']} {str(a.get('path') or '').strip()}".strip()
        _put(c["tool_call_id"] or f"ev:{c['run_id']}:{c['seq']}", label, text)

    parts: List[str] = []
    for key in order:
        label, text = picked[key]
        parts.append(f"\n\n[交付物: {label}]\n{text}")
    return "".join(parts)


async def _collect_tool_artifacts(
    db: AsyncSession, run_ids: List[str],
) -> List[Dict[str, Any]]:
    """工具调用口径的交付文件清单（工作区不可用时的兜底）。"""
    if not run_ids:
        return []
    rows = (await db.execute(
        select(AgentToolExecution.tool_name, AgentToolExecution.args_snapshot,
               AgentToolExecution.status)
        .where(AgentToolExecution.run_id.in_(run_ids))
        .where(AgentToolExecution.status == "succeeded")
        .where(AgentToolExecution.tool_name.in_(list(_ARTIFACT_TOOLS)))
    )).all()
    out: List[Dict[str, Any]] = []
    for tool_name, args, _status in rows:
        a = dict(args or {})
        path = str(a.get("path") or "").strip()
        if not path:
            continue
        text = str(a.get("content") or a.get("new_string") or a.get("code") or "")
        out.append({"path": path, "size": len(text), "text_chars": len(text),
                    "source": "tool_call", "tool": tool_name})
    return out


# ────────────────────────────── Trial 执行 ──────────────────────────────


async def _load_run(db: AsyncSession, run_id: str) -> Optional[AgentRun]:
    await db.rollback()
    for _attempt in range(3):
        run = (await db.execute(
            select(AgentRun).where(AgentRun.id == run_id))).scalar_one_or_none()
        if run is not None:
            return run
        await db.rollback()
        await asyncio.sleep(0.5)
    return None


async def _wait_run_terminal(
    db: AsyncSession, runtime, run_id: str, *, deadline: float, clock,
) -> Optional[AgentRun]:
    """等待已有 AgentRun 到达终态（恢复场景，不重复执行）。"""
    while clock() < deadline:
        run = await _load_run(db, run_id)
        if run is not None and run.status in TERMINAL_STATUSES:
            return run
        await asyncio.sleep(min(1.0, max(0.0, deadline - clock())))
    return await _load_run(db, run_id)


def _persist_stats(
    turn_stats: List[Dict[str, Any]], session_id: Optional[int],
    actual_output: str,
) -> Dict[str, Any]:
    stats = _merge_turn_facts(turn_stats)
    stats["output_chars"] = len(actual_output or "")
    stats["turns_count"] = len(turn_stats)
    stats["session_id"] = session_id
    stats["turns"] = [
        {k: t.get(k) for k in (
            "turn", "run_id", "input", "output", "status", "steps",
            "tool_calls", "tokens", "duration_s", "used_tools")}
        for t in turn_stats]
    return stats


async def _grade_trial(
    db: AsyncSession,
    *,
    item_id: int,
    snap_method: str,
    snap_config: Dict[str, Any],
    snap_expected: str,
    snap_criteria: str,
    snap_meta: Dict[str, Any],
    judge_input: str,
    actual_output: str,
    output_text: str,
    stats: Dict[str, Any],
    artifacts: List[Dict[str, Any]],
    workspace_text: str,
    execution_status: str,
) -> Dict[str, Any]:
    """执行 Verifier + Judge，返回状态与可落库字段。"""
    method = (snap_method or "none").strip().lower()
    verifier_method = method
    verifier_config = snap_config
    judge_config = snap_config
    if method == "hybrid":
        verifier_spec = snap_config.get("verifier") or {}
        verifier_method = str(verifier_spec.get("method") or "none").strip().lower()
        verifier_config = dict(verifier_spec.get("config") or {})
        judge_config = dict(snap_config.get("judge") or snap_config)
    stats = dict(stats or {})
    tool_chain = list(stats.get("tool_chain") or [])
    required_tools = [str(t) for t in (snap_meta.get("required_tools") or []) if t]
    forbid_tools = bool(snap_config.get("forbid_tools"))
    used_tools = list(stats.get("used_tools") or [])
    succeeded_tools = sorted({entry["tool_name"] for entry in tool_chain
                              if entry.get("status") == "succeeded"})
    missing_tools = [t for t in required_tools if t not in succeeded_tools]
    enforce = bool(snap_config.get("enforce_required_tools", True))
    expected_min_steps = snap_meta.get("expected_min_steps")
    steps_met = (expected_min_steps is None) or (
        int(stats.get("steps") or 0) >= int(expected_min_steps))
    capability = {
        "required_tools": required_tools,
        "used_tools": used_tools,
        "succeeded_tools": succeeded_tools,
        "missing_tools": missing_tools,
        "zero_tool": not used_tools,
        "forbid_tools": forbid_tools,
        "expected_min_steps": expected_min_steps,
        "steps_met": steps_met,
    }
    stats["missing_tools"] = missing_tools
    stats["required_tools"] = required_tools
    stats["steps_met"] = steps_met

    # include_artifacts：交付物并入确定性评分文本（长程任务）
    artifact_text = workspace_text or ""
    if snap_config.get("include_artifacts") and not artifact_text:
        artifact_text = actual_output
    score_text = (output_text or "") + (artifact_text or "")
    if not snap_config.get("include_artifacts"):
        # 文本类 Verifier 默认只看最终回答；产物由 artifact 验证器单独检查
        score_text = output_text or ""
    stats["artifact_chars"] = len(artifact_text or "")

    verification = await verify_output(VerifyContext(
        method=verifier_method,
        config=verifier_config,
        input_text=judge_input,
        expected=snap_expected,
        criteria=snap_criteria,
        actual_output=score_text,
        output_text=output_text,
        tool_chain=tool_chain,
        artifacts=artifacts,
        workspace_text=workspace_text or "",
    ))

    # 客观能力约束：必需工具需成功执行；forbid_tools 时不允许任何工具调用
    checks: List[str] = []
    if required_tools and enforce and missing_tools:
        checks.append(f"必需工具未成功执行：{','.join(missing_tools)}")
    if forbid_tools and used_tools:
        checks.append(f"任务要求不使用工具，但调用了：{','.join(used_tools)}")
    if checks:
        verification["status"] = "failed"
        verification["passed"] = False
        verification["score"] = verification.get("score") or 0.0
        verification.setdefault("detail", {})["capability_check"] = {
            "enforced": True,
            "missing_tools": missing_tools,
            "used_tools": used_tools,
            "checks": checks,
            "note": "工具使用不符合用例声明（客观校验）",
        }
    verification.setdefault("detail", {})["capability"] = capability
    stats["capability"] = capability

    # Judge：只补充主观质量；Verifier 硬失败不调用 Judge（不能让高分覆盖硬失败）
    grading: Dict[str, Any] = {"status": "unscored", "score": None, "passed": None,
                               "detail": {}, "evidence": {}}
    if method in ("llm_judge", "hybrid") and verification.get("status") != "failed":
        try:
            graded = await judge_trial(
                db,
                input_text=judge_input,
                expected=snap_expected,
                criteria=snap_criteria,
                actual_output=(output_text or "") + (workspace_text or ""),
                output_text=output_text,
                execution_status=execution_status,
                tool_chain=tool_chain,
                artifacts=artifacts,
                config=judge_config,
            )
            grading = {
                "status": "graded", "score": graded.get("score"),
                "passed": graded.get("passed"), "detail": graded.get("detail") or {},
                "evidence": graded.get("evidence") or {},
            }
        except ScoringError as e:
            grading = {"status": "error", "score": None, "passed": None,
                       "detail": {"method": "llm_judge", "error": str(e)[:1000]},
                       "evidence": {}}

    verification_status = verification.get("status") or "unscored"
    if method == "llm_judge":
        # llm_judge 用例没有确定性验证器；执行成功即视为验证未配置
        verification_status = "unscored" if verification_status == "unscored" else verification_status
    grading_status = grading["status"]

    # Outcome 判定（任务书 §4.3）
    if execution_status != "succeeded":
        outcome = _outcome_from_execution(execution_status)
    elif verification_status == "failed":
        outcome = "fail"
    elif verification_status == "error":
        outcome = "inconclusive"
    elif grading_status == "graded":
        outcome = "pass" if grading.get("passed") else "fail"
    elif grading_status == "error":
        outcome = "inconclusive"
    elif verification_status == "passed":
        outcome = "pass"
    else:
        outcome = "unscored"

    # 兼容旧字段：score / detail / passed
    detail = dict(verification.get("detail") or {})
    reasoning = grading["detail"].get("reasoning")
    if reasoning:
        detail["reasoning"] = reasoning
    detail["verification"] = {
        "status": verification_status,
        "score": verification.get("score"),
        "passed": verification.get("passed"),
        "evidence": verification.get("evidence") or {},
    }
    detail["grading"] = {
        "status": grading_status,
        "score": grading.get("score"),
        "passed": grading.get("passed"),
        "reasoning": reasoning,
        "prompt_version": grading["detail"].get("prompt_version"),
        "judge_model_config_id": grading["detail"].get("judge_model_config_id"),
        "error": grading["detail"].get("error"),
    }
    if grading_status == "graded":
        detail.setdefault("method", "llm_judge")
        detail["judge_model_config_id"] = grading["detail"].get("judge_model_config_id")
        detail["prompt_version"] = grading["detail"].get("prompt_version")
        detail["pass_threshold"] = grading["detail"].get("pass_threshold")
    elif verification.get("detail", {}).get("method"):
        detail.setdefault("method", verification["detail"]["method"])
    if method == "hybrid":
        detail["method"] = "hybrid"
        detail["verifier_method"] = verifier_method

    score = grading.get("score") if grading_status == "graded" else verification.get("score")
    passed = True if outcome == "pass" else (False if outcome == "fail" else None)

    return {
        "execution_status": execution_status,
        "verification_status": verification_status,
        "grading_status": grading_status,
        "outcome": outcome,
        "legacy_status": _legacy_status(outcome),
        "score": score,
        "passed": passed,
        "detail": detail,
        "verification": verification,
        "grading": grading,
        "stats": stats,
    }


async def _record_grades(
    db: AsyncSession, trial_id: int, *,
    verification: Dict[str, Any], grading: Dict[str, Any],
) -> None:
    def _add(kind: str, payload: Dict[str, Any]) -> None:
        status = payload.get("status")
        if status in (None, "unscored"):
            return
        detail = payload.get("detail") or {}
        db.add(EvalGrade(
            trial_id=trial_id,
            kind=kind,
            name=str(detail.get("method") or kind),
            status=str(status),
            score=payload.get("score"),
            passed=payload.get("passed"),
            detail=detail,
            evidence=payload.get("evidence") or {},
            model_config_id=detail.get("judge_model_config_id"),
            prompt_version=detail.get("prompt_version"),
        ))

    _add("verifier", verification or {})
    _add("judge", grading or {})
    await db.flush()


async def _execute_item(
    db: AsyncSession,
    eval_run_id: int,
    item: EvalRunItem,
    agent_config: Dict[str, Any],
    timeout_s: float,
    user_id: Optional[int],
    *,
    campaign_runtime: Any = None,
) -> None:
    """执行单条 Trial：恢复检查 →（多轮）执行 → 事实/产物采集 → Verifier/Judge → 回写。"""
    from app.services.agent_runtime import get_runtime_service

    # 先把所需字段快照到局部（db.rollback() 会过期 ORM 对象）
    item_id = item.id
    snap_input = item.snap_input or ""
    snap_expected = item.snap_expected or ""
    snap_criteria = item.snap_criteria or ""
    snap_method = item.snap_scoring_method or "none"
    snap_config = dict(item.snap_scoring_config or {})
    snap_meta = dict(item.snap_meta or {})
    persisted = dict(item.stats or {})
    prepared_turns = [dict(t) for t in (persisted.get("turns") or [])]
    session_id: Optional[int] = persisted.get("session_id")
    resume_run_id = item.run_id

    started = _now()
    await db.execute(
        update(EvalRunItem).where(EvalRunItem.id == item_id).values(
            status="running", execution_status="running",
            started_at=started))
    await db.commit()

    run_id: Optional[str] = None
    active_run_id: Optional[str] = None
    principal = user_id or 0
    runtime = get_runtime_service()
    clock = asyncio.get_running_loop().time
    deadline = clock() + timeout_s
    turn_stats: List[Dict[str, Any]] = prepared_turns
    actual_output = str(prepared_turns[-1].get("output") or "") if prepared_turns else ""
    try:
        turn_inputs = [snap_input] + [
            str(t) for t in (snap_meta.get("turns") or []) if str(t).strip()
        ]
        history: List[Dict[str, Any]] = []
        for t in prepared_turns:
            history.append({"role": "user", "content": t.get("input") or ""})
            history.append({"role": "assistant", "content": t.get("output") or ""})
        start_idx = len(prepared_turns)
        execution_error = ""
        execution_status = ""

        # 恢复：已有 AgentRun 但尚未记录为完成的轮次 → 先等待其终态，不重复执行
        recorded_run_ids = {str(t.get("run_id")) for t in prepared_turns if t.get("run_id")}
        last_recorded_status = (
            str(prepared_turns[-1].get("status") or "") if prepared_turns else "")
        if prepared_turns and last_recorded_status and last_recorded_status != STATUS_COMPLETED:
            # 已记录的最后一轮本身未完成：不继续下一轮，按执行失败收口
            execution_error = (
                f"第 {len(prepared_turns)} 轮 AgentRun 状态为 "
                f"{last_recorded_status or 'unknown'}")
            execution_status = _execution_status_from_run(last_recorded_status)
        elif (resume_run_id and resume_run_id not in recorded_run_ids
              and start_idx < len(turn_inputs)):
            existing = await _load_run(db, resume_run_id)
            if existing is not None and existing.status not in TERMINAL_STATUSES:
                existing = await _wait_run_terminal(
                    db, runtime, resume_run_id, deadline=deadline, clock=clock)
            if existing is not None:
                run_id = resume_run_id
                if existing.status == STATUS_COMPLETED:
                    facts = await _collect_run_facts(db, resume_run_id)
                    actual_output = existing.final_text or ""
                    turn_stats.append({
                        "turn": start_idx + 1, "run_id": resume_run_id,
                        "input": turn_inputs[start_idx], "output": actual_output,
                        "status": existing.status, **facts,
                    })
                    session_id = existing.session_id or session_id
                    history.append({"role": "user", "content": turn_inputs[start_idx]})
                    history.append({"role": "assistant", "content": actual_output})
                    start_idx += 1
                    stats = _persist_stats(turn_stats, session_id, actual_output)
                    await db.execute(
                        update(EvalRunItem).where(EvalRunItem.id == item_id).values(
                            run_id=resume_run_id, actual_output=actual_output,
                            stats=stats))
                    await db.commit()
                elif existing.status in TERMINAL_STATUSES:
                    # 上一轮 AgentRun 已失败/取消：记录事实后按执行失败收口
                    facts = await _collect_run_facts(db, resume_run_id)
                    actual_output = existing.final_text or actual_output
                    turn_stats.append({
                        "turn": start_idx + 1, "run_id": resume_run_id,
                        "input": turn_inputs[start_idx], "output": actual_output,
                        "status": existing.status, **facts,
                    })
                    session_id = existing.session_id or session_id
                    execution_error = (
                        f"第 {start_idx + 1} 轮 AgentRun 状态为 "
                        f"{existing.status or 'unknown'}")
                    if existing.error:
                        execution_error += f"。{existing.error[:1600]}"
                    execution_status = _execution_status_from_run(
                        existing.status, existing.error or "")
                else:
                    # 等待超时仍未终态：取消该 Run，不重复执行（避免重复副作用）
                    cleanup = await _cancel_unfinished_run(
                        runtime, resume_run_id, principal)
                    execution_status = "timeout"
                    execution_error = (
                        f"恢复等待 AgentRun 超时（>{timeout_s:g}s，"
                        f"cancel={cleanup}）")

        last_run_status = ""
        while not execution_error and start_idx < len(turn_inputs):
            if clock() >= deadline:
                raise asyncio.TimeoutError
            if campaign_runtime is not None and campaign_runtime.cancel_requested:
                execution_status = "cancelled"
                execution_error = "测评任务已取消"
                break
            text = turn_inputs[start_idx]
            user_message = {"role": "user", "content": text}
            run_id, session_id, stream = await runtime.start_run_with_session(
                agent_config=agent_config,
                messages=history + [user_message],
                user_id=principal,
                session_id=session_id,
                endpoint="eval",
                user_message=user_message,
                source="evaluation",
            )
            active_run_id = run_id
            if campaign_runtime is not None:
                campaign_runtime.active_run_id = run_id
            await asyncio.wait_for(_drain_stream(stream), timeout=deadline - clock())

            agent_run = await _load_run(db, run_id)
            if agent_run is None:
                raise RuntimeError(f"AgentRun 未找到: {run_id}")
            actual_output = agent_run.final_text or ""
            run_status = agent_run.status or ""
            facts = await _collect_run_facts(db, run_id)
            turn_stats.append({
                "turn": start_idx + 1, "run_id": run_id,
                "input": text, "output": actual_output,
                "status": run_status, **facts,
            })
            history.append(user_message)
            history.append({"role": "assistant", "content": actual_output})
            # 每轮结束即持久化进度：崩溃恢复时只续做未完成轮次
            stats = _persist_stats(turn_stats, session_id, actual_output)
            await db.execute(
                update(EvalRunItem).where(EvalRunItem.id == item_id).values(
                    run_id=run_id, actual_output=actual_output, stats=stats))
            await db.commit()
            if run_status != STATUS_COMPLETED:
                last_run_status = run_status
                execution_error = f"第 {start_idx + 1} 轮 AgentRun 状态为 {run_status or 'unknown'}"
                if agent_run.error:
                    execution_error += f"。{agent_run.error[:1600]}"
                if run_status not in TERMINAL_STATUSES:
                    cleanup_status = await _cancel_unfinished_run(runtime, run_id, principal)
                    execution_error += f"（取消状态：{cleanup_status}）"
                execution_status = _execution_status_from_run(
                    run_status, agent_run.error or "")
                break
            start_idx += 1
            active_run_id = None
            if campaign_runtime is not None:
                campaign_runtime.active_run_id = None

        stats = _persist_stats(turn_stats, session_id, actual_output)

        if execution_error:
            if not execution_status:
                execution_status = _execution_status_from_run(last_run_status)
            outcome = _outcome_from_execution(execution_status)
            await db.execute(update(EvalRunItem).where(EvalRunItem.id == item_id).values(
                status=_legacy_status(outcome), execution_status=execution_status,
                verification_status="unscored", grading_status="unscored",
                outcome=outcome,
                passed=False if outcome == "fail" else None,
                run_id=run_id, actual_output=actual_output,
                stats=stats, score_detail={"execution": {
                    "passed": False, "turns_expected": len(turn_inputs),
                    "turns_attempted": len(turn_stats),
                    "run_status": last_run_status or None,
                    "execution_status": execution_status,
                }},
                error=execution_error, finished_at=_now(),
            ))
            await db.commit()
            return

        # 评分输入：多轮时给裁判完整对话上下文
        if len(turn_inputs) > 1:
            judge_input = "\n".join(
                f"[{'用户' if h['role'] == 'user' else '助手'}] {h['content']}"
                for h in history[:-1])
        else:
            judge_input = snap_input

        # 真实工作区最终产物（优先）与工具调用口径兜底
        run_ids = [str(t.get("run_id")) for t in turn_stats if t.get("run_id")]
        workspace = await collect_workspace_artifacts(
            principal, agent_config.get("id"), session_id)
        artifacts = list(workspace.get("files") or [])
        workspace_text = str(workspace.get("text") or "")
        if not artifacts:
            artifacts = await _collect_tool_artifacts(db, run_ids)
        if not workspace_text and snap_config.get("include_artifacts"):
            workspace_text = await _collect_artifact_text(db, run_ids)
        stats["artifact_source"] = "workspace" if workspace.get("files") else "tool_call"
        stats["artifacts"] = artifacts[:100]

        graded = await _grade_trial(
            db,
            item_id=item_id,
            snap_method=snap_method,
            snap_config=snap_config,
            snap_expected=snap_expected,
            snap_criteria=snap_criteria,
            snap_meta=snap_meta,
            judge_input=judge_input,
            actual_output=(actual_output or "") + (workspace_text or ""),
            output_text=actual_output or "",
            stats=stats,
            artifacts=artifacts,
            workspace_text=workspace_text,
            execution_status="succeeded",
        )
        await db.execute(
            update(EvalRunItem).where(EvalRunItem.id == item_id).values(
                status=graded["legacy_status"],
                execution_status=graded["execution_status"],
                verification_status=graded["verification_status"],
                grading_status=graded["grading_status"],
                outcome=graded["outcome"],
                run_id=run_id,
                actual_output=actual_output,
                score=graded["score"],
                passed=graded["passed"],
                score_detail=graded["detail"],
                stats=graded["stats"],
                error=None,
                finished_at=_now(),
            ))
        await _record_grades(
            db, item_id, verification=graded["verification"],
            grading=graded["grading"])
        await db.commit()
    except asyncio.TimeoutError:
        await db.rollback()
        cancellation_status = (
            await _cancel_unfinished_run(runtime, active_run_id, principal)
            if active_run_id else None
        )
        partial = _persist_stats(
            [dict(t) for t in turn_stats], session_id, actual_output or "")
        await db.execute(
            update(EvalRunItem).where(EvalRunItem.id == item_id).values(
                status="error", execution_status="timeout",
                verification_status="unscored", grading_status="unscored",
                outcome="fail", passed=False, run_id=run_id,
                actual_output=actual_output or "",
                stats=partial,
                score_detail={"execution": {
                    "passed": False, "execution_status": "timeout",
                    "cancellation_status": cancellation_status}},
                error=f"用例执行超时（>{timeout_s:g}s）", finished_at=_now()))
        await db.commit()
    except Exception as e:  # noqa: BLE001
        logger.exception("测评用例执行失败 eval_run=%s item=%s", eval_run_id, item_id)
        await db.rollback()
        text = str(e)
        await db.execute(
            update(EvalRunItem).where(EvalRunItem.id == item_id).values(
                status="error", execution_status="infra_error",
                verification_status="unscored", grading_status="unscored",
                outcome="infra_error", run_id=run_id,
                error=text[:2000], finished_at=_now()))
        await db.commit()


# ────────────────────────────── Campaign 执行 ──────────────────────────────


class _CampaignRuntime:
    """执行期共享状态：当前活跃 AgentRun 与取消意图。"""

    def __init__(self) -> None:
        self.active_run_id: Optional[str] = None
        self.cancel_requested = False
        self.lease_lost = False


def _default_eval_worker_id() -> str:
    return getattr(settings, "EVALUATION_WORKER_ID", "") or f"eval-{uuid.uuid4().hex[:10]}"


async def _claim_campaign_lease(
    db: AsyncSession, eval_run_id: int, worker_id: str, lease_seconds: int,
) -> Optional[int]:
    """CAS 领取 Campaign 执行租约；返回新 lease_version，失败返回 None。"""
    for _ in range(3):
        await db.rollback()
        row = (await db.execute(
            select(EvalRun.id, EvalRun.status, EvalRun.worker_id,
                   EvalRun.lease_expires_at, EvalRun.lease_version)
            .where(EvalRun.id == eval_run_id))).first()
        if row is None or row.status not in ("pending", "running"):
            return None
        now = _now()
        if row.worker_id and row.lease_expires_at and row.lease_expires_at > now:
            return None
        new_version = int(row.lease_version or 0) + 1
        res = await db.execute(
            update(EvalRun).where(
                EvalRun.id == eval_run_id,
                EvalRun.lease_version == (row.lease_version or 0),
                EvalRun.status.in_(("pending", "running")),
                or_(EvalRun.worker_id.is_(None),
                    EvalRun.lease_expires_at.is_(None),
                    EvalRun.lease_expires_at <= now),
            ).values(
                worker_id=worker_id,
                lease_expires_at=now + timedelta(seconds=lease_seconds),
                heartbeat_at=now,
                lease_version=new_version,
                status="running",
                started_at=func.coalesce(EvalRun.started_at, now),
                attempt=func.coalesce(EvalRun.attempt, 0) + 1,
            ))
        await db.commit()
        if res.rowcount:
            return new_version
    return None


async def _heartbeat_campaign(
    eval_run_id: int, worker_id: str, lease_version: int,
    lease_seconds: float, runtime: _CampaignRuntime, user_id: int,
    stop_event: asyncio.Event, interval: float,
) -> None:
    """Campaign 心跳：续租 + 发现取消意图（取消传递到真实 AgentRun）。"""
    while not stop_event.is_set():
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
            return
        except asyncio.TimeoutError:
            pass
        try:
            async with AsyncSessionLocal() as hb:
                now = _now()
                res = await hb.execute(
                    update(EvalRun).where(
                        EvalRun.id == eval_run_id,
                        EvalRun.worker_id == worker_id,
                        EvalRun.lease_version == lease_version,
                    ).values(
                        heartbeat_at=now,
                        lease_expires_at=now + timedelta(seconds=int(lease_seconds))))
                await hb.commit()
                if not res.rowcount:
                    runtime.lease_lost = True
                    return
                flag = (await hb.execute(
                    select(EvalRun.cancel_requested, EvalRun.status)
                    .where(EvalRun.id == eval_run_id))).first()
            if flag and (flag.cancel_requested or flag.status == "cancel_requested"):
                runtime.cancel_requested = True
                from app.services.agent_runtime import get_runtime_service

                if runtime.active_run_id:
                    try:
                        await get_runtime_service().cancel_run(
                            runtime.active_run_id, user_id)
                    except Exception:  # noqa: BLE001
                        logger.warning("测评心跳取消 AgentRun 失败", exc_info=True)
        except Exception:  # noqa: BLE001
            logger.warning("Campaign 心跳失败（继续）", exc_info=True)


async def _recover_running_trials(db: AsyncSession, eval_run_id: int) -> int:
    """重启恢复：running Trial 退回 pending（保留 run_id，由执行逻辑先查 Run 状态）。"""
    res = await db.execute(
        update(EvalRunItem).where(
            EvalRunItem.eval_run_id == eval_run_id,
            EvalRunItem.status == "running",
        ).values(status="pending", execution_status="pending"))
    await db.commit()
    return int(res.rowcount or 0)


async def _campaign_cancelled(db: AsyncSession, eval_run_id: int) -> bool:
    await db.rollback()
    row = (await db.execute(
        select(EvalRun.status, EvalRun.cancel_requested)
        .where(EvalRun.id == eval_run_id))).first()
    return bool(row and (row.cancel_requested or row.status == "cancel_requested"))


async def _campaign_budget_exceeded(
    db: AsyncSession, eval_run_id: int, config: Dict[str, Any],
) -> Optional[str]:
    """Campaign 级总耗时 / 总 Token 预算检查（返回超限原因，未超限返回 None）。

    Trial 级超时与 Runtime 单 Run 预算之外的第二道资源上限，防止一次 Campaign
    因大量重复 Trial 或长任务无限消耗。检查发生在 Trial 之间（不打断在途 Run）。
    """
    await db.rollback()
    row = (await db.execute(
        select(EvalRun.started_at, EvalRun.aggregate)
        .where(EvalRun.id == eval_run_id))).first()
    if row is None:
        return None
    timeout_s = float(config.get("campaign_timeout_s") or 0)
    if timeout_s > 0 and row.started_at is not None:
        elapsed = (_now() - row.started_at).total_seconds()
        if elapsed >= timeout_s:
            return (f"Campaign 总耗时超限（{elapsed:.0f}s ≥ {timeout_s:.0f}s），"
                    f"未执行的 Trial 已跳过")
    max_tokens = int(config.get("campaign_max_tokens") or 0)
    if max_tokens > 0:
        used = int((row.aggregate or {}).get("total_tokens") or 0)
        if used >= max_tokens:
            return (f"Campaign 总 Token 超限（{used} ≥ {max_tokens}），"
                    f"未执行的 Trial 已跳过")
    return None


async def _skip_pending_trials(
    db: AsyncSession, eval_run_id: int, reason: str,
) -> None:
    await db.execute(
        update(EvalRunItem).where(
            EvalRunItem.eval_run_id == eval_run_id,
            EvalRunItem.status.in_(("pending", "running")),
            EvalRunItem.outcome == "pending",
        ).values(
            status="skipped", execution_status="cancelled",
            verification_status="unscored", grading_status="unscored",
            outcome="cancelled", error=reason[:500], finished_at=_now()))
    await db.commit()


async def execute_eval_run(eval_run_id: int) -> None:
    """Campaign 主循环：Lease 领取 → 恢复检查 → 逐 Trial 执行 → 聚合 → 终态。"""
    worker_id = _default_eval_worker_id()
    lease_seconds = int(getattr(settings, "EVALUATION_LEASE_SECONDS", 120) or 120)
    async with AsyncSessionLocal() as db:
        eval_run = (await db.execute(
            select(EvalRun).where(EvalRun.id == eval_run_id))).scalar_one_or_none()
        if eval_run is None:
            logger.warning("execute_eval_run: 任务不存在 %s", eval_run_id)
            return
        if eval_run.status not in ("pending", "running"):
            return

        lease_version = await _claim_campaign_lease(
            db, eval_run_id, worker_id, lease_seconds)
        if lease_version is None:
            return

        # 领取租约时执行了 rollback（ORM 属性过期）：重新加载事实行
        eval_run = (await db.execute(
            select(EvalRun).where(EvalRun.id == eval_run_id)
            .execution_options(populate_existing=True))).scalar_one()
        config = dict(eval_run.config or {})
        timeout_s = float(config.get("timeout_s") or DEFAULT_ITEM_TIMEOUT_S)
        approval_mode = config.get("approval_mode") if config.get("approval_mode") in (
            "manual", "auto") else "auto"
        use_memory = bool(config.get("use_memory"))
        infra_retry_default = getattr(settings, "EVALUATION_INFRA_RETRY_MAX", 1)
        infra_retry_max = int(config.get("infra_retry_max")
                              if config.get("infra_retry_max") is not None
                              else infra_retry_default)

        # Run 的 user_id 有外键约束：未指定创建人时兜底取一个真实用户
        user_id = eval_run.created_by
        if user_id is None:
            from app.models.user import User
            user_id = (await db.execute(
                select(User.id).order_by(User.id).limit(1))).scalar()

        campaign_runtime = _CampaignRuntime()
        budget_error = ""
        stop_event = asyncio.Event()
        heartbeat = asyncio.create_task(_heartbeat_campaign(
            eval_run_id, worker_id, lease_version, lease_seconds,
            campaign_runtime, user_id or 0, stop_event,
            float(getattr(settings, "EVALUATION_HEARTBEAT_INTERVAL", 20.0) or 20.0),
        ))

        try:
            await _recover_running_trials(db, eval_run_id)
            agent = (await db.execute(
                select(Agent).where(Agent.id == eval_run.agent_id))).scalar_one_or_none()
            if agent is None:
                raise RuntimeError("Agent 不存在或已删除")
            # 版本固定：只读 Campaign 固定的 AgentVersion
            agent_config = await _build_agent_config(
                db, agent, use_memory=use_memory,
                agent_version_id=eval_run.agent_version_id)
            agent_config["approval_mode"] = approval_mode

            while True:
                if campaign_runtime.lease_lost:
                    logger.warning("Campaign %s 租约丢失，停止执行", eval_run_id)
                    return
                if await _campaign_cancelled(db, eval_run_id):
                    campaign_runtime.cancel_requested = True
                    await db.execute(
                        update(EvalRunItem).where(
                            EvalRunItem.eval_run_id == eval_run_id,
                            EvalRunItem.status.in_(("pending", "running")),
                            EvalRunItem.outcome == "pending",
                        ).values(
                            status="skipped", execution_status="cancelled",
                            verification_status="unscored", grading_status="unscored",
                            outcome="cancelled", finished_at=_now()))
                    await db.commit()
                    break

                # Campaign 级资源上限（Trial 之间检查，不打断在途 Run）
                budget_error = await _campaign_budget_exceeded(
                    db, eval_run_id, config)
                if budget_error:
                    await _skip_pending_trials(
                        db, eval_run_id, budget_error)
                    break

                item = (await db.execute(
                    select(EvalRunItem).where(
                        EvalRunItem.eval_run_id == eval_run_id,
                        EvalRunItem.status == "pending",
                    ).order_by(EvalRunItem.position).limit(1))).scalar_one_or_none()
                if item is None:
                    break

                claimed = await db.execute(
                    update(EvalRunItem).where(
                        EvalRunItem.id == item.id,
                        EvalRunItem.status == "pending",
                    ).values(status="running", execution_status="running"))
                await db.commit()
                if claimed.rowcount == 0:
                    continue

                await _execute_item(db, eval_run_id, item, agent_config,
                                    timeout_s, user_id,
                                    campaign_runtime=campaign_runtime)
                # 基础设施故障的保守重试：仅当没有成功工具调用（无副作用证据）
                await _maybe_retry_infra(db, item.id, infra_retry_max)
                await _refresh_aggregate(db, eval_run_id)

            agg = await _refresh_aggregate(db, eval_run_id)
            await db.rollback()
            row = (await db.execute(
                select(EvalRun.status, EvalRun.cancel_requested)
                .where(EvalRun.id == eval_run_id))).first()
            final = "cancelled" if (
                row and (row.cancel_requested or row.status == "cancel_requested")
            ) else ("failed" if budget_error else "completed")
            final_values: Dict[str, Any] = {
                "status": final, "finished_at": _now(), "aggregate": agg,
                "cancel_requested": False,
                "lease_expires_at": None, "heartbeat_at": None,
            }
            if budget_error:
                final_values["error"] = budget_error[:2000]
            await db.execute(
                update(EvalRun).where(
                    EvalRun.id == eval_run_id,
                    EvalRun.worker_id == worker_id,
                    EvalRun.lease_version == lease_version,
                ).values(**final_values))
            await db.commit()
        except Exception as e:  # noqa: BLE001
            logger.exception("测评任务执行失败 eval_run=%s", eval_run_id)
            await db.rollback()
            res = await db.execute(
                update(EvalRun).where(
                    EvalRun.id == eval_run_id,
                    EvalRun.worker_id == worker_id,
                    EvalRun.lease_version == lease_version,
                ).values(
                    status="failed", error=str(e)[:2000], finished_at=_now(),
                    lease_expires_at=None, heartbeat_at=None))
            await db.commit()
            if not res.rowcount:
                logger.warning(
                    "Campaign %s 执行异常但租约已失效（不覆盖其他 Worker 状态）",
                    eval_run_id)
        finally:
            stop_event.set()
            if not heartbeat.done():
                heartbeat.cancel()
            try:
                await heartbeat
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass


async def _maybe_retry_infra(db: AsyncSession, item_id: int, max_retry: int) -> None:
    """基础设施故障重试（Attempt）：仅无成功工具调用的 Trial 允许自动重试。

    存在成功外部副作用证据时禁止自动重放（任务书 §4.8）。
    """
    if max_retry <= 0:
        return
    await db.rollback()
    row = (await db.execute(
        select(EvalRunItem.execution_status, EvalRunItem.attempt,
               EvalRunItem.stats)
        .where(EvalRunItem.id == item_id))).first()
    if row is None or row.execution_status != "infra_error":
        return
    chain = list((row.stats or {}).get("tool_chain") or [])
    if chain:
        # 任何工具调用（即使状态 failed/unknown）都可能产生外部副作用：
        # 无成功副作用证据也不代表没有副作用，禁止自动重放
        return
    if int(row.attempt or 0) >= max_retry:
        return
    await db.execute(
        update(EvalRunItem).where(EvalRunItem.id == item_id).values(
            status="pending", execution_status="pending", outcome="pending",
            verification_status="pending", grading_status="pending",
            attempt=int(row.attempt or 0) + 1, run_id=None,
            error=None, started_at=None, finished_at=None))
    await db.commit()


# 后台任务引用（防 GC 提前回收）
_background_tasks: set = set()


async def start_eval_run_background(eval_run_id: int) -> None:
    """把测评任务丢到后台执行（进程内快速路径；重启后由 Evaluation Worker 接管）。"""
    task = asyncio.create_task(execute_eval_run(eval_run_id))
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


async def cancel_eval_run(db: AsyncSession, eval_run: EvalRun) -> Dict[str, Any]:
    """取消测评：运行中置 cancel_requested，未开始 Trial 标记取消。"""
    if eval_run.status in ("completed", "failed", "cancelled"):
        return {"status": eval_run.status, "changed": False}
    if eval_run.status == "running":
        eval_run.status = "cancel_requested"
        eval_run.cancel_requested = True
    else:
        eval_run.status = "cancelled"
        eval_run.cancel_requested = False
        eval_run.finished_at = _now()
        await db.execute(
            update(EvalRunItem).where(
                EvalRunItem.eval_run_id == eval_run.id,
                EvalRunItem.status.in_(("pending", "running")),
            ).values(
                status="skipped", execution_status="cancelled",
                verification_status="unscored", grading_status="unscored",
                outcome="cancelled", finished_at=_now()))
    await db.commit()
    return {"status": eval_run.status, "changed": True}


async def resume_eval_run(db: AsyncSession, eval_run: EvalRun) -> Dict[str, Any]:
    """恢复测评：未通过/异常/未评分的 Trial 重置为 pending 后重新执行。

    已通过的 Trial 不重置（避免重复消耗与重复副作用）；失败 Trial 的原评分
    记录保留在 eval_grades，形成可追溯的评分历史。
    """
    if eval_run.status in ("pending", "running", "cancel_requested"):
        raise ValueError("任务尚在进行中，无需恢复")
    if eval_run.agent_version_id is None:
        # 历史测评缺少固定版本：不猜测补写，也不允许用最新版本静默恢复
        raise ValueError(
            "历史测评未固定 AgentVersion，无法安全恢复；请基于相同数据集/套件新建测评任务")
    await db.execute(
        update(EvalRunItem).where(
            EvalRunItem.eval_run_id == eval_run.id,
            or_(
                EvalRunItem.status.in_(("error", "skipped", "running", "unscored")),
                EvalRunItem.outcome.in_(("inconclusive", "infra_error", "unscored")),
            ),
        ).values(
            status="pending", execution_status="pending",
            verification_status="pending", grading_status="pending",
            outcome="pending", attempt=0, error=None, run_id=None,
            score=None, passed=None, score_detail={}, stats={},
            started_at=None, finished_at=None))
    eval_run.status = "pending"
    eval_run.cancel_requested = False
    eval_run.error = None
    eval_run.finished_at = None
    eval_run.worker_id = None
    eval_run.lease_expires_at = None
    eval_run.heartbeat_at = None
    await db.commit()
    await _refresh_aggregate(db, eval_run.id)
    return {"status": eval_run.status}
