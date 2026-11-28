"""Message Bus 仓库：持久化先于唤醒，按 message_id / idempotency_key 去重。"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from sqlalchemy import or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.team import TeamMessage

from ..states import MESSAGE_ACKED, MESSAGE_DELIVERED, MESSAGE_PENDING


class MessageRepository:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def get(self, team_run_id: str, message_id: str) -> Optional[TeamMessage]:
        return await self.db.scalar(
            select(TeamMessage).where(
                TeamMessage.team_run_id == team_run_id,
                TeamMessage.message_id == message_id,
            )
        )

    async def get_by_idempotency_key(
        self, team_run_id: str, idempotency_key: str,
    ) -> Optional[TeamMessage]:
        return await self.db.scalar(
            select(TeamMessage).where(
                TeamMessage.team_run_id == team_run_id,
                TeamMessage.idempotency_key == idempotency_key,
            )
        )

    async def create(
        self,
        *,
        team_run_id: str,
        message_id: str,
        sender_member_id: str,
        recipient_member_id: Optional[str],
        message_type: str = "text",
        payload: Optional[dict[str, Any]] = None,
        artifact_id: Optional[str] = None,
        correlation_id: Optional[str] = None,
        idempotency_key: Optional[str] = None,
    ) -> tuple[TeamMessage, bool]:
        """返回 (消息, 是否新建)。重复 idempotency_key 返回既有记录。"""
        if idempotency_key:
            existing = await self.get_by_idempotency_key(team_run_id, idempotency_key)
            if existing is not None:
                return existing, False
        row = TeamMessage(
            team_run_id=team_run_id,
            message_id=message_id,
            sender_member_id=sender_member_id,
            recipient_member_id=recipient_member_id,
            message_type=message_type[:40],
            payload=payload or {},
            artifact_id=artifact_id,
            correlation_id=correlation_id,
            idempotency_key=idempotency_key,
            status=MESSAGE_PENDING,
        )
        try:
            async with self.db.begin_nested():
                self.db.add(row)
                await self.db.flush()
        except IntegrityError:
            existing = None
            if idempotency_key:
                existing = await self.get_by_idempotency_key(team_run_id, idempotency_key)
            if existing is None:
                existing = await self.get(team_run_id, message_id)
            if existing is None:
                raise
            return existing, False
        return row, True

    async def list_inbox(
        self,
        team_run_id: str,
        recipient_member_id: str,
        *,
        statuses: tuple[str, ...] = (MESSAGE_PENDING, MESSAGE_DELIVERED),
        limit: int = 50,
        include_broadcast: bool = False,
    ) -> list[TeamMessage]:
        conditions = [
            TeamMessage.team_run_id == team_run_id,
            TeamMessage.status.in_(list(statuses)),
        ]
        if include_broadcast:
            conditions.append(or_(
                TeamMessage.recipient_member_id == recipient_member_id,
                TeamMessage.recipient_member_id.is_(None),
            ))
        else:
            conditions.append(TeamMessage.recipient_member_id == recipient_member_id)
        rows = await self.db.execute(
            select(TeamMessage)
            .where(*conditions)
            .order_by(TeamMessage.id.asc())
            .limit(max(1, limit))
        )
        return list(rows.scalars().all())

    async def mark_delivered(self, team_run_id: str, message_ids: list[str]) -> int:
        if not message_ids:
            return 0
        result = await self.db.execute(
            update(TeamMessage)
            .where(
                TeamMessage.team_run_id == team_run_id,
                TeamMessage.message_id.in_(message_ids),
                TeamMessage.status == MESSAGE_PENDING,
            )
            .values(status=MESSAGE_DELIVERED, delivered_at=datetime.utcnow(),
                    updated_at=datetime.utcnow())
        )
        return result.rowcount or 0

    async def deliver_next(
        self, team_run_id: str, recipient_member_id: str,
    ) -> Optional[TeamMessage]:
        """原子领取一条待投递消息（pending -> delivered），用于可恢复等待。"""
        row = await self.db.scalar(
            select(TeamMessage)
            .where(
                TeamMessage.team_run_id == team_run_id,
                TeamMessage.recipient_member_id == recipient_member_id,
                TeamMessage.status == MESSAGE_PENDING,
            )
            .order_by(TeamMessage.id.asc())
            .limit(1)
        )
        if row is None:
            return None
        result = await self.db.execute(
            update(TeamMessage)
            .where(
                TeamMessage.team_run_id == team_run_id,
                TeamMessage.message_id == row.message_id,
                TeamMessage.status == MESSAGE_PENDING,
            )
            .values(status=MESSAGE_DELIVERED, delivered_at=datetime.utcnow(),
                    updated_at=datetime.utcnow())
        )
        if result.rowcount != 1:
            return None
        await self.db.refresh(row)
        return row

    async def ack(
        self, team_run_id: str, message_id: str, *, recipient_member_id: str,
    ) -> str:
        """返回 acked / already；仅接收方可确认。"""
        row = await self.get(team_run_id, message_id)
        if row is None:
            return "missing"
        if row.recipient_member_id not in (None, recipient_member_id):
            return "forbidden"
        if row.status == MESSAGE_ACKED:
            return "already"
        result = await self.db.execute(
            update(TeamMessage)
            .where(
                TeamMessage.team_run_id == team_run_id,
                TeamMessage.message_id == message_id,
                TeamMessage.status.in_([MESSAGE_PENDING, MESSAGE_DELIVERED]),
            )
            .values(status=MESSAGE_ACKED, acked_at=datetime.utcnow(),
                    delivered_at=TeamMessage.delivered_at,
                    updated_at=datetime.utcnow())
        )
        return "acked" if result.rowcount == 1 else "conflict"

    async def mark_failed(self, team_run_id: str, message_id: str, error: str) -> bool:
        result = await self.db.execute(
            update(TeamMessage)
            .where(
                TeamMessage.team_run_id == team_run_id,
                TeamMessage.message_id == message_id,
            )
            .values(status="failed", error=error[:2000], updated_at=datetime.utcnow())
        )
        return result.rowcount == 1
