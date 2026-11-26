"""决策快照持久化：写入 Run.input_payload.execution_context，恢复时复用。

- 进程重启后不重复调用决策模型；
- 决策快照与执行上下文同源保存，可从 Run ID 回看模型建议与最终接受结果；
- 会话消息的 document_run_id 标记在自动模式下由 Run 内决策补写，
  保证后续轮次的历史锚点与人工输入回收仍可用。
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from sqlalchemy import select, update

logger = logging.getLogger(__name__)


async def persist_execution_context(run_id: str, execution_context: dict[str, Any]) -> None:
    """原子替换 Run 输入快照中的执行上下文（决策失败向上抛出）。"""
    from app.core.database import AsyncSessionLocal
    from app.models.agent import AgentRun

    async with AsyncSessionLocal() as db:
        payload = await db.scalar(
            select(AgentRun.input_payload).where(AgentRun.id == run_id)
        )
        updated = dict(payload or {})
        updated["execution_context"] = execution_context
        await db.execute(
            update(AgentRun).where(AgentRun.id == run_id).values(input_payload=updated)
        )
        await db.commit()


async def load_input_payload(run_id: str) -> dict[str, Any]:
    from app.core.database import AsyncSessionLocal
    from app.models.agent import AgentRun

    async with AsyncSessionLocal() as db:
        payload = await db.scalar(
            select(AgentRun.input_payload).where(AgentRun.id == run_id)
        )
    return dict(payload or {})


async def mark_message_document_run(session_id: int, message_id: str, run_id: str) -> None:
    """给触发本轮 Run 的用户消息补写 document_run_id（best-effort）。"""
    if not message_id:
        return
    from app.core.database import AsyncSessionLocal
    from app.models.agent import AgentSession

    for _ in range(3):
        async with AsyncSessionLocal() as db:
            row = (await db.execute(
                select(AgentSession).where(AgentSession.id == session_id).with_for_update()
            )).scalar_one_or_none()
            if row is None:
                return
            messages = list(row.messages or [])
            target = next(
                (item for item in messages
                 if isinstance(item, dict) and item.get("message_id") == message_id),
                None,
            )
            if target is None:
                return
            if target.get("document_run_id") == run_id:
                return
            target["document_run_id"] = run_id
            result = await db.execute(
                update(AgentSession)
                .where(
                    AgentSession.id == session_id,
                    AgentSession.version == (row.version or 0),
                )
                .values(
                    messages=messages,
                    version=(row.version or 0) + 1,
                    updated_at=datetime.utcnow(),
                )
            )
            if result.rowcount == 1:
                await db.commit()
                return
    logger.warning("会话 %s 消息 %s 文档标记写入冲突（不影响执行）", session_id, message_id)
