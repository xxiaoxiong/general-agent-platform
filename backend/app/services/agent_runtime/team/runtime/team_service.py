"""团队聚合服务：Team Run 行与阶段状态（只做轻量事实持久化）。"""
from __future__ import annotations

from typing import Any, Optional

from app.core.database import AsyncSessionLocal

from ..repositories import TeamRepository


class TeamService:
    def __init__(self, db_factory=AsyncSessionLocal):
        self._db_factory = db_factory

    async def ensure(
        self,
        *,
        team_run_id: str,
        objective: str,
        mode: str = "team",
        max_members: int = 4,
        max_parallel: int = 4,
        config_snapshot: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        async with self._db_factory() as db:
            row = await TeamRepository(db).ensure(
                team_run_id=team_run_id,
                objective=objective,
                mode=mode,
                max_members=max_members,
                max_parallel=max_parallel,
                config_snapshot=config_snapshot or {},
            )
            await db.commit()
            return self._serialize(row)

    async def update(
        self,
        team_run_id: str,
        *,
        phase: Optional[str] = None,
        status: Optional[str] = None,
        leader_member_id: Optional[str] = None,
        finished: bool = False,
    ) -> bool:
        async with self._db_factory() as db:
            ok = await TeamRepository(db).update_state(
                team_run_id,
                status=status,
                phase=phase,
                leader_member_id=leader_member_id,
                finished=finished,
            )
            await db.commit()
            return ok

    async def get(self, team_run_id: str) -> Optional[dict[str, Any]]:
        async with self._db_factory() as db:
            row = await TeamRepository(db).get(team_run_id)
            return self._serialize(row) if row else None

    @staticmethod
    def _serialize(row) -> dict[str, Any]:
        return {
            "team_run_id": row.team_run_id,
            "objective": row.objective,
            "mode": row.mode,
            "status": row.status,
            "phase": row.phase,
            "leader_member_id": row.leader_member_id,
            "max_members": row.max_members,
            "max_parallel": row.max_parallel,
            "config_snapshot": row.config_snapshot or {},
            "created_at": row.created_at.isoformat() if row.created_at else None,
            "finished_at": row.finished_at.isoformat() if row.finished_at else None,
        }
