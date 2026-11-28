"""执行中补充要求（通用能力）。

用户在 Run 执行过程中提出的新信息/纠正，会在最近的安全边界（下一次模型调用前）
注入当前执行上下文；目标模式（/goal）只是额外解除预算限制，补充能力本身不限模式。

- 受理：与 Run 事件日志、会话历史同一事务写入（API 侧）；
- 注入：执行侧在最近的安全边界读取并注入上下文；
- 去重：按 ``message_id`` 稳定 ID，崩溃恢复后重复读取不会重复注入，
  ``delivered_at`` 仅用于可观测性。
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import HumanMessage
from sqlalchemy import select, update

from app.core.database import AsyncSessionLocal
from app.models.agent import AgentRun, AgentRunSteering

from .context import RuntimeContext
from .events import (
    RUN_STEERED, RUN_STEERING_APPLIED, EventBus, RuntimeEvent, notify_wakeup,
)
from .repositories.session_repository import SessionRepository

logger = logging.getLogger(__name__)

MAX_STEERING_CHARS = 8000
# 注入模型上下文的引导语：补充要求优先于原计划，避免模型把纠正当作参考信息忽略
STEERING_MESSAGE_PREFIX = "用户在执行过程中补充/纠正的要求（优先于原计划，必须在后续执行中落实）："
_STEERING_ID_PREFIX = "steer"


class SteeringError(ValueError):
    """补充要求受理失败（明确 HTTP 语义，调用方不得静默忽略）。"""

    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code


def steering_state_id(message_id: str) -> str:
    """注入图状态时使用的稳定消息 ID（按 ID 去重，恢复后重复注入无副作用）。"""
    return f"{_STEERING_ID_PREFIX}-{message_id}"


async def submit_steering(run_id: str, user_id: int, content: str) -> Dict[str, Any]:
    """受理执行中的补充要求：写库 + 事件 + 会话历史（单事务）。"""
    text = (content or "").strip()
    if not text:
        raise SteeringError(422, "补充内容不能为空")
    if len(text) > MAX_STEERING_CHARS:
        raise SteeringError(422, f"补充内容不能超过 {MAX_STEERING_CHARS} 字")

    async with AsyncSessionLocal() as db:
        run = (await db.execute(
            select(AgentRun).where(AgentRun.id == run_id).with_for_update()
        )).scalar_one_or_none()
        if run is None or run.user_id != user_id:
            raise SteeringError(404, "运行不存在")
        route = (run.input_payload or {}).get("execution_context") or {}
        goal_team = bool((run.agent_config_snapshot or {}).get("goal_mode"))
        if route.get("team_mode") and not goal_team:
            # 普通团队 Run 未接入补充注入；目标模式的团队由 Goal 契约统一记录
            raise SteeringError(409, "团队协作任务暂不支持执行中补充")
        if run.status not in ("pending", "running") or run.cancel_requested:
            raise SteeringError(409, "任务当前不在可补充的执行状态")

        row = AgentRunSteering(
            run_id=run_id, user_id=user_id,
            message_id=uuid.uuid4().hex, content=text,
        )
        db.add(row)
        await db.flush()
        message_id = row.message_id

        # Goal Runtime：执行中补充要求必须形成目标契约新 revision，
        # 并把已通过项标记为待复核；只注入消息不算目标契约已更新。
        goal_revision = None
        try:
            from app.services.goal_runtime.contracts import record_steering_revision

            goal_revision = await record_steering_revision(
                db, run_id, text, message_id,
            )
        except Exception:  # noqa: BLE001 目标契约写入失败不得阻断补充受理
            logger.warning("run %s 补充要求写入目标契约失败（忽略）", run_id, exc_info=True)

        event = RuntimeEvent(
            type=RUN_STEERED, run_id=run_id, thread_id=run.thread_id or "",
            data={
                "message_id": message_id, "content": text,
                **({"goal_revision": goal_revision} if goal_revision else {}),
            },
        )
        event.sequence = await EventBus.persist_in_transaction(event, db)
        if run.session_id:
            # 会话历史按稳定 message_id 去重：刷新页面、下一轮对话仍能看到补充要求
            await SessionRepository(db).append_messages(run.session_id, [{
                "message_id": message_id,
                "role": "user",
                "content": text,
                "created_at": datetime.utcnow().isoformat(),
                "steering_run_id": run_id,
            }])
        await db.commit()

    notify_wakeup(run_id)
    return {
        "run_id": run_id, "status": "queued",
        "message_id": message_id, "sequence": event.sequence,
    }


async def load_steering(run_id: str) -> List[Dict[str, Any]]:
    """读取 Run 的全部补充要求（含已注入项，按稳定 ID 去重决定是否注入）。"""
    async with AsyncSessionLocal() as db:
        rows = (await db.execute(
            select(
                AgentRunSteering.id, AgentRunSteering.message_id,
                AgentRunSteering.content, AgentRunSteering.delivered_at,
            ).where(AgentRunSteering.run_id == run_id)
            .order_by(AgentRunSteering.id)
            .limit(200)
        )).all()
    return [
        {"id": row[0], "message_id": row[1], "content": row[2], "delivered_at": row[3]}
        for row in rows
    ]


async def mark_steering_delivered(ids: List[int], via: str) -> None:
    """标记已注入（可观测性；首次注入时间，不参与去重决策）。"""
    if not ids:
        return
    async with AsyncSessionLocal() as db:
        await db.execute(
            update(AgentRunSteering)
            .where(AgentRunSteering.id.in_(ids), AgentRunSteering.delivered_at.is_(None))
            .values(delivered_at=datetime.utcnow(), delivered_via=via),
            execution_options={"synchronize_session": False},
        )
        await db.commit()


async def publish_steering_applied(
    bus: Optional[EventBus], run_id: str, thread_id: str, rows: List[Dict[str, Any]],
) -> None:
    if bus is None or not rows:
        return
    try:
        await bus.publish(RuntimeEvent(
            type=RUN_STEERING_APPLIED, run_id=run_id, thread_id=thread_id or "",
            data={"messages": [
                {"message_id": row["message_id"], "content": row["content"][:2000]}
                for row in rows
            ]},
        ))
    except Exception:  # noqa: BLE001 可观测性失败不得打断执行
        logger.warning("run %s 补充要求注入事件发布失败", run_id, exc_info=True)


class SteeringMiddleware(AgentMiddleware):
    """每次模型调用前把新的补充要求注入图状态（通用能力）。"""

    def __init__(self, ctx: RuntimeContext, bus: Optional[EventBus] = None):
        super().__init__()
        self.ctx = ctx
        self.bus = bus

    async def abefore_model(self, state, runtime):
        rows = await load_steering(self.ctx.run_id)
        if not rows:
            return None
        existing = {
            str(getattr(message, "id", "") or "")
            for message in (state.get("messages") or [])
        }
        fresh = [
            row for row in rows
            if steering_state_id(row["message_id"]) not in existing
        ]
        if not fresh:
            return None
        await mark_steering_delivered([row["id"] for row in fresh], "agent")
        await publish_steering_applied(self.bus, self.ctx.run_id, self.ctx.thread_id, fresh)
        return {"messages": [
            HumanMessage(
                id=steering_state_id(row["message_id"]),
                content=f"{STEERING_MESSAGE_PREFIX}\n{row['content']}",
            )
            for row in fresh
        ]}
