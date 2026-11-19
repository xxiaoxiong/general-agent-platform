"""测评运行器：对某 Agent × 某数据集批量执行用例并评分聚合。

执行模型：
- 每条用例按轮起真实 AgentRun（start_run_with_session，approval_mode 默认 auto、
  默认不带记忆，保证测评可复现），消费旧契约事件流至 done 后取 final_text；
  多轮用例（turns）逐轮执行，同 Session 累积对话历史，评分针对最终输出；
- 逐条 claim（UPDATE ... WHERE status='pending'）防并发重复执行；
- 评分走 scoring.score_output；执行事实（步数/工具链/tokens 等）从平台事实表
  （工具执行账本 + 运行事件日志）采集为纯客观数据（不改事实表、无 LLM、无主观判断）；
- 工具能力客观校验：用例声明 required_tools 时，未调用要求工具即判不通过
  （scoring_config.enforce_required_tools=false 可关闭），供「Agent 能力」评测；
- 支持取消（pending→skipped）与恢复（重跑未完成用例）。
"""
import asyncio
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import AsyncSessionLocal
from app.models.agent import Agent, AgentRun, AgentRunEvent, AgentToolExecution
from app.models.evaluation import EvalDataset, EvalDatasetItem, EvalRun, EvalRunItem
from app.services.agent_runtime.events import TOOL_COMPLETED, TOOL_FAILED, TOOL_STARTED

from .scoring import ScoringError, resolve_method, score_output

logger = logging.getLogger(__name__)

# 用例执行默认超时（秒）：长程任务（长文档/工程构建/深度研究）需要更长执行时间
DEFAULT_ITEM_TIMEOUT_S = 300

# 复杂任务体系常量（与前端展示口径一致）
COMPLEXITY_LEVELS = ("L1", "L2", "L3", "L4")
COMPLEXITY_TEXT = {"L1": "简单", "L2": "中等", "L3": "复杂", "L4": "困难"}
TASK_TYPES = ("direct_qa", "multi_step_reasoning", "tool_use",
              "instruction_following", "multi_turn", "adversarial",
              "project_build", "long_document", "complex_math", "deep_research")


def _now() -> datetime:
    return datetime.utcnow()


def _avg(vals: List[Any]) -> Optional[float]:
    vals = [v for v in vals if v is not None]
    return round(sum(vals) / len(vals), 3) if vals else None


def _layer_summary(rows: List[EvalRunItem]) -> Dict[str, Any]:
    """一组用例的分层汇总（确定性计算）。"""
    done = [i for i in rows if i.status in ("passed", "failed", "error")]
    ok = sum(1 for i in rows if i.status == "passed")
    scored_rows = [i for i in rows if i.status in ("passed", "failed")]
    st = [i.stats or {} for i in scored_rows]
    return {
        "total": len(rows),
        "completed": len(done),
        "passed": ok,
        "failed": sum(1 for i in rows if i.status == "failed"),
        "error": sum(1 for i in rows if i.status == "error"),
        "pass_rate": round(ok / len(done), 4) if done else None,
        "avg_score": _avg([i.score for i in scored_rows]),
        "avg_steps": _avg([s.get("steps") for s in st]),
        "avg_tool_calls": _avg([s.get("tool_calls") for s in st]),
        "avg_duration_s": _avg([s.get("duration_s") for s in st]),
        "avg_output_chars": _avg([s.get("output_chars") for s in st]),
    }


