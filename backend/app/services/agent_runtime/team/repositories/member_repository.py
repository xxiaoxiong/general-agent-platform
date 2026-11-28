"""团队成员仓库：身份、状态机（CAS）与预算计数。

所有查询按 ``team_run_id`` 作用域，禁止跨团队读写（任务书 §4.7）。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.team import TeamMember

from ..states import ALLOWED_MEMBER_TRANSITIONS


class MemberRepository:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def get(self, team_run_id: str, member_id: str) -> Optional[TeamMember]:
        return await self.db.scalar(
            select(TeamMember).where(
                TeamMember.team_run_id == team_run_id,
                TeamMember.member_id == member_id,
            )
        )

    async def get_by_task(self, team_run_id: str, task_id: str) -> Optional[TeamMember]:
        return await self.db.scalar(
            select(TeamMember)
            .where(TeamMember.team_run_id == team_run_id, TeamMember.task_id == task_id)
            .order_by(TeamMember.id.desc())
            .limit(1)
        )

    async def list(self, team_run_id: str) -> list[TeamMember]:
        rows = await self.db.execute(
            select(TeamMember)
            .where(TeamMember.team_run_id == team_run_id)
            .order_by(TeamMember.id.asc())
        )
        return list(rows.scalars().all())

    async def create(
        self,
        *,
        member_id: str,
        team_run_id: str,
        role: str,
        name: str,
        thread_id: str,
        profile_snapshot: dict[str, Any],
        model_snapshot: dict[str, Any],
        budget: dict[str, Any],
        parent_member_id: Optional[str] = None,
        task_id: Optional[str] = None,
        status: str = "created",
    ) -> TeamMember:
        row = TeamMember(
            member_id=member_id,
            team_run_id=team_run_id,
            parent_member_id=parent_member_id,
            task_id=task_id,
            role=role[:80],
            name=name[:80],
            thread_id=thread_id,
            profile_snapshot=profile_snapshot,
            model_snapshot=model_snapshot,
            budget=budget,
            status=status,
        )
        try:
            async with self.db.begin_nested():
                self.db.add(row)
                await self.db.flush()
        except IntegrityError:
            existing = await self.get(team_run_id, member_id)
            if existing is None:
                raise
            return existing
        return row

    async def transition(
        self,
        team_run_id: str,
        member_id: str,
        *,
        target: str,
        sources: Optional[frozenset[str]] = None,
        **fields: Any,
    ) -> bool:
        """原子状态迁移：WHERE 当前状态 ∈ sources（默认取状态机合法来源）。"""
        if sources is None:
            sources = frozenset(
                source for source, allowed in ALLOWED_MEMBER_TRANSITIONS.items()
                if target in allowed
            )
        values = {"status": target, "updated_at": datetime.utcnow(), **fields}
        result = await self.db.execute(
            update(TeamMember)
            .where(
                TeamMember.team_run_id == team_run_id,
                TeamMember.member_id == member_id,
                TeamMember.status.in_(sorted(sources)),
            )
            .values(**values)
        )
        return result.rowcount == 1

    async def update_fields(self, team_run_id: str, member_id: str, **fields: Any) -> bool:
        if not fields:
            return True
        result = await self.db.execute(
            update(TeamMember)
            .where(
                TeamMember.team_run_id == team_run_id,
                TeamMember.member_id == member_id,
            )
            .values(**fields, updated_at=datetime.utcnow())
        )
        return result.rowcount == 1

    async def add_usage(
        self,
        team_run_id: str,
        member_id: str,
        *,
        model_calls: int = 0,
        tool_calls: int = 0,
        tokens: int = 0,
    ) -> None:
        if not (model_calls or tool_calls or tokens):
            return
        await self.db.execute(
            update(TeamMember)
            .where(
                TeamMember.team_run_id == team_run_id,
                TeamMember.member_id == member_id,
            )
            .values(
                model_calls=TeamMember.model_calls + model_calls,
                tool_calls=TeamMember.tool_calls + tool_calls,
                tokens=TeamMember.tokens + tokens,
                updated_at=datetime.utcnow(),
            )
        )

    async def increment_recovery(self, team_run_id: str, member_id: str) -> None:
        await self.db.execute(
            update(TeamMember)
            .where(
                TeamMember.team_run_id == team_run_id,
                TeamMember.member_id == member_id,
            )
            .values(
                recovery_count=TeamMember.recovery_count + 1,
                updated_at=datetime.utcnow(),
            )
        )
