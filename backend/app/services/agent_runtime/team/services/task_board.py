"""Task Board 业务服务：DAG 校验、CAS 更新、认领/租约、完成/失败/重试。

所有事实落库；LangGraph State 只保存控制流需要的引用与摘要。
"""
from __future__ import annotations

import logging
from datetime import datetime
from graphlib import CycleError, TopologicalSorter
from typing import Any, Iterable, Optional

from app.core.database import AsyncSessionLocal

from ..errors import PlanValidationError, TaskConflict, TaskStateError, TeamNotFound
from ..repositories import ArtifactRepository, MemberRepository, TaskRepository
from ..states import TASK_ACTIVE, TASK_COMPLETED, TASK_PENDING, TASK_READY
from ..states import can_transition_task

logger = logging.getLogger("ai_plat.team")

_VIEW_STATUS = {
    "pending": "pending",
    "ready": "pending",
    "claimed": "running",
    "running": "running",
    "blocked": "pending",
    "completed": "completed",
    "failed": "failed",
    "cancelled": "cancelled",
    "skipped": "skipped",
}


def _now() -> datetime:
    return datetime.utcnow()


class TaskBoardService:
    def __init__(self, db_factory=AsyncSessionLocal):
        self._db_factory = db_factory

    # ── 查询 / 创建 ──────────────────────────────────────────────────────

    async def list_tasks(self, team_run_id: str) -> list[dict[str, Any]]:
        async with self._db_factory() as db:
            tasks = await TaskRepository(db).list_tasks(team_run_id)
            deps = await TaskRepository(db).list_dependencies(team_run_id)
            artifacts = await ArtifactRepository(db).list_by_ids(
                team_run_id,
                [task.result_artifact_id for task in tasks if task.result_artifact_id],
            )
            by_id = {item.artifact_id: item for item in artifacts}
            return [self._serialize(task, deps, by_id) for task in tasks]

    async def get_task(self, team_run_id: str, task_id: str) -> dict[str, Any]:
        async with self._db_factory() as db:
            repo = TaskRepository(db)
            task = await repo.get(team_run_id, task_id)
            if task is None:
                raise TeamNotFound(f"任务 {task_id} 不存在")
            deps = [dep for dep in await repo.list_dependencies(team_run_id)
                    if dep.task_id == task_id]
            artifact = None
            if task.result_artifact_id:
                artifact = await ArtifactRepository(db).get(team_run_id, task.result_artifact_id)
            return self._serialize(task, deps, {artifact.artifact_id: artifact} if artifact else {})

    async def create_task(
        self,
        team_run_id: str,
        *,
        task_id: str,
        subject: str,
        description: str,
        role: str,
        dependencies: Iterable[str] = (),
        priority: int = 0,
        write_scopes: Optional[list[str]] = None,
        max_retries: Optional[int] = None,
    ) -> dict[str, Any]:
        from app.core.config import settings

        async with self._db_factory() as db:
            task = await TaskRepository(db).create(
                team_run_id=team_run_id,
                task_id=task_id,
                subject=subject,
                description=description,
                role=role,
                priority=int(priority or 0),
                write_scopes=write_scopes,
                max_retries=int(
                    max_retries if max_retries is not None
                    else settings.AGENT_TEAM_MAX_TASK_RETRIES
                ),
                dependencies=dependencies,
            )
            await db.commit()
            return await self.get_task(team_run_id, task.task_id)

    async def reset_plan(self, team_run_id: str) -> None:
        """计划阶段重置任务板；任何任务已进入执行/完成则拒绝。"""
        async with self._db_factory() as db:
            repo = TaskRepository(db)
            tasks = await repo.list_tasks(team_run_id)
            if any(task.status not in {TASK_PENDING, "cancelled", "failed"} for task in tasks):
                raise TaskStateError("任务已开始执行，禁止重置团队计划")
            await repo.clear(team_run_id)
            await db.commit()

    async def validate_plan(
        self,
        team_run_id: str,
        *,
        min_tasks: int = 2,
        max_tasks: int = 4,
        require_unique_roles: bool = True,
    ) -> list[dict[str, Any]]:
        tasks = await self.list_tasks(team_run_id)
        if not min_tasks <= len(tasks) <= max_tasks:
            raise PlanValidationError(
                f"团队计划须包含 {min_tasks} 至 {max_tasks} 项任务（当前 {len(tasks)} 项）"
            )
        ids = [task["id"] for task in tasks]
        if len(set(ids)) != len(ids):
            raise PlanValidationError("任务 ID 必须唯一")
        if require_unique_roles:
            roles = [str(task["role"]).casefold() for task in tasks]
            if len(set(roles)) != len(roles):
                raise PlanValidationError("成员角色必须唯一")
        known = set(ids)
        dependencies: dict[str, list[str]] = {}
        for task in tasks:
            deps = list(task["depends_on"])
            if len(set(deps)) != len(deps):
                raise PlanValidationError(f"任务 {task['id']} 的依赖不能重复")
            if task["id"] in deps:
                raise PlanValidationError(f"任务 {task['id']} 不能依赖自身")
            missing = [dep for dep in deps if dep not in known]
            if missing:
                raise PlanValidationError(
                    f"任务 {task['id']} 依赖未登记的任务: {', '.join(missing)}"
                )
            dependencies[task["id"]] = deps
        try:
            tuple(TopologicalSorter(dependencies).static_order())
        except CycleError as exc:
            raise PlanValidationError("任务依赖不能形成循环") from exc
        return tasks

    async def ensure_tasks_from_plan(
        self, team_run_id: str, tasks: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """把结构化计划写入任务板（仅当任务板为空时；用于 Leader 未使用工具时的兜底）。"""
        existing = await self.list_tasks(team_run_id)
        if existing:
            return existing
        for item in tasks:
            await self.create_task(
                team_run_id,
                task_id=str(item["id"]),
                subject=str(item["title"]),
                description=str(item["brief"]),
                role=str(item["role"]),
                dependencies=item.get("depends_on") or (),
                priority=int(item.get("priority") or 0),
            )
        return await self.list_tasks(team_run_id)

    # ── 调度辅助 ─────────────────────────────────────────────────────────

    async def mark_ready(self, team_run_id: str, task_ids: Iterable[str]) -> int:
        async with self._db_factory() as db:
            count = await TaskRepository(db).mark_ready(team_run_id, task_ids)
            await db.commit()
            return count

    async def ready_tasks(self, team_run_id: str) -> list[dict[str, Any]]:
        tasks = await self.list_tasks(team_run_id)
        by_id = {task["id"]: task for task in tasks}
        ready = []
        for task in tasks:
            if task["board_status"] not in {TASK_PENDING, TASK_READY}:
                continue
            if all(by_id[dep]["board_status"] == TASK_COMPLETED for dep in task["depends_on"]):
                ready.append(task)
        return ready

    async def skip_blocked_dependents(self, team_run_id: str) -> list[str]:
        """依赖失败/取消/跳过的活动任务标记为 skipped（拓扑顺序直到稳定）。"""
        tasks = await self.list_tasks(team_run_id)
        by_id = {task["id"]: task for task in tasks}
        dependencies = {task["id"]: task["depends_on"] for task in tasks}
        try:
            order = list(TopologicalSorter(dependencies).static_order())
        except CycleError:
            order = list(by_id)
        skipped: list[str] = []
        async with self._db_factory() as db:
            repo = TaskRepository(db)
            for task_id in order:
                task = by_id[task_id]
                if task["board_status"] not in TASK_ACTIVE:
                    continue
                if any(
                    by_id.get(dep, {}).get("board_status") in {"failed", "cancelled", "skipped"}
                    for dep in task["depends_on"]
                ):
                    if await repo.mark_skipped(
                        team_run_id, task_id, "所依赖的任务没有交付结果",
                    ):
                        skipped.append(task_id)
                        task["board_status"] = "skipped"
            await db.commit()
        return skipped

    async def reclaim_expired(self, team_run_id: str) -> int:
        async with self._db_factory() as db:
            count = await TaskRepository(db).reclaim_expired(team_run_id)
            await db.commit()
            return count

    # ── 认领 / 租约 ──────────────────────────────────────────────────────

    async def claim(
        self,
        team_run_id: str,
        task_id: str,
        *,
        member_id: str,
        lease_seconds: Optional[int] = None,
    ) -> dict[str, Any]:
        from app.core.config import settings

        lease_seconds = int(
            lease_seconds if lease_seconds is not None else settings.AGENT_TEAM_TASK_LEASE_SECONDS
        )
        import secrets

        async with self._db_factory() as db:
            repo = TaskRepository(db)
            task = await repo.get(team_run_id, task_id)
            if task is None:
                raise TeamNotFound(f"任务 {task_id} 不存在")
            if task.status == TASK_COMPLETED:
                return await self.get_task(team_run_id, task_id)
            if (
                task.owner_member_id == member_id
                and task.status in {"claimed", "running"}
                and task.lease_expires_at
                and task.lease_expires_at > _now()
            ):
                # 幂等：同一成员重复认领自己的有效租约
                return await self.get_task(team_run_id, task_id)
            deps = [dep for dep in await repo.list_dependencies(team_run_id)
                    if dep.task_id == task_id]
            dep_ids = [dep.depends_on_task_id for dep in deps]
            if dep_ids:
                dep_tasks = {item.task_id: item for item in await repo.list_tasks(team_run_id)}
                incomplete = [
                    dep for dep in dep_ids
                    if dep_tasks.get(dep) is None or dep_tasks[dep].status != TASK_COMPLETED
                ]
                if incomplete:
                    raise TaskConflict(f"任务 {task_id} 的前置任务未完成: {', '.join(incomplete)}")
            token = secrets.token_hex(16)
            claimed = await repo.claim(
                team_run_id, task_id,
                member_id=member_id,
                lease_token=token,
                expected_revision=int(task.revision),
                lease_seconds=lease_seconds,
            )
            if not claimed:
                await db.rollback()
                fresh = await repo.get(team_run_id, task_id)
                raise TaskConflict(
                    f"任务 {task_id} 认领失败（已被 {getattr(fresh, 'owner_member_id', None) or '其他成员'} 认领或版本冲突）"
                )
            await db.commit()
            return await self.get_task(team_run_id, task_id)

    async def mark_running(
        self, team_run_id: str, task_id: str, *, member_id: str, lease_token: str,
    ) -> None:
        async with self._db_factory() as db:
            ok = await TaskRepository(db).mark_running(
                team_run_id, task_id, member_id=member_id, lease_token=lease_token,
            )
            await db.commit()
        if not ok:
            raise TaskConflict(f"任务 {task_id} 无法进入 running（租约失效或状态已变化）")

    async def renew_lease(
        self,
        team_run_id: str,
        task_id: str,
        *,
        member_id: str,
        lease_token: str,
        lease_seconds: Optional[int] = None,
    ) -> bool:
        from app.core.config import settings

        async with self._db_factory() as db:
            ok = await TaskRepository(db).renew_lease(
                team_run_id, task_id,
                member_id=member_id,
                lease_token=lease_token,
                lease_seconds=int(
                    lease_seconds if lease_seconds is not None
                    else settings.AGENT_TEAM_TASK_LEASE_SECONDS
                ),
            )
            await db.commit()
            return ok

    async def complete(
        self,
        team_run_id: str,
        task_id: str,
        *,
        member_id: str,
        lease_token: str,
        artifact_id: Optional[str],
    ) -> str:
        async with self._db_factory() as db:
            outcome = await TaskRepository(db).complete(
                team_run_id, task_id,
                member_id=member_id,
                lease_token=lease_token,
                artifact_id=artifact_id,
            )
            await db.commit()
            return outcome

    async def fail(
        self,
        team_run_id: str,
        task_id: str,
        *,
        member_id: str,
        lease_token: str,
        error: str,
    ) -> bool:
        async with self._db_factory() as db:
            ok = await TaskRepository(db).fail(
                team_run_id, task_id,
                member_id=member_id, lease_token=lease_token, error=error,
            )
            await db.commit()
            return ok

    async def release(
        self,
        team_run_id: str,
        task_id: str,
        *,
        member_id: str,
        lease_token: str,
        reason: str = "",
    ) -> bool:
        async with self._db_factory() as db:
            ok = await TaskRepository(db).release(
                team_run_id, task_id,
                member_id=member_id, lease_token=lease_token, reason=reason,
            )
            await db.commit()
            return ok

    async def retry(self, team_run_id: str, task_id: str) -> bool:
        async with self._db_factory() as db:
            ok = await TaskRepository(db).retry(team_run_id, task_id)
            await db.commit()
            return ok

    async def cancel_task(
        self, team_run_id: str, task_id: str, *, reason: str = "",
    ) -> bool:
        async with self._db_factory() as db:
            ok = await TaskRepository(db).cancel(team_run_id, task_id, reason=reason)
            await db.commit()
            return ok

    async def update_task(
        self,
        team_run_id: str,
        task_id: str,
        *,
        expected_revision: int,
        actor: dict[str, Any],
        changes: dict[str, Any],
    ) -> dict[str, Any]:
        """受控更新：字段白名单由 Team Tools 校验，这里只做状态机 + CAS。"""
        async with self._db_factory() as db:
            repo = TaskRepository(db)
            task = await repo.get(team_run_id, task_id)
            if task is None:
                raise TeamNotFound(f"任务 {task_id} 不存在")
            if int(expected_revision) != int(task.revision):
                raise TaskConflict(
                    f"任务 {task_id} 版本冲突（期望 revision={expected_revision}，当前 {task.revision}）"
                )
            is_leader = bool(actor.get("is_leader"))
            member_id = str(actor.get("member_id") or "")
            if not is_leader and task.owner_member_id not in (None, member_id):
                raise TaskConflict(f"任务 {task_id} 由其他成员认领，无权更新")
            target_status = changes.get("status")
            if target_status and target_status != task.status:
                if not can_transition_task(task.status, target_status):
                    raise TaskStateError(
                        f"任务 {task_id} 不允许从 {task.status} 迁移到 {target_status}"
                    )
                if not is_leader and not (
                    target_status == "blocked" and member_id == task.owner_member_id
                ):
                    raise TaskStateError("成员仅可将本人任务标记为 blocked")
            if not is_leader and set(changes) - {"status", "blocked_reason"}:
                raise TaskStateError("成员仅允许更新本人任务的 blocked_reason/status")
            allowed_fields = {
                "subject", "description", "priority", "status", "blocked_reason", "write_scopes",
            }
            values = {key: value for key, value in changes.items() if key in allowed_fields}
            if "subject" in values:
                values["subject"] = str(values["subject"])[:200]
            if "description" in values:
                values["description"] = str(values["description"])[:20_000]
            if "blocked_reason" in values and values["blocked_reason"] is not None:
                values["blocked_reason"] = str(values["blocked_reason"])[:4000]
            ok = await repo.update_cas(
                team_run_id, task_id,
                expected_revision=int(expected_revision),
                **values,
            )
            if not ok:
                await db.rollback()
                raise TaskConflict(f"任务 {task_id} 更新失败（版本已变化）")
            await db.commit()
            return await self.get_task(team_run_id, task_id)

    async def block_task(
        self, team_run_id: str, task_id: str, *, reason: str, expected_revision: int,
    ) -> bool:
        async with self._db_factory() as db:
            ok = await TaskRepository(db).set_blocked(
                team_run_id, task_id, reason=reason, expected_revision=expected_revision,
            )
            await db.commit()
            return ok

    async def assign(
        self,
        team_run_id: str,
        task_id: str,
        *,
        member: str,
        expected_revision: int,
    ) -> dict[str, Any]:
        """负责人分配任务：按 member_id 或角色解析唯一成员，写入 owner。"""
        async with self._db_factory() as db:
            members = await MemberRepository(db).list(team_run_id)
            matches = [item for item in members if item.member_id == member]
            if not matches:
                matches = [
                    item for item in members
                    if item.role.casefold() == member.casefold()
                    or item.name.casefold() == member.casefold()
                ]
            if len(matches) != 1:
                raise TeamNotFound(f"无法解析成员 '{member}'（不存在或角色不唯一）")
            member_id = matches[0].member_id
            repo = TaskRepository(db)
            task = await repo.get(team_run_id, task_id)
            if task is None:
                raise TeamNotFound(f"任务 {task_id} 不存在")
            if task.status not in {TASK_PENDING, TASK_READY, "blocked"}:
                raise TaskStateError(f"任务 {task_id} 当前状态 {task.status} 不允许分配")
            ok = await repo.update_cas(
                team_run_id, task_id,
                expected_revision=int(expected_revision),
                target_status=TASK_READY if task.status == TASK_PENDING else None,
                allowed_sources=[TASK_PENDING, TASK_READY, "blocked"],
                owner_member_id=member_id,
            )
            if not ok:
                await db.rollback()
                raise TaskConflict(f"任务 {task_id} 分配失败（版本已变化）")
            await db.commit()
            return await self.get_task(team_run_id, task_id)

    # ── 快照（team.updated 兼容前端契约 + 成员身份）──────────────────────

    async def snapshot(
        self,
        team_run_id: str,
        *,
        phase: str,
        lead: str = "团队负责人",
        running: Iterable[str] = (),
        summary: str = "",
        include_members: bool = True,
    ) -> dict[str, Any]:
        tasks = await self.list_tasks(team_run_id)
        running_ids = set(running)
        board_tasks = []
        for task in tasks:
            view_status = _VIEW_STATUS.get(task["board_status"], "pending")
            if task["id"] in running_ids and view_status == "pending":
                view_status = "running"
            item = {
                "id": task["id"],
                "role": task["role"],
                "title": task["title"],
                "brief": task["brief"],
                "depends_on": task["depends_on"],
                "status": view_status,
                "revision": task["revision"],
                "board_status": task["board_status"],
                "owner_member_id": task["owner_member_id"],
                "priority": task["priority"],
            }
            if task.get("result"):
                item["result"] = task["result"]
            if task.get("error"):
                item["error"] = task["error"]
            board_tasks.append(item)
        board: dict[str, Any] = {
            "phase": phase,
            "lead": lead,
            "tasks": board_tasks,
            "team_run_id": team_run_id,
        }
        if summary:
            board["summary"] = summary
        if include_members:
            async with self._db_factory() as db:
                members = await MemberRepository(db).list(team_run_id)
                board["members"] = [
                    {
                        "member_id": member.member_id,
                        "role": member.role,
                        "name": member.name,
                        "status": member.status,
                        "task_id": member.task_id,
                        "thread_id": member.thread_id,
                        "model_calls": member.model_calls,
                        "tool_calls": member.tool_calls,
                        "tokens": member.tokens,
                        "recovery_count": member.recovery_count,
                        "last_error": (member.last_error or "")[:400] or None,
                    }
                    for member in members
                ]
        return board

    # ── 序列化 ───────────────────────────────────────────────────────────

    @staticmethod
    def _serialize(task, dependencies, artifacts_by_id) -> dict[str, Any]:
        artifact = artifacts_by_id.get(task.result_artifact_id)
        return {
            "id": task.task_id,
            "task_id": task.task_id,
            "role": task.role,
            "title": task.subject,
            "brief": task.description,
            "depends_on": [
                dep.depends_on_task_id for dep in dependencies
                if dep.task_id == task.task_id
            ],
            "board_status": task.status,
            "revision": task.revision,
            "owner_member_id": task.owner_member_id,
            "priority": task.priority,
            "write_scopes": list(task.write_scopes or []),
            "retry_count": task.retry_count,
            "max_retries": task.max_retries,
            "lease_token": task.lease_token,
            "lease_expires_at": task.lease_expires_at.isoformat() if task.lease_expires_at else None,
            "result": (artifact.content_preview if artifact else None),
            "result_artifact_id": task.result_artifact_id,
            "error": task.error,
            "created_at": task.created_at.isoformat() if task.created_at else None,
            "updated_at": task.updated_at.isoformat() if task.updated_at else None,
        }
