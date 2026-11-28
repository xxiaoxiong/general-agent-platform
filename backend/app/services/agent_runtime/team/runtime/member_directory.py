"""团队成员目录（供 Team Tools 解析接收者、查看成员状态）。"""
from __future__ import annotations

from typing import Any

from app.core.database import AsyncSessionLocal

from ..repositories import MemberRepository


class MemberDirectory:
    def __init__(self, db_factory=AsyncSessionLocal):
        self._db_factory = db_factory

    async def list_members(self, team_run_id: str) -> list[dict[str, Any]]:
        async with self._db_factory() as db:
            rows = await MemberRepository(db).list(team_run_id)
            return [
                {
                    "member_id": row.member_id,
                    "role": row.role,
                    "name": row.name,
                    "status": row.status,
                    "task_id": row.task_id,
                    "thread_id": row.thread_id,
                    "model_calls": row.model_calls,
                    "tool_calls": row.tool_calls,
                    "tokens": row.tokens,
                    "recovery_count": row.recovery_count,
                    "last_error": (row.last_error or "")[:400] or None,
                    "model": (row.model_snapshot or {}).get("name"),
                    "started_at": row.started_at.isoformat() if row.started_at else None,
                    "completed_at": row.completed_at.isoformat() if row.completed_at else None,
                }
                for row in rows
            ]

    async def exists(self, team_run_id: str, member_id: str) -> bool:
        async with self._db_factory() as db:
            return await MemberRepository(db).get(team_run_id, member_id) is not None
