"""Artifact Store：成员成果的引用式共享（元数据 + 内容 + 预览 + 校验和）。

- 消息/任务只保存 ``artifact_id``，不复制完整成果到 Prompt 或 LangGraph State；
- 读取必须属于同一 Team Run（跨团队统一按不存在处理，防 IDOR 枚举）；
- 单条内容超过 ``AGENT_TEAM_ARTIFACT_MAX_BYTES`` 时截断并明确标注；
- 记录创建者、任务、大小、类型、Checksum 与创建时间。
"""
from __future__ import annotations

import hashlib
import logging
import secrets
from typing import Any, Iterable, Optional

from app.core.database import AsyncSessionLocal

from ..errors import ArtifactError, TeamNotFound
from ..repositories import ArtifactRepository, MemberRepository

logger = logging.getLogger("ai_plat.team")


class ArtifactStore:
    def __init__(self, db_factory=AsyncSessionLocal):
        self._db_factory = db_factory

    async def create(
        self,
        team_run_id: str,
        *,
        member_id: Optional[str],
        task_id: Optional[str],
        name: str,
        content: str,
        content_type: str = "text/plain",
    ) -> dict[str, Any]:
        from app.core.config import settings

        if member_id:
            async with self._db_factory() as db:
                if await MemberRepository(db).get(team_run_id, member_id) is None:
                    raise TeamNotFound(f"成员 {member_id} 不属于该团队")
        raw = content if isinstance(content, str) else str(content)
        max_bytes = max(1024, int(settings.AGENT_TEAM_ARTIFACT_MAX_BYTES))
        encoded = raw.encode("utf-8", errors="replace")
        truncated = len(encoded) > max_bytes
        if truncated:
            # 按字节截断后回退到完整字符边界，避免产生非法 UTF-8
            encoded = encoded[:max_bytes]
            raw = encoded.decode("utf-8", errors="ignore") + "\n\n[内容超限已截断]"
        preview_chars = max(200, int(settings.AGENT_TEAM_ARTIFACT_PREVIEW_CHARS))
        artifact_id = f"art-{secrets.token_hex(10)}"
        async with self._db_factory() as db:
            row = await ArtifactRepository(db).create(
                team_run_id=team_run_id,
                artifact_id=artifact_id,
                name=name or "成员成果",
                content=raw,
                content_preview=raw[:preview_chars],
                content_type=content_type or "text/plain",
                size_bytes=len(raw.encode("utf-8", errors="replace")),
                checksum=hashlib.sha256(raw.encode("utf-8", errors="replace")).hexdigest(),
                task_id=task_id,
                member_id=member_id,
            )
            await db.commit()
            result = self._serialize(row)
            result["truncated"] = truncated
            return result

    async def get(
        self,
        team_run_id: str,
        artifact_id: str,
        *,
        snippet_chars: Optional[int] = None,
        offset: int = 0,
    ) -> dict[str, Any]:
        async with self._db_factory() as db:
            row = await ArtifactRepository(db).get(team_run_id, artifact_id)
            if row is None:
                raise TeamNotFound(f"产物 {artifact_id} 不存在")
            result = self._serialize(row, include_content=True)
        if snippet_chars is not None:
            start = max(0, int(offset))
            end = start + max(1, int(snippet_chars))
            result["content"] = (row.content or "")[start:end]
            result["snippet"] = {"offset": start, "length": end - start,
                                 "total_chars": len(row.content or "")}
        return result

    async def list(
        self, team_run_id: str, *, task_id: Optional[str] = None, limit: int = 100,
    ) -> list[dict[str, Any]]:
        async with self._db_factory() as db:
            rows = await ArtifactRepository(db).list(team_run_id, task_id=task_id, limit=limit)
            return [self._serialize(row) for row in rows]

    async def previews(
        self, team_run_id: str, artifact_ids: Iterable[str],
    ) -> dict[str, dict[str, Any]]:
        async with self._db_factory() as db:
            rows = await ArtifactRepository(db).list_by_ids(team_run_id, artifact_ids)
            return {row.artifact_id: self._serialize(row) for row in rows}

    @staticmethod
    def _serialize(row, *, include_content: bool = False) -> dict[str, Any]:
        result = {
            "artifact_id": row.artifact_id,
            "team_run_id": row.team_run_id,
            "task_id": row.task_id,
            "member_id": row.member_id,
            "name": row.name,
            "content_type": row.content_type,
            "content_preview": row.content_preview,
            "size_bytes": row.size_bytes,
            "checksum": row.checksum,
            "created_at": row.created_at.isoformat() if row.created_at else None,
        }
        if include_content:
            result["content"] = row.content
        return result
