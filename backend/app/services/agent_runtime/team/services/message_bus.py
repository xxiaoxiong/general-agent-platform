"""Message Bus：持久化消息、Inbox、Ack 与可恢复等待。

交付语义（任务书 §4.4）：
- 先持久化消息，再尝试唤醒接收方；
- 接收方运行中：消息留在 Inbox，由成员在下一轮安全边界读取；
- 接收方空闲：由调度器调用 ``wake_members_with_messages`` 启动；
- 至少一次投递：``deliver_next`` 原子标记 delivered，重复调用不重复返回；
- “进入收件箱”不等于“已阅读或已处理”，只有显式 ack 才算处理完成；
- ``idempotency_key`` 去重，重复发送返回既有消息、不产生新副作用。
"""
from __future__ import annotations

import asyncio
import json
import logging
import secrets
from typing import Any, Optional

from app.core.database import AsyncSessionLocal

from ..errors import MessageError, TeamNotFound
from ..repositories import MemberRepository, MessageRepository
from ..states import MESSAGE_ACKED, MESSAGE_DELIVERED, MESSAGE_PENDING

logger = logging.getLogger("ai_plat.team")

_MAX_PAYLOAD_CHARS = 20_000


class MessageBusService:
    def __init__(self, db_factory=AsyncSessionLocal):
        self._db_factory = db_factory

    async def send(
        self,
        team_run_id: str,
        *,
        sender_member_id: str,
        recipient_member_id: Optional[str],
        message_type: str = "text",
        payload: Optional[dict[str, Any]] = None,
        artifact_id: Optional[str] = None,
        correlation_id: Optional[str] = None,
        idempotency_key: Optional[str] = None,
    ) -> dict[str, Any]:
        body = payload or {}
        if len(json.dumps(body, ensure_ascii=False, default=str)) > _MAX_PAYLOAD_CHARS:
            raise MessageError(
                "消息 payload 过大：请把长内容写入 team_artifact_create，再用 artifact_id 引用"
            )
        async with self._db_factory() as db:
            members = {row.member_id for row in await MemberRepository(db).list(team_run_id)}
            if sender_member_id not in members:
                raise TeamNotFound(f"发送者 {sender_member_id} 不属于该团队")
            if recipient_member_id is not None and recipient_member_id not in members:
                raise TeamNotFound(f"接收者 {recipient_member_id} 不属于该团队")
            row, created = await MessageRepository(db).create(
                team_run_id=team_run_id,
                message_id=f"msg-{secrets.token_hex(10)}",
                sender_member_id=sender_member_id,
                recipient_member_id=recipient_member_id,
                message_type=message_type,
                payload=body,
                artifact_id=artifact_id,
                correlation_id=correlation_id,
                idempotency_key=idempotency_key,
            )
            await db.commit()
            result = self._serialize(row)
            result["created"] = created
            return result

    async def list_inbox(
        self,
        team_run_id: str,
        member_id: str,
        *,
        limit: int = 50,
        include_broadcast: bool = False,
        mark_delivered: bool = True,
    ) -> list[dict[str, Any]]:
        async with self._db_factory() as db:
            repo = MessageRepository(db)
            rows = await repo.list_inbox(
                team_run_id, member_id,
                limit=limit,
                include_broadcast=include_broadcast,
            )
            if mark_delivered:
                await repo.mark_delivered(team_run_id, [row.message_id for row in rows])
                await db.commit()
            return [self._serialize(row, delivered=mark_delivered) for row in rows]

    async def has_pending(self, team_run_id: str, member_id: str) -> bool:
        """是否存在待处理消息（不改状态、不标记 delivered）。"""
        async with self._db_factory() as db:
            rows = await MessageRepository(db).list_inbox(
                team_run_id, member_id, limit=1,
            )
            return bool(rows)

    async def ack(self, team_run_id: str, member_id: str, message_id: str) -> str:
        async with self._db_factory() as db:
            outcome = await MessageRepository(db).ack(
                team_run_id, message_id, recipient_member_id=member_id,
            )
            await db.commit()
        if outcome == "missing":
            raise TeamNotFound(f"消息 {message_id} 不存在")
        if outcome == "forbidden":
            raise MessageError(f"消息 {message_id} 不属于当前成员")
        return outcome

    async def get(self, team_run_id: str, message_id: str) -> dict[str, Any]:
        async with self._db_factory() as db:
            row = await MessageRepository(db).get(team_run_id, message_id)
            if row is None:
                raise TeamNotFound(f"消息 {message_id} 不存在")
            return self._serialize(row)

    async def wait_for_message(
        self,
        team_run_id: str,
        member_id: str,
        *,
        timeout: Optional[float] = None,
        poll_interval: float = 0.5,
    ) -> Optional[dict[str, Any]]:
        """可恢复等待：轮询数据库（不依赖进程内存），超时返回 None。"""
        from app.core.config import settings

        timeout = float(
            timeout if timeout is not None else settings.AGENT_TEAM_MESSAGE_WAIT_SECONDS
        )
        deadline = asyncio.get_running_loop().time() + max(0.0, timeout)
        while True:
            async with self._db_factory() as db:
                row = await MessageRepository(db).deliver_next(team_run_id, member_id)
                await db.commit()
                if row is not None:
                    return self._serialize(row, delivered=True)
            if asyncio.get_running_loop().time() >= deadline:
                return None
            await asyncio.sleep(max(0.05, poll_interval))

    @staticmethod
    def _serialize(row, *, delivered: bool = False) -> dict[str, Any]:
        status = row.status
        if delivered and status == MESSAGE_PENDING:
            status = MESSAGE_DELIVERED
        return {
            "message_id": row.message_id,
            "team_run_id": row.team_run_id,
            "sender_member_id": row.sender_member_id,
            "recipient_member_id": row.recipient_member_id,
            "message_type": row.message_type,
            "payload": row.payload or {},
            "artifact_id": row.artifact_id,
            "correlation_id": row.correlation_id,
            "status": status,
            "created_at": row.created_at.isoformat() if row.created_at else None,
            "delivered_at": (
                row.delivered_at.isoformat() if row.delivered_at else None
            ),
            "acked_at": row.acked_at.isoformat() if row.acked_at else None,
            "acked": status == MESSAGE_ACKED,
        }
