"""测评运行器：对某 Agent × 某数据集批量执行用例并评分聚合。

执行模型：
- 每条用例起一次真实 AgentRun（start_run_with_session，approval_mode 默认 auto、
  默认不带记忆，保证测评可复现），消费旧契约事件流至 done 后取 final_text；
- 逐条 claim（UPDATE ... WHERE status='pending'）防并发重复执行；
- 评分走 scoring.score_output，运行统计走 evolution.metrics.compute_deterministic；
- 支持取消（pending→skipped）与恢复（重跑未完成用例）。
"""
import asyncio
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import AsyncSessionLocal
from app.models.agent import Agent, AgentRun
from app.models.evaluation import EvalDataset, EvalDatasetItem, EvalRun, EvalRunItem

from .scoring import ScoringError, resolve_method, score_output

logger = logging.getLogger(__name__)

# 用例执行默认超时（秒）
DEFAULT_ITEM_TIMEOUT_S = 180


def _now() -> datetime:
    return datetime.utcnow()


def compute_aggregate(items: List[EvalRunItem]) -> Dict[str, Any]:
    """由用例结果行聚合汇总指标（确定性计算，无 LLM）。"""
    def _avg(vals):
        vals = [v for v in vals if v is not None]
        return round(sum(vals) / len(vals), 3) if vals else None

    total = len(items)
    completed = [i for i in items if i.status in ("passed", "failed", "error")]
    passed = sum(1 for i in items if i.status == "passed")
    failed = sum(1 for i in items if i.status == "failed")
    error = sum(1 for i in items if i.status == "error")
    skipped = sum(1 for i in items if i.status == "skipped")
    stats = [i.stats or {} for i in items if i.status in ("passed", "failed")]
    return {
        "total": total,
        "completed": len(completed),
        "passed": passed,
        "failed": failed,
        "error": error,
        "skipped": skipped,
        "pending": sum(1 for i in items if i.status in ("pending", "running")),
        "pass_rate": round(passed / len(completed), 4) if completed else None,
        "avg_score": _avg([i.score for i in items if i.status in ("passed", "failed")]),
        "avg_duration_s": _avg([s.get("duration_s") for s in stats]),
        "total_tokens": sum(int(s.get("tokens") or 0) for s in stats),
        "total_tool_calls": sum(int(s.get("tool_calls") or 0) for s in stats),
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
    """创建测评任务与用例结果行（快照数据集内容）。"""
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


async def _execute_item(
    db: AsyncSession,
    eval_run: EvalRun,
    item: EvalRunItem,
    agent_config: Dict[str, Any],
    timeout_s: float,
    user_id: Optional[int],
) -> None:
    """执行单条用例：起 Run → 收流 → 取结果 → 评分 → 回写。"""
    from app.services.agent_runtime import get_runtime_service
    from app.services.evolution.metrics import compute_deterministic

    started = _now()
    await db.execute(
        update(EvalRunItem).where(EvalRunItem.id == item.id).values(
            status="running", started_at=started))
    await db.commit()

    run_id: Optional[str] = None
    try:
        user_message = {"role": "user", "content": item.snap_input}
        run_id, _session_id, stream = await get_runtime_service().start_run_with_session(
            agent_config=agent_config,
            messages=[user_message],
            user_id=user_id or 0,
            session_id=None,
            endpoint="eval",
            user_message=user_message,
        )
        await asyncio.wait_for(_drain_stream(stream), timeout=timeout_s)

        agent_run = (await db.execute(
            select(AgentRun).where(AgentRun.id == run_id))).scalar_one_or_none()
        if agent_run is None:
            raise RuntimeError(f"AgentRun 未找到: {run_id}")
        actual_output = agent_run.final_text or ""
        stats_full = await compute_deterministic(db, agent_run)
        stats = {
            "duration_s": stats_full.get("duration_s"),
            "tokens": stats_full.get("tokens"),
            "steps": stats_full.get("steps"),
            "tool_calls": stats_full.get("tool_calls"),
            "run_status": stats_full.get("run_status"),
        }

        result = await score_output(
            db,
            method=item.snap_scoring_method,
            config=item.snap_scoring_config,
            input_text=item.snap_input,
            expected=item.snap_expected,
            criteria=item.snap_criteria,
            actual_output=actual_output,
        )
        if agent_run.status not in ("completed",) and result.get("passed") is None:
            status = "error"
        else:
            status = "passed" if result.get("passed") else (
                "failed" if result.get("passed") is False else "error")
        # Run 执行失败但评分给出通过判定时，仍以评分为准（模型可能给出正确答案）
        await db.execute(
            update(EvalRunItem).where(EvalRunItem.id == item.id).values(
                status=status,
                run_id=run_id,
                actual_output=actual_output,
                score=result.get("score"),
                passed=result.get("passed"),
                score_detail=result.get("detail") or {},
                stats=stats,
                error=None if agent_run.status == "completed"
                else f"AgentRun 状态: {agent_run.status}",
                finished_at=_now(),
            ))
        await db.commit()
    except asyncio.TimeoutError:
        await db.execute(
            update(EvalRunItem).where(EvalRunItem.id == item.id).values(
                status="error", run_id=run_id,
                error=f"执行超时（>{int(timeout_s)}s）", finished_at=_now()))
        await db.commit()
    except (ScoringError, Exception) as e:  # noqa: BLE001
        logger.exception("测评用例执行失败 eval_run=%s item=%s", eval_run.id, item.id)
        await db.execute(
            update(EvalRunItem).where(EvalRunItem.id == item.id).values(
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

                await _execute_item(db, eval_run, item, agent_config,
                                    timeout_s, eval_run.created_by)
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


def start_eval_run_background(eval_run_id: int) -> None:
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
