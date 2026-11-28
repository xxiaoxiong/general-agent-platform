"""Artifact 仓库：元数据、内容、预览与校验和。"""
from __future__ import annotations

from typing import Iterable, Optional

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.team import TeamArtifact


class ArtifactRepository:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def get(self, team_run_id: str, artifact_id: str) -> Optional[TeamArtifact]:
        return await self.db.scalar(
            select(TeamArtifact).where(
                TeamArtifact.team_run_id == team_run_id,
                TeamArtifact.artifact_id == artifact_id,
            )
        )

    async def list_by_ids(
        self, team_run_id: str, artifact_ids: Iterable[str],
    ) -> list[TeamArtifact]:
        ids = list(dict.fromkeys(artifact_ids))
        if not ids:
            return []
        rows = await self.db.execute(
            select(TeamArtifact).where(
                TeamArtifact.team_run_id == team_run_id,
                TeamArtifact.artifact_id.in_(ids),
            )
        )
        return list(rows.scalars().all())

    async def list(
        self, team_run_id: str, *, task_id: Optional[str] = None, limit: int = 100,
    ) -> list[TeamArtifact]:
        conditions = [TeamArtifact.team_run_id == team_run_id]
        if task_id:
            conditions.append(TeamArtifact.task_id == task_id)
        rows = await self.db.execute(
            select(TeamArtifact)
            .where(*conditions)
            .order_by(TeamArtifact.id.desc())
            .limit(max(1, limit))
        )
        return list(rows.scalars().all())

    async def create(
        self,
        *,
        team_run_id: str,
        artifact_id: str,
        name: str,
        content: str,
        content_preview: str,
        content_type: str,
        size_bytes: int,
        checksum: str,
        task_id: Optional[str] = None,
        member_id: Optional[str] = None,
    ) -> TeamArtifact:
        row = TeamArtifact(
            team_run_id=team_run_id,
            artifact_id=artifact_id,
            task_id=task_id,
            member_id=member_id,
            name=name[:200],
            content_type=content_type[:80],
            content=content,
            content_preview=content_preview,
            size_bytes=size_bytes,
            checksum=checksum,
        )
        try:
            async with self.db.begin_nested():
                self.db.add(row)
                await self.db.flush()
        except IntegrityError:
            existing = await self.get(team_run_id, artifact_id)
            if existing is None:
                raise
            return existing
        return row