def compute_aggregate(items: List[EvalRunItem]) -> Dict[str, Any]:
    """由用例结果行聚合汇总（确定性计算，无 LLM、无主观判断）。

    除总体指标外，按复杂度/任务类型分层并汇总工具使用与问题用例清单，
    作为「Agent 能力画像」与开发迭代分析的纯客观数据。
    """
    total = len(items)
    completed = [i for i in items if i.status in ("passed", "failed", "error")]
    passed = sum(1 for i in items if i.status == "passed")
    failed = sum(1 for i in items if i.status == "failed")
    error = sum(1 for i in items if i.status == "error")
    skipped = sum(1 for i in items if i.status == "skipped")
    scored = [i for i in items if i.status in ("passed", "failed")]
    stats = [i.stats or {} for i in scored]

    # 按复杂度 / 任务类型分层
    by_complexity: Dict[str, Any] = {}
    for lvl in COMPLEXITY_LEVELS:
        rows = [i for i in items if (i.snap_complexity or "L2") == lvl]
        if rows:
            by_complexity[lvl] = _layer_summary(rows)
    by_task_type: Dict[str, Any] = {}
    for tt in sorted({(i.snap_task_type or "direct_qa") for i in items}):
        by_task_type[tt] = _layer_summary(
            [i for i in items if (i.snap_task_type or "direct_qa") == tt])

    # 工具使用统计（跨用例汇总）
    tool_usage: Dict[str, Dict[str, int]] = {}
    zero_tool = 0
    multi_step = 0
    limit_exceeded = 0
    missing_required = 0
    tool_total_all = 0
    tool_failed_all = 0
    # 约束达成统计（constraint 评分用例，纯客观）
    cons_total = 0
    cons_passed = 0
    cons_items = 0
    for i in scored:
        s = i.stats or {}
        d = i.score_detail or {}
        if d.get("method") == "constraint":
            cons_items += 1
            cons_total += int(d.get("total_constraints") or 0)
            cons_passed += int(d.get("passed_constraints") or 0)
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

    # 问题用例清单（纯客观事实：失败/异常/未用要求工具/未达期望步数）
    problem_items: List[Dict[str, Any]] = []
    for i in items:
        s = i.stats or {}
        problems: List[str] = []
        if i.status == "error":
            problems.append("error")
        elif i.status == "failed":
            problems.append("failed")
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
                "complexity": i.snap_complexity or "L2",
                "task_type": i.snap_task_type or "direct_qa",
                "problems": problems,
                "score": i.score,
                "steps": s.get("steps"),
                "tool_calls": s.get("tool_calls"),
                "output_chars": s.get("output_chars"),
                "used_tools": s.get("used_tools") or [],
                "missing_tools": s.get("missing_tools") or [],
                "error": (i.error or "")[:200] or None,
            })

    return {
        "total": total,
        "completed": len(completed),
        "passed": passed,
        "failed": failed,
        "error": error,
        "skipped": skipped,
        "pending": sum(1 for i in items if i.status in ("pending", "running")),
        "pass_rate": round(passed / len(completed), 4) if completed else None,
        "avg_score": _avg([i.score for i in scored]),
        "avg_duration_s": _avg([s.get("duration_s") for s in stats]),
        "total_tokens": sum(int(s.get("tokens") or 0) for s in stats),
        "total_tool_calls": sum(int(s.get("tool_calls") or 0) for s in stats),
        # ── Agent 能力画像（纯客观） ──
        "by_complexity": by_complexity,
        "by_task_type": by_task_type,
        "tool_usage": tool_usage,
        "capability": {
            "avg_steps": _avg([s.get("steps") for s in stats]),
            "max_steps": max([int(s.get("steps") or 0) for s in stats], default=0),
            "multi_step_count": multi_step,       # 步数 ≥ 2 的用例数
            "zero_tool_count": zero_tool,         # 全程未用任何工具的用例数
            "tool_error_rate": round(tool_failed_all / tool_total_all, 4)
            if tool_total_all else 0.0,
            "missing_required_tools_count": missing_required,
            "limit_exceeded_count": limit_exceeded,
            # 长程任务客观指标（输出规模 / 约束达成）
            "avg_output_chars": _avg([s.get("output_chars") for s in stats]),
            "max_output_chars": max(
                [int(s.get("output_chars") or 0) for s in stats], default=0),
            "constraint_items": cons_items,
            "constraint_total": cons_total,
            "constraint_passed": cons_passed,
            "constraint_ratio": round(cons_passed / cons_total, 4)
            if cons_total else None,
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


async def create_eval_run(
    db: AsyncSession,
    *,
    name: str,
    dataset_id: int,
    agent_id: int,
    created_by: Optional[int],
    config: Optional[Dict[str, Any]] = None,
) -> EvalRun:
    """创建测评任务与用例结果行（快照数据集内容，含复杂任务维度）。"""
    dataset = (await db.execute(
        select(EvalDataset).where(EvalDataset.id == dataset_id))).scalar_one_or_none()
    if dataset is None:
        raise ValueError("数据集不存在")
    agent = (await db.execute(
        select(Agent).where(Agent.id == agent_id))).scalar_one_or_none()
    if agent is None:
        raise ValueError("Agent 不存在")

    config = dict(config or {})
    limit = int(config.get("limit") or 0)
    stmt = select(EvalDatasetItem).where(EvalDatasetItem.dataset_id == dataset_id) \
        .order_by(EvalDatasetItem.position)
    if limit > 0:
        stmt = stmt.limit(limit)
    dataset_items = (await db.execute(stmt)).scalars().all()
    if not dataset_items:
        raise ValueError("数据集没有用例")

    eval_run = EvalRun(
        name=name,
        dataset_id=dataset_id,
        agent_id=agent_id,
        agent_version_id=getattr(agent, "current_version_id", None),
        status="pending",
        config=config,
        aggregate={"total": len(dataset_items), "completed": 0, "passed": 0,
                   "failed": 0, "error": 0, "skipped": 0, "pending": len(dataset_items),
                   "pass_rate": None, "avg_score": None, "avg_duration_s": None,
                   "total_tokens": 0, "total_tool_calls": 0},
        created_by=created_by,
    )
    db.add(eval_run)
    await db.flush()
    for pos, it in enumerate(dataset_items):
        db.add(EvalRunItem(
            eval_run_id=eval_run.id,
            dataset_item_id=it.id,
            position=pos,
            snap_input=it.input or "",
            snap_expected=it.expected or "",
            snap_criteria=it.criteria or "",
            snap_scoring_method=resolve_method(it.scoring_method, dataset.scoring_method),
            snap_scoring_config=dict(it.scoring_config or {}),
            snap_complexity=it.complexity or "L2",
            snap_task_type=it.task_type or "direct_qa",
            snap_meta={
                "required_tools": list(it.required_tools or []),
                "expected_min_steps": it.expected_min_steps,
                "turns": list(it.turns or []),
                "metadata": dict(it.item_metadata or {}),
            },
            status="pending",
        ))
    await db.commit()
    await db.refresh(eval_run)
    return eval_run


async def _build_agent_config(db: AsyncSession, agent: Agent, *,
                              use_memory: bool) -> Dict[str, Any]:
    """由 Agent 行构建运行配置（与 v2 runs._load_agent_config 同口径）。"""
    from app.assistant.versions import config_from_version, ensure_version

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
    return cfg


async def _drain_stream(stream) -> None:
    """消费旧契约事件流至 done（执行由 Runtime Worker 驱动）。"""
    async for event in stream:
        if isinstance(event, dict) and event.get("type") == "done":
            break


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
        ev = ev_by_id.get(r.tool_call_id or "")
        if ev is not None:
            matched.add(r.tool_call_id)
            sort_key = (0, ev["seq"])
            if not entry.get("args") and ev.get("args") is not None:
                entry["args"] = str(ev["args"])[:200]
            if ev.get("internal"):
                entry["internal"] = True
        else:
            sort_key = (1, r.started_at or r.created_at or datetime.min)
        ordered.append((sort_key, entry))
    # 事件独有调用（内部工具 / 策略拒绝等未入账本的调用）按事件序号并入
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


# 交付物类工具：其成功调用的参数即 Agent 实际产出的交付物内容
_ARTIFACT_TOOLS = ("write_file", "edit_file", "python_exec", "execute")


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

    picked: Dict[str, tuple] = {}  # key -> (label, text)
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


async def _execute_item(
    db: AsyncSession,
    eval_run_id: int,
    item: EvalRunItem,
    agent_config: Dict[str, Any],
    timeout_s: float,
    user_id: Optional[int],
) -> None:
    """执行单条用例：（多轮逐轮）起 Run → 收流 → 客观执行事实 → 评分 → 能力校验 → 回写。"""
    from app.services.agent_runtime import get_runtime_service

    # 先把所需字段快照到局部（db.rollback() 会过期 ORM 对象，之后属性访问触发懒加载会炸）
    item_id = item.id
    snap_input = item.snap_input or ""
    snap_expected = item.snap_expected or ""
    snap_criteria = item.snap_criteria or ""
    snap_method = item.snap_scoring_method
    snap_config = dict(item.snap_scoring_config or {})
    snap_meta = dict(item.snap_meta or {})

    started = _now()
    await db.execute(
        update(EvalRunItem).where(EvalRunItem.id == item_id).values(
            status="running", started_at=started))
    await db.commit()

    run_id: Optional[str] = None
    try:
        turn_inputs = [snap_input] + [
            str(t) for t in (snap_meta.get("turns") or []) if str(t).strip()]
        history: List[Dict[str, Any]] = []
        session_id: Optional[int] = None
        turn_stats: List[Dict[str, Any]] = []
        actual_output = ""
        run_status = ""

        for idx, text in enumerate(turn_inputs):
            user_message = {"role": "user", "content": text}
            run_id, session_id, stream = await get_runtime_service().start_run_with_session(
                agent_config=agent_config,
                messages=history + [user_message],
                user_id=user_id or 0,
                session_id=session_id,
                endpoint="eval",
                user_message=user_message,
            )
            await asyncio.wait_for(_drain_stream(stream), timeout=timeout_s)

            # 运行时在独立连接写入 Run 事实行：结束本会话旧事务，
            # 否则 MySQL REPEATABLE READ 快照读不到新提交的行（多轮第 2 轮起报「AgentRun 未找到」）
            await db.rollback()
            agent_run = None
            for _attempt in range(3):
                agent_run = (await db.execute(
                    select(AgentRun).where(AgentRun.id == run_id))).scalar_one_or_none()
                if agent_run is not None:
                    break
                await asyncio.sleep(0.5)
            if agent_run is None:
                raise RuntimeError(f"AgentRun 未找到: {run_id}")
            actual_output = agent_run.final_text or ""
            run_status = agent_run.status or ""
            facts = await _collect_run_facts(db, run_id)
            turn_stats.append({
                "turn": idx + 1, "run_id": run_id,
                "input": text, "output": actual_output,
                "status": run_status, **facts,
            })
            history.append(user_message)
            history.append({"role": "assistant", "content": actual_output})

        stats = _merge_turn_facts(turn_stats)
        stats["output_chars"] = len(actual_output or "")
        stats["turns_count"] = len(turn_inputs)
        stats["turns"] = [
            {k: t.get(k) for k in (
                "turn", "run_id", "input", "output", "status", "steps",
                "tool_calls", "tokens", "duration_s", "used_tools")}
            for t in turn_stats]

        # 评分输入：多轮时给裁判完整对话上下文
        if len(turn_inputs) > 1:
            judge_input = "\n".join(
                f"[{'用户' if h['role'] == 'user' else '助手'}] {h['content']}"
                for h in history[:-1])
        else:
            judge_input = snap_input

        # 交付物评分口径：工程构建类用例（include_artifacts）把工具交付物
        # （文件写入内容 / 成功执行的代码）并入评分文本，评分覆盖真实交付物
        artifact_text = ""
        if snap_config.get("include_artifacts"):
            artifact_text = await _collect_artifact_text(
                db, [str(t.get("run_id")) for t in turn_stats if t.get("run_id")])
        stats["artifact_chars"] = len(artifact_text)
        score_text = (actual_output or "") + artifact_text

        result = await score_output(
            db,
            method=snap_method,
            config=snap_config,
            input_text=judge_input,
            expected=snap_expected,
            criteria=snap_criteria,
            actual_output=score_text,
            # 最终回答原文：constraint 长度约束区分口径（min 测合并、max 仅测回答）
            output_text=actual_output or "",
        )

        # 工具能力客观校验：要求工具未被调用 / 禁用工具却被调用即判不通过（可用配置关闭）
        required_tools = [str(t) for t in (snap_meta.get("required_tools") or []) if t]
        used_tools = list(stats.get("used_tools") or [])
        missing_tools = [t for t in required_tools if t not in used_tools]
        enforce = bool(snap_config.get("enforce_required_tools", True))
        forbid_tools = bool(snap_config.get("forbid_tools"))
        expected_min_steps = snap_meta.get("expected_min_steps")
        steps_met = (expected_min_steps is None) or (
            int(stats.get("steps") or 0) >= int(expected_min_steps))
        capability = {
            "required_tools": required_tools,
            "used_tools": used_tools,
            "missing_tools": missing_tools,
            "zero_tool": not used_tools,
            "forbid_tools": forbid_tools,
            "expected_min_steps": expected_min_steps,
            "steps_met": steps_met,
        }
        stats["missing_tools"] = missing_tools
        stats["required_tools"] = required_tools
        stats["steps_met"] = steps_met
        detail = dict(result.get("detail") or {})
        checks: List[str] = []
        if required_tools and enforce and missing_tools:
            checks.append(f"必需工具未被调用：{','.join(missing_tools)}")
        if forbid_tools and used_tools:
            checks.append(f"任务要求不使用工具，但调用了：{','.join(used_tools)}")
        if checks:
            result["passed"] = False
            detail["capability_check"] = {
                "enforced": True,
                "missing_tools": missing_tools,
                "used_tools": used_tools,
                "checks": checks,
                "note": "工具使用不符合用例声明（客观校验）",
            }
        detail["capability"] = capability

        if run_status not in ("completed",) and result.get("passed") is None:
            status = "error"
        else:
            status = "passed" if result.get("passed") else (
                "failed" if result.get("passed") is False else "error")
        # Run 执行失败但评分给出通过判定时，仍以评分为准（模型可能给出正确答案）
        await db.execute(
            update(EvalRunItem).where(EvalRunItem.id == item_id).values(
                status=status,
                run_id=run_id,
                actual_output=actual_output,
                score=result.get("score"),
                passed=result.get("passed"),
                score_detail=detail,
                stats=stats,
                error=None if run_status == "completed"
                else f"AgentRun 状态: {run_status}",
                finished_at=_now(),
            ))
        await db.commit()
    except asyncio.TimeoutError:
        await db.rollback()
        await db.execute(
            update(EvalRunItem).where(EvalRunItem.id == item_id).values(
                status="error", run_id=run_id,
                error=f"执行超时（>{int(timeout_s)}s）", finished_at=_now()))
        await db.commit()
    except (ScoringError, Exception) as e:  # noqa: BLE001
        logger.exception("测评用例执行失败 eval_run=%s item=%s", eval_run_id, item_id)
        await db.rollback()
        await db.execute(
            update(EvalRunItem).where(EvalRunItem.id == item_id).values(
                status="error", run_id=run_id,
                error=str(e)[:2000], finished_at=_now()))
        await db.commit()


async def execute_eval_run(eval_run_id: int) -> None:
    """测评任务主循环（后台任务）：逐条执行用例并滚动更新聚合。"""
    async with AsyncSessionLocal() as db:
        eval_run = (await db.execute(
            select(EvalRun).where(EvalRun.id == eval_run_id))).scalar_one_or_none()
        if eval_run is None:
            logger.warning("execute_eval_run: 任务不存在 %s", eval_run_id)
            return
        if eval_run.status not in ("pending", "running"):
            return

        eval_run.status = "running"
        eval_run.started_at = eval_run.started_at or _now()
        await db.commit()

        config = dict(eval_run.config or {})
        timeout_s = float(config.get("timeout_s") or DEFAULT_ITEM_TIMEOUT_S)
        approval_mode = config.get("approval_mode") if config.get("approval_mode") in (
            "manual", "auto") else "auto"
        use_memory = bool(config.get("use_memory"))

        # Run 的 user_id 有外键约束：未指定创建人时兜底取一个真实用户
        user_id = eval_run.created_by
        if user_id is None:
            from app.models.user import User
            user_id = (await db.execute(
                select(User.id).order_by(User.id).limit(1))).scalar()

        try:
            agent = (await db.execute(
                select(Agent).where(Agent.id == eval_run.agent_id))).scalar_one_or_none()
            if agent is None:
                raise RuntimeError("Agent 不存在或已删除")
            agent_config = await _build_agent_config(db, agent, use_memory=use_memory)
            agent_config["approval_mode"] = approval_mode

            while True:
                # 取消检查
                cur = (await db.execute(
                    select(EvalRun.status).where(EvalRun.id == eval_run_id))).scalar()
                if cur == "cancel_requested":
                    await db.execute(
                        update(EvalRunItem).where(
                            EvalRunItem.eval_run_id == eval_run_id,
                            EvalRunItem.status.in_(("pending", "running")),
                        ).values(status="skipped", finished_at=_now()))
                    await db.commit()
                    break

                item = (await db.execute(
                    select(EvalRunItem).where(
                        EvalRunItem.eval_run_id == eval_run_id,
                        EvalRunItem.status == "pending",
                    ).order_by(EvalRunItem.position).limit(1))).scalar_one_or_none()
                if item is None:
                    break

                # claim：抢占式置 running，防并发重复执行
                claimed = await db.execute(
                    update(EvalRunItem).where(
                        EvalRunItem.id == item.id,
                        EvalRunItem.status == "pending",
                    ).values(status="running"))
                await db.commit()
                if claimed.rowcount == 0:
                    continue

                await _execute_item(db, eval_run_id, item, agent_config,
                                    timeout_s, user_id)
                await _refresh_aggregate(db, eval_run_id)

            # 终态
            agg = await _refresh_aggregate(db, eval_run_id)
            cur = (await db.execute(
                select(EvalRun.status).where(EvalRun.id == eval_run_id))).scalar()
            final = "cancelled" if cur == "cancel_requested" else "completed"
            await db.execute(
                update(EvalRun).where(EvalRun.id == eval_run_id).values(
                    status=final, finished_at=_now(), aggregate=agg))
            await db.commit()
        except Exception as e:  # noqa: BLE001
            logger.exception("测评任务执行失败 eval_run=%s", eval_run_id)
            await db.execute(
                update(EvalRun).where(EvalRun.id == eval_run_id).values(
                    status="failed", error=str(e)[:2000], finished_at=_now()))
            await db.commit()


# 后台任务引用（防 GC 提前回收）
_background_tasks: set = set()


async def start_eval_run_background(eval_run_id: int) -> None:
    """把测评任务丢到后台执行（事件循环内）。"""
    task = asyncio.create_task(execute_eval_run(eval_run_id))
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


async def cancel_eval_run(db: AsyncSession, eval_run: EvalRun) -> Dict[str, Any]:
    """取消测评：运行中置 cancel_requested，未开始用例标记 skipped。"""
    if eval_run.status in ("completed", "failed", "cancelled"):
        return {"status": eval_run.status, "changed": False}
    if eval_run.status == "running":
        eval_run.status = "cancel_requested"
    else:
        eval_run.status = "cancelled"
        eval_run.finished_at = _now()
        await db.execute(
            update(EvalRunItem).where(
                EvalRunItem.eval_run_id == eval_run.id,
                EvalRunItem.status.in_(("pending", "running")),
            ).values(status="skipped", finished_at=_now()))
    await db.commit()
    return {"status": eval_run.status, "changed": True}


async def resume_eval_run(db: AsyncSession, eval_run: EvalRun) -> Dict[str, Any]:
    """恢复测评：error/skipped 用例重置为 pending 后重新执行。"""
    if eval_run.status in ("pending", "running", "cancel_requested"):
        raise ValueError("任务尚在进行中，无需恢复")
    await db.execute(
        update(EvalRunItem).where(
            EvalRunItem.eval_run_id == eval_run.id,
            EvalRunItem.status.in_(("error", "skipped", "running")),
        ).values(status="pending", error=None, run_id=None,
                 score=None, passed=None, score_detail={}, stats={},
                 started_at=None, finished_at=None))
    eval_run.status = "pending"
    eval_run.error = None
    eval_run.finished_at = None
    await db.commit()
    await _refresh_aggregate(db, eval_run.id)
    return {"status": eval_run.status}
