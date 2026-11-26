"""评估器：周期扫描终态 Run → 确定性指标 + LLM-as-judge → agent_evaluations。

- 并发安全：INSERT UNIQUE(run_id) 抢占（IntegrityError=已领）+ Redis 进程锁
  （多 UVICORN_WORKERS 防重复调度；锁失效时唯一约束兜底）；
- Langfuse 不可用时降级为仅确定性指标（不阻塞、不报错）。
"""
import asyncio
import logging
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from sqlalchemy import and_, or_, select
from sqlalchemy.exc import IntegrityError

from app.core.config import settings
from app.core.database import AsyncSessionLocal
from app.models.agent import AgentRun
from app.models.evolution import AgentEvaluation

logger = logging.getLogger(__name__)

TERMINAL_STATUSES = ("completed", "failed", "cancelled", "limit_exceeded")
JUDGE_STATUSES = ("completed", "failed", "limit_exceeded")  # cancelled 不值得花 token
_LOCK_KEY = "evolution:evaluator:lock"
_MAX_ATTEMPTS = 3  # 失败评估的最大尝试次数（超过后不再自动重试）


def production_run_condition():
    """Evolution 只处理生产来源 Run（任务书 §6：Benchmark 不混入生产统计）。

    ``agent_runs.source`` 老数据为空，按生产运行对待；``evaluation`` 为
    标准 Benchmark 测评 Run，由 Evaluation 模块自行评分，Evolution 不得再次计分。
    """
    from sqlalchemy import or_

    return or_(AgentRun.source.is_(None), AgentRun.source != "evaluation")


async def evaluate_run(db, run: AgentRun) -> Optional[int]:
    """评估单个 Run（调用方需持有 db 会话）。返回 evaluation id；不可评估返回 None。"""
    from app.services.evolution.judge import JudgeError, judge_run
    from app.services.evolution.metrics import compute_deterministic
    from app.services.evolution import langfuse_api

    # 先取值：rollback 后 ORM 属性过期，except 路径不能再触懒加载
    run_id, agent_id = run.id, run.agent_id
    version_id, session_id = run.agent_version_id, run.session_id

    # 抢占：UNIQUE(run_id) 保证幂等（与评估同事务，rollback 时一并撤销）
    ev = AgentEvaluation(
        run_id=run_id,
        agent_id=agent_id,
        agent_version_id=version_id,
        session_id=session_id,
        status="pending",
        langfuse_trace_id=None,
    )
    db.add(ev)
    try:
        await db.flush()
    except IntegrityError:
        await db.rollback()
        # 二次进入走更新路径：仅失败且尝试未达上限时接管重试
        ev = (await db.execute(
            select(AgentEvaluation).where(AgentEvaluation.run_id == run_id)
        )).scalar_one_or_none()
        if ev is None or ev.status != "failed" or (ev.attempts or 0) >= _MAX_ATTEMPTS:
            return None
        ev.status = "pending"
        await db.refresh(run)  # rollback 使 run 属性过期，重新加载后再评估

    try:
        deterministic = await compute_deterministic(db, run)
        ev.deterministic = deterministic
        ev.success = bool(deterministic.get("success"))
        ev.overall = deterministic.get("deterministic_score")

        judged: Dict[str, Any] = {}
        if run.status in JUDGE_STATUSES:
            try:
                judged = await judge_run(db, run, deterministic)
                ev.judge_scores = judged
                ev.judge_model_config_id = judged.get("judge_model_config_id")
                ev.overall = judged.get("overall")
                ev.success = ev.success and judged.get("verdict") == "pass"
            except JudgeError as e:
                logger.warning("run %s judge 失败（保留确定性指标）: %s", run.id, e)
                ev.error = f"judge_failed: {str(e)[:500]}"

        # 评估分回写 Langfuse（Trace 详情可见；失败忽略）
        from app.observability.langfuse_tracing import run_trace_id
        trace_id = run_trace_id(run.id)
        ev.langfuse_trace_id = trace_id
        scores: List[Dict[str, Any]] = [
            {"name": "agent_deterministic_score",
             "value": deterministic.get("deterministic_score", 0.0)},
            {"name": "agent_overall", "value": ev.overall or 0.0,
             "comment": judged.get("verdict", "")},
        ]
        for dim, val in (judged.get("dimensions") or {}).items():
            scores.append({"name": f"agent_{dim}", "value": val})
        await langfuse_api.write_scores(trace_id, scores)

        ev.status = "computed"
        ev.evaluated_at = datetime.utcnow()
        await db.commit()
        return ev.id
    except Exception as e:  # noqa: BLE001
        await db.rollback()
        logger.warning("run %s 评估失败: %s", run_id, e)
        # 失败留痕（独立短事务；抢占随 rollback 撤销，需补插失败行保留尝试次数）
        async with AsyncSessionLocal() as db2:
            row = (await db2.execute(
                select(AgentEvaluation).where(AgentEvaluation.run_id == run_id)
            )).scalar_one_or_none()
            if row is not None:
                row.status = "failed"
                row.attempts = (row.attempts or 0) + 1
                row.error = str(e)[:1000]
            else:
                db2.add(AgentEvaluation(
                    run_id=run_id, agent_id=agent_id,
                    agent_version_id=version_id, session_id=session_id,
                    status="failed", attempts=1, error=str(e)[:1000]))
            await db2.commit()
        return None


