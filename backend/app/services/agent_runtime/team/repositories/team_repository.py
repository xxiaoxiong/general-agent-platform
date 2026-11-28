"""团队聚合仓库：agent_team_runs 的唯一读写入口。"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.team import TeamMember, TeamRun


class TeamRepository:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def get(self, team_run_id: str) -> Optional[TeamRun]:
        return await self.db.scalar(
            select(TeamRun).where(TeamRun.team_run_id == team_run_id)
        )

    async def ensure(
        self,
        *,
        team_run_id: str,
        objective: str,
        mode: str = "team",
        max_members: int = 4,
        max_parallel: int = 4,
        config_snapshot: Optional[dict[str, Any]] = None,
    ) -> TeamRun:
        """幂等创建团队行（并发首建由唯一约束兜底）。"""
        existing = await self.get(team_run_id)
        if existing is not None:
            return existing
        row = TeamRun(
            team_run_id=team_run_id,
            objective=objective[:20_000],
            mode=mode,
            status="running",
            phase="planning",
            max_members=max_members,
            max_parallel=max_parallel,
            config_snapshot=config_snapshot or {},
        )
        try:
            async with self.db.begin_nested():
                self.db.add(row)
                await self.db.flush()
        except IntegrityError:
            existing = await self.get(team_run_id)
            if existing is None:
                raise
            return existing
        return row

    async def update_state(
        self,
        team_run_id: str,
        *,
        status: Optional[str] = None,
        phase: Optional[str] = None,
        leader_member_id: Optional[str] = None,
        finished: bool = False,
    ) -> bool:
        values: dict[str, Any] = {"updated_at": datetime.utcnow()}
        if status is not None:
            values["status"] = status
        if phase is not None:
            values["phase"] = phase
        if leader_member_id is not None:
            values["leader_member_id"] = leader_member_id
        if finished:
            values["finished_at"] = datetime.utcnow()
        result = await self.db.execute(
            update(TeamRun).where(TeamRun.team_run_id == team_run_id).values(**values)
        )
        return result.rowcount == 1

    async def list_members(self, team_run_id: str) -> list[TeamMember]:
        rows = await self.db.execute(
            select(TeamMember)
            .where(TeamMember.team_run_id == team_run_id)
            .order_by(TeamMember.id.asc())
        )
        return list(rows.scalars().all())
