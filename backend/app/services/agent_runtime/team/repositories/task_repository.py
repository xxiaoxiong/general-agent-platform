"""Task Board 仓库：CAS 更新、认领/租约、完成/失败/重试与依赖。

并发正确性（任务书 §4.2 / §4.3）：
- 所有更新使用数据库原子条件 UPDATE，WHERE 同时校验 ``revision``（CAS）、
  ``owner_member_id`` 与 ``lease_token``（租约）；
- 认领只允许 ``pending/ready`` 且无有效租约的任务，两个成员并发认领只有一个
  ``rowcount == 1``；
- 完成/失败必须携带 owner + lease token，旧租约成员无法覆盖新成员结果；
- 重复完成（同一 artifact）幂等返回 already，不重复产生副作用。
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Iterable, Optional

from sqlalchemy import delete, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.team import TeamTask, TeamTaskDependency

from ..errors import TaskConflict, TaskStateError, TeamNotFound
from ..states import TASK_ACTIVE, TASK_CLAIMED, TASK_COMPLETED, TASK_READY, TASK_RUNNING


class TaskRepository:
    def __init__(self, db: AsyncSession):
        self.db = db

    # ── 查询 ─────────────────────────────────────────────────────────────

    async def get(self, team_run_id: str, task_id: str) -> Optional[TeamTask]:
        return await self.db.scalar(
            select(TeamTask).where(
                TeamTask.team_run_id == team_run_id,
                TeamTask.task_id == task_id,
            )
        )

    async def list_tasks(self, team_run_id: str) -> list[TeamTask]:
        rows = await self.db.execute(
            select(TeamTask)
            .where(TeamTask.team_run_id == team_run_id)
            .order_by(TeamTask.id.asc())
        )
        return list(rows.scalars().all())

    async def list_dependencies(self, team_run_id: str) -> list[TeamTaskDependency]:
        rows = await self.db.execute(
            select(TeamTaskDependency)
            .where(TeamTaskDependency.team_run_id == team_run_id)
            .order_by(TeamTaskDependency.id.asc())
        )
        return list(rows.scalars().all())

    # ── 创建 / 重置 ──────────────────────────────────────────────────────

    async def create(
        self,
        *,
        team_run_id: str,
        task_id: str,
        subject: str,
        description: str,
        role: str,
        parent_task_id: Optional[str] = None,
        priority: int = 0,
        write_scopes: Optional[list[str]] = None,
        max_retries: int = 2,
        dependencies: Iterable[str] = (),
    ) -> TeamTask:
        row = TeamTask(
            team_run_id=team_run_id,
            task_id=task_id,
            parent_task_id=parent_task_id,
            subject=subject[:200],
            description=description[:20_000],
            role=role[:80],
            priority=priority,
            write_scopes=list(write_scopes or []),
            max_retries=max_retries,
            status="pending",
            revision=1,
        )
        try:
            async with self.db.begin_nested():
                self.db.add(row)
                await self.db.flush()
                for dependency in dict.fromkeys(str(dep) for dep in dependencies):
                    self.db.add(TeamTaskDependency(
                        team_run_id=team_run_id,
                        task_id=task_id,
                        depends_on_task_id=dependency[:40],
                    ))
                await self.db.flush()
        except IntegrityError:
            existing = await self.get(team_run_id, task_id)
            if existing is None:
                raise
            return existing
        return row

    async def clear(self, team_run_id: str) -> None:
        """清理团队任务（仅计划编排前使用；运行中禁止调用）。"""
        await self.db.execute(
            delete(TeamTaskDependency).where(TeamTaskDependency.team_run_id == team_run_id)
        )
        await self.db.execute(delete(TeamTask).where(TeamTask.team_run_id == team_run_id))

    # ── 状态迁移（CAS）──────────────────────────────────────────────────

    async def update_cas(
        self,
        team_run_id: str,
        task_id: str,
        *,
        expected_revision: int,
        target_status: Optional[str] = None,
        allowed_sources: Optional[Iterable[str]] = None,
        **values: Any,
    ) -> bool:
        payload = {**values, "revision": TeamTask.revision + 1, "updated_at": datetime.utcnow()}
        if target_status is not None:
            payload["status"] = target_status
        conditions = [
            TeamTask.team_run_id == team_run_id,
            TeamTask.task_id == task_id,
            TeamTask.revision == expected_revision,
        ]
        if allowed_sources is not None:
            conditions.append(TeamTask.status.in_(list(allowed_sources)))
        result = await self.db.execute(update(TeamTask).where(*conditions).values(**payload))
        return result.rowcount == 1

    async def mark_ready(self, team_run_id: str, task_ids: Iterable[str]) -> int:
        ids = list(dict.fromkeys(task_ids))
        if not ids:
            return 0
        result = await self.db.execute(
            update(TeamTask)
            .where(
                TeamTask.team_run_id == team_run_id,
                TeamTask.task_id.in_(ids),
                TeamTask.status == "pending",
            )
            .values(status=TASK_READY, revision=TeamTask.revision + 1,
                    updated_at=datetime.utcnow())
        )
        return result.rowcount or 0

    async def claim(
        self,
        team_run_id: str,
        task_id: str,
        *,
        member_id: str,
        lease_token: str,
        expected_revision: int,
        lease_seconds: int,
    ) -> bool:
        now = datetime.utcnow()
        result = await self.db.execute(
            update(TeamTask)
            .where(
                TeamTask.team_run_id == team_run_id,
                TeamTask.task_id == task_id,
                TeamTask.revision == expected_revision,
                TeamTask.status.in_(["pending", TASK_READY]),
                or_(TeamTask.owner_member_id.is_(None), TeamTask.owner_member_id == member_id),
                or_(TeamTask.lease_expires_at.is_(None), TeamTask.lease_expires_at < now),
            )
            .values(
                owner_member_id=member_id,
                status=TASK_CLAIMED,
                lease_token=lease_token,
                lease_expires_at=now + timedelta(seconds=max(1, lease_seconds)),
                revision=TeamTask.revision + 1,
                error=None,
                blocked_reason=None,
                updated_at=now,
            )
        )
        return result.rowcount == 1

    async def mark_running(
        self, team_run_id: str, task_id: str, *, member_id: str, lease_token: str,
    ) -> bool:
        result = await self.db.execute(
            update(TeamTask)
            .where(
                TeamTask.team_run_id == team_run_id,
                TeamTask.task_id == task_id,
                TeamTask.owner_member_id == member_id,
                TeamTask.lease_token == lease_token,
                TeamTask.status == TASK_CLAIMED,
            )
            .values(status=TASK_RUNNING, revision=TeamTask.revision + 1,
                    updated_at=datetime.utcnow())
        )
        return result.rowcount == 1

    async def renew_lease(
        self,
        team_run_id: str,
        task_id: str,
        *,
        member_id: str,
        lease_token: str,
        lease_seconds: int,
    ) -> bool:
        result = await self.db.execute(
            update(TeamTask)
            .where(
                TeamTask.team_run_id == team_run_id,
                TeamTask.task_id == task_id,
                TeamTask.owner_member_id == member_id,
                TeamTask.lease_token == lease_token,
                TeamTask.status.in_([TASK_CLAIMED, TASK_RUNNING]),
            )
            .values(
                lease_expires_at=datetime.utcnow() + timedelta(seconds=max(1, lease_seconds)),
                updated_at=datetime.utcnow(),
            )
        )
        return result.rowcount == 1

    async def complete(
        self,
        team_run_id: str,
        task_id: str,
        *,
        member_id: str,
        lease_token: str,
        artifact_id: Optional[str],
    ) -> str:
        """完成契约：返回 completed / already；冲突抛 TaskConflict。"""
        current = await self.get(team_run_id, task_id)
        if current is None:
            raise TeamNotFound(f"任务 {task_id} 不存在")
        if current.status == TASK_COMPLETED:
            if artifact_id and current.result_artifact_id not in (None, artifact_id):
                raise TaskConflict("任务已完成且结果产物不一致（拒绝覆盖）")
            return "already"
        result = await self.db.execute(
            update(TeamTask)
            .where(
                TeamTask.team_run_id == team_run_id,
                TeamTask.task_id == task_id,
                TeamTask.owner_member_id == member_id,
                TeamTask.lease_token == lease_token,
                TeamTask.status.in_([TASK_CLAIMED, TASK_RUNNING]),
            )
            .values(
                status=TASK_COMPLETED,
                result_artifact_id=artifact_id,
                completed_at=datetime.utcnow(),
                lease_token=None,
                lease_expires_at=None,
                error=None,
                revision=TeamTask.revision + 1,
                updated_at=datetime.utcnow(),
            )
        )
        if result.rowcount == 1:
            return "completed"
        fresh = await self.get(team_run_id, task_id)
        if fresh is not None and fresh.status == TASK_COMPLETED:
            if artifact_id and fresh.result_artifact_id not in (None, artifact_id):
                raise TaskConflict("任务已完成且结果产物不一致（拒绝覆盖）")
            return "already"
        raise TaskConflict(
            f"任务 {task_id} 完成被拒（租约/owner/revision 已失效，当前状态 {getattr(fresh, 'status', 'missing')}）"
        )

    async def fail(
        self,
        team_run_id: str,
        task_id: str,
        *,
        member_id: str,
        lease_token: str,
        error: str,
    ) -> bool:
        result = await self.db.execute(
            update(TeamTask)
            .where(
                TeamTask.team_run_id == team_run_id,
                TeamTask.task_id == task_id,
                TeamTask.owner_member_id == member_id,
                TeamTask.lease_token == lease_token,
                TeamTask.status.in_([TASK_CLAIMED, TASK_RUNNING]),
            )
            .values(
                status="failed",
                error=error[:4000],
                lease_token=None,
                lease_expires_at=None,
                revision=TeamTask.revision + 1,
                updated_at=datetime.utcnow(),
            )
        )
        return result.rowcount == 1

    async def release(
        self,
        team_run_id: str,
        task_id: str,
        *,
        member_id: str,
        lease_token: str,
        reason: str = "",
    ) -> bool:
        result = await self.db.execute(
            update(TeamTask)
            .where(
                TeamTask.team_run_id == team_run_id,
                TeamTask.task_id == task_id,
                TeamTask.owner_member_id == member_id,
                TeamTask.lease_token == lease_token,
                TeamTask.status.in_([TASK_CLAIMED, TASK_RUNNING]),
            )
            .values(
                status=TASK_READY,
                owner_member_id=None,
                lease_token=None,
                lease_expires_at=None,
                error=(reason[:4000] or None),
                revision=TeamTask.revision + 1,
                updated_at=datetime.utcnow(),
            )
        )
        return result.rowcount == 1

    async def reclaim_expired(self, team_run_id: str) -> int:
        """回收过期租约：claimed/running 且 lease 到期 -> ready。"""
        result = await self.db.execute(
            update(TeamTask)
            .where(
                TeamTask.team_run_id == team_run_id,
                TeamTask.status.in_([TASK_CLAIMED, TASK_RUNNING]),
                TeamTask.lease_expires_at.is_not(None),
                TeamTask.lease_expires_at < datetime.utcnow(),
            )
            .values(
                status=TASK_READY,
                owner_member_id=None,
                lease_token=None,
                lease_expires_at=None,
                error="任务租约过期，已重新调度",
                revision=TeamTask.revision + 1,
                updated_at=datetime.utcnow(),
            )
        )
        return result.rowcount or 0

    async def retry(self, team_run_id: str, task_id: str) -> bool:
        """显式重试：failed/skipped/cancelled -> ready（retry_count+1，超上限拒绝）。"""
        task = await self.get(team_run_id, task_id)
        if task is None:
            raise TeamNotFound(f"任务 {task_id} 不存在")
        if task.status not in {"failed", "skipped", "cancelled"}:
            raise TaskStateError(f"任务 {task_id} 当前状态 {task.status} 不允许重试")
        if int(task.retry_count or 0) >= int(task.max_retries or 0):
            raise TaskStateError(f"任务 {task_id} 重试次数已达上限")
        result = await self.db.execute(
            update(TeamTask)
            .where(
                TeamTask.team_run_id == team_run_id,
                TeamTask.task_id == task_id,
                TeamTask.revision == task.revision,
                TeamTask.status == task.status,
            )
            .values(
                status=TASK_READY,
                retry_count=TeamTask.retry_count + 1,
                owner_member_id=None,
                lease_token=None,
                lease_expires_at=None,
                error=None,
                revision=TeamTask.revision + 1,
                updated_at=datetime.utcnow(),
            )
        )
        return result.rowcount == 1

    async def mark_skipped(self, team_run_id: str, task_id: str, reason: str) -> bool:
        result = await self.db.execute(
            update(TeamTask)
            .where(
                TeamTask.team_run_id == team_run_id,
                TeamTask.task_id == task_id,
                TeamTask.status.in_(sorted(TASK_ACTIVE)),
            )
            .values(
                status="skipped",
                error=reason[:4000],
                revision=TeamTask.revision + 1,
                updated_at=datetime.utcnow(),
            )
        )
        return result.rowcount == 1

    async def set_blocked(
        self, team_run_id: str, task_id: str, *, reason: str, expected_revision: int,
    ) -> bool:
        return await self.update_cas(
            team_run_id,
            task_id,
            expected_revision=expected_revision,
            target_status="blocked",
            allowed_sources=[TASK_READY, TASK_CLAIMED, TASK_RUNNING],
            blocked_reason=reason[:4000],
        )

    async def cancel(self, team_run_id: str, task_id: str, *, reason: str = "") -> bool:
        result = await self.db.execute(
            update(TeamTask)
            .where(
                TeamTask.team_run_id == team_run_id,
                TeamTask.task_id == task_id,
                TeamTask.status.in_(["pending", TASK_READY, TASK_CLAIMED, TASK_RUNNING, "blocked"]),
            )
            .values(
                status="cancelled",
                error=(reason[:4000] or None),
                lease_token=None,
                lease_expires_at=None,
                revision=TeamTask.revision + 1,
                updated_at=datetime.utcnow(),
            )
        )
        return result.rowcount == 1
