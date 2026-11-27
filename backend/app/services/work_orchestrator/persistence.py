"""WorkPlan 持久化：计划快照 + 运行时状态的幂等写入。

任务书 §8.3：
- 计划快照写入 ``AgentRun.input_payload.execution_context.work_plan``；
- 写入必须带 Lease/Fencing 校验，不能无保护地覆盖整个 ``input_payload``；
- 计划生成具备幂等性：写入成功后即使 Worker 在发布事件前退出，恢复时也读取原计划；
- 运行时步骤状态由 LangGraph Checkpoint 保存，本模块只保存可审计快照与事件视图。
"""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select, update

from app.core.database import AsyncSessionLocal
from app.models.agent import AgentRun
from app.services.agent_runtime.lifecycle import RunExecutionLease
from app.services.agent_runtime.repositories import RunRepository
from app.services.agent_runtime.repositories.errors import PersistenceError

from .schemas import WorkPlanRecord, plan_record_from_snapshot

logger = logging.getLogger(__name__)


async def load_work_plan_record(run_id: str) -> WorkPlanRecord | None:
    from app.services.agent_runtime.decision.persistence import load_input_payload

    payload = await load_input_payload(run_id)
    route = payload.get("execution_context") or {}
    return plan_record_from_snapshot(route.get("work_plan"))


async def persist_work_plan_record(
    run_id: str,
    record: WorkPlanRecord,
    *,
    lease: RunExecutionLease | None,
) -> None:
    """原子更新 execution_context.work_plan（可选 Lease/Fencing 校验）。

    关键写入失败向上抛出 PersistenceError，不允许"计划未保存但继续执行"。
    """
    try:
        async with AsyncSessionLocal() as db:
            if lease is not None:
                await RunRepository(db).verify_running_lease(lease)
            row = (await db.execute(
                select(AgentRun.id, AgentRun.input_payload)
                .where(AgentRun.id == run_id)
                .with_for_update()
            )).first()
            if row is None:
                raise PersistenceError(f"run {run_id} 不存在，无法保存 WorkPlan")
            payload = dict(row[1] or {})
            route = dict(payload.get("execution_context") or {})
            route["work_plan"] = record.model_dump(mode="json")
            payload["execution_context"] = route
            await db.execute(
                update(AgentRun).where(AgentRun.id == run_id).values(input_payload=payload)
            )
            await db.commit()
    except PersistenceError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise PersistenceError(f"run {run_id} WorkPlan 持久化失败: {exc}") from exc


async def persist_work_plan_diagnostic(
    run_id: str,
    diagnostic: dict[str, Any],
    *,
    lease: RunExecutionLease | None,
) -> None:
    """保存编排诊断（shadow/降级原因），供审计与评测采样。"""
    try:
        async with AsyncSessionLocal() as db:
            if lease is not None:
                await RunRepository(db).verify_running_lease(lease)
            row = (await db.execute(
                select(AgentRun.id, AgentRun.input_payload)
                .where(AgentRun.id == run_id)
                .with_for_update()
            )).first()
            if row is None:
                return
            payload = dict(row[1] or {})
            route = dict(payload.get("execution_context") or {})
            history = list(route.get("work_plan_diagnostics") or [])
            history.append(diagnostic)
            route["work_plan_diagnostics"] = history[-8:]
            payload["execution_context"] = route
            await db.execute(
                update(AgentRun).where(AgentRun.id == run_id).values(input_payload=payload)
            )
            await db.commit()
    except Exception:  # noqa: BLE001 诊断失败不影响主链路
        logger.warning("run %s WorkPlan 诊断写入失败（忽略）", run_id, exc_info=True)