async def evaluate_pending_runs(limit: Optional[int] = None) -> int:
    """扫描并评估一批终态未评估 Run；返回实际评估数。"""
    limit = limit or int(getattr(settings, "EVOLUTION_EVAL_BATCH_SIZE", 10) or 10)
    done = 0
    async with AsyncSessionLocal() as db:
        # 扫描只取 id：整行 AgentRun 含大 JSON/TEXT 列，MySQL ORDER BY 把它们
        # 拷入 sort buffer 会触发 "Out of sort memory"（1038）；整行在循环内按 id 重取
        run_ids = (await db.execute(
            select(AgentRun.id)
            .outerjoin(AgentEvaluation, AgentEvaluation.run_id == AgentRun.id)
            .where(
                AgentRun.status.in_(TERMINAL_STATUSES),
                AgentRun.finished_at.isnot(None),
                AgentRun.finished_at < datetime.utcnow() - timedelta(seconds=30),
                production_run_condition(),
                or_(AgentEvaluation.id.is_(None),
                    and_(AgentEvaluation.status == "failed",
                         AgentEvaluation.attempts < _MAX_ATTEMPTS)),
            )
            .order_by(AgentRun.finished_at.desc())
            .limit(limit)
        )).scalars().all()
        for run_id in run_ids:
            # 逐 Run 独立短事务；LLM 调用不持有长事务
            async with AsyncSessionLocal() as db_run:
                run_row = (await db_run.execute(
                    select(AgentRun).where(AgentRun.id == run_id))).scalar_one_or_none()
                if run_row is None:
                    continue
                result = await evaluate_run(db_run, run_row)
                if result is not None:
                    done += 1
    return done


async def evaluator_loop(stop_event: asyncio.Event) -> None:
    """周期评估循环（lifespan 启动；Redis 锁防多 Worker 重复）。"""
    interval = max(30, int(getattr(settings, "EVOLUTION_EVAL_INTERVAL_SECONDS", 300) or 300))
    logger.info("evolution evaluator 启动（间隔 %ss）", interval)
    while not stop_event.is_set():
        try:
            locked = False
            try:
                from app.core.redis import get_redis
                redis = await get_redis()
                locked = bool(await redis.set(
                    _LOCK_KEY, "1", nx=True, ex=max(10, interval - 10)))
            except Exception:  # noqa: BLE001
                locked = True  # Redis 不可用时不阻塞评估
            if locked:
                try:
                    n = await evaluate_pending_runs()
                    if n:
                        logger.info("evolution evaluator 本轮评估 %s 个 Run", n)
                    try:
                        from app.services.evolution.evolution_advisor import check_validations
                        async with AsyncSessionLocal() as db_v:
                            await check_validations(db_v)
                    except Exception as e:  # noqa: BLE001
                        logger.warning("evolution 变更验证检查失败（继续）: %s", e)
                finally:
                    try:
                        from app.core.redis import get_redis
                        redis = await get_redis()
                        await redis.delete(_LOCK_KEY)
                    except Exception:  # noqa: BLE001
                        pass
        except Exception as e:  # noqa: BLE001
            logger.warning("evolution evaluator 循环异常（继续）: %s", e)
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass
    logger.info("evolution evaluator 已停止")
