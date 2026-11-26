"""资源调度与原子预留（任务书 §6.2 / §6.3 / §11.3）。

- 调度前校验用户/租户配额；
- 过滤 offline / draining / 资源不足的 Runner，按剩余资源与活动 Sandbox 数评分；
- 在数据库事务中锁定 Runner 行并原子预留（reserved 计数与预留记录同时提交）；
- 停止 Sandbox 释放 CPU/RAM，保留 Workspace Volume 磁盘配额；
- 重复释放幂等，计数不得为负。
"""
from __future__ import annotations

from typing import Iterable, Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.sandbox import (
    Project, SandboxInstance, SandboxResourceProfile, SandboxResourceReservation,
    SandboxRunnerNode, Workspace,
)

from .common import (
    SandboxQuotaError, SandboxResourceUnavailable, emit_event, new_token, utcnow,
)

ACTIVE_OBSERVED_STATES = ("queued", "provisioning", "starting", "ready", "recovering")


async def check_quota(db: AsyncSession, owner_user_id: int,
                      tenant_id: Optional[str] = None) -> None:
    active_states = ACTIVE_OBSERVED_STATES
    user_count = (await db.execute(
        select(func.count(SandboxInstance.id))
        .join(Workspace, Workspace.id == SandboxInstance.workspace_id)
        .where(
            Workspace.owner_user_id == owner_user_id,
            Workspace.deleted_at.is_(None),
            SandboxInstance.observed_state.in_(active_states),
            SandboxInstance.desired_state != "deleted",
        )
    )).scalar() or 0
    limit = int(settings.SANDBOX_MAX_ACTIVE_PER_USER or 0)
    if limit > 0 and user_count >= limit:
        raise SandboxQuotaError(
            f"已达到用户最大活动 Sandbox 数（{limit}）。请停止不再使用的项目工作空间后重试。"
        )
    tenant_limit = int(settings.SANDBOX_MAX_ACTIVE_PER_TENANT or 0)
    if tenant_limit > 0 and tenant_id:
        tenant_count = (await db.execute(
            select(func.count(SandboxInstance.id))
            .join(Workspace, Workspace.id == SandboxInstance.workspace_id)
            .where(
                Workspace.tenant_id == tenant_id,
                Workspace.deleted_at.is_(None),
                SandboxInstance.observed_state.in_(active_states),
                SandboxInstance.desired_state != "deleted",
            )
        )).scalar() or 0
        if tenant_count >= tenant_limit:
            raise SandboxQuotaError(
                f"已达到租户最大活动 Sandbox 数（{tenant_limit}），请联系管理员调整配额。"
            )


def _available(runner: SandboxRunnerNode) -> tuple[int, int, int]:
    return (
        max(0, int(runner.cpu_allocatable_millis) - int(runner.cpu_reserved_millis)),
        max(0, int(runner.memory_allocatable_bytes) - int(runner.memory_reserved_bytes)),
        max(0, int(runner.disk_allocatable_bytes) - int(runner.disk_reserved_bytes)),
    )


def _fits(runner: SandboxRunnerNode, profile: SandboxResourceProfile) -> bool:
    cpu, mem, disk = _available(runner)
    return (
        cpu >= int(profile.cpu_request_millis)
        and mem >= int(profile.memory_request_bytes)
        and disk >= int(profile.disk_quota_bytes)
    )


async def select_runner(
    db: AsyncSession,
    profile: SandboxResourceProfile,
    *,
    exclude_runner_id: Optional[int] = None,
) -> SandboxRunnerNode:
    """选择并锁定 Runner（行锁）；无可用节点抛 429（排队/拒绝，不无限创建）。"""
    stmt = (
        select(SandboxRunnerNode)
        .where(SandboxRunnerNode.status == "online")
        .order_by(SandboxRunnerNode.active_sandbox_count.asc(), SandboxRunnerNode.id.asc())
        .with_for_update()
    )
    if exclude_runner_id is not None:
        stmt = stmt.where(SandboxRunnerNode.id != exclude_runner_id)
    runners: Iterable[SandboxRunnerNode] = (await db.execute(stmt)).scalars().all()
    candidates = [r for r in runners if _fits(r, profile)]
    if not candidates:
        raise SandboxResourceUnavailable(
            "当前没有满足资源需求的 Sandbox Runner（CPU/内存/磁盘不足或节点离线）。"
            "请稍后重试，或联系管理员扩容 Runner 资源。"
        )
    # 评分：活动 Sandbox 少者优先；同分时剩余内存多者优先（降低 OOM 概率）
    candidates.sort(key=lambda r: (r.active_sandbox_count, -_available(r)[1]))
    return candidates[0]


async def reserve(
    db: AsyncSession,
    runner: SandboxRunnerNode,
    workspace: Workspace,
    profile: SandboxResourceProfile,
    *,
    sandbox_id: Optional[int] = None,
) -> SandboxResourceReservation:
    """原子预留：Runner 计数 + 预留记录同事务提交（调用方负责 commit）。"""
    # 重新锁定并校验（防止 select 与 reserve 之间的并发超卖）
    locked = (await db.execute(
        select(SandboxRunnerNode)
        .where(SandboxRunnerNode.id == runner.id)
        .with_for_update()
    )).scalar_one()
    if locked.status != "online":
        raise SandboxResourceUnavailable("Runner 已离线或正在排空，无法分配 Sandbox")
    if not _fits(locked, profile):
        raise SandboxResourceUnavailable("Runner 资源不足，Sandbox 请求已拒绝/排队")
    locked.cpu_reserved_millis = int(locked.cpu_reserved_millis) + int(profile.cpu_request_millis)
    locked.memory_reserved_bytes = int(locked.memory_reserved_bytes) + int(profile.memory_request_bytes)
    locked.disk_reserved_bytes = int(locked.disk_reserved_bytes) + int(profile.disk_quota_bytes)
    reservation = SandboxResourceReservation(
        runner_id=locked.id,
        workspace_id=workspace.id,
        sandbox_id=sandbox_id,
        cpu_reserved_millis=int(profile.cpu_request_millis),
        memory_reserved_bytes=int(profile.memory_request_bytes),
        disk_reserved_bytes=int(profile.disk_quota_bytes),
        status="reserved",
        reservation_token=new_token(16),
    )
    db.add(reservation)
    await emit_event(
        db, "sandbox.resource_reserved",
        tenant_id=workspace.tenant_id, user_id=workspace.owner_user_id,
        project_id=workspace.project_id, workspace_id=workspace.id,
        sandbox_id=sandbox_id,
        payload={"runner_id": locked.id, "cpu": profile.cpu_request_millis,
                 "memory": profile.memory_request_bytes, "disk": profile.disk_quota_bytes},
    )
    await db.flush()
    return reservation


async def _decrement(db: AsyncSession, runner_id: int,
                     cpu: int, memory: int, disk: int) -> None:
    runner = (await db.execute(
        select(SandboxRunnerNode).where(SandboxRunnerNode.id == runner_id).with_for_update()
    )).scalar_one_or_none()
    if runner is None:
        return
    runner.cpu_reserved_millis = max(0, int(runner.cpu_reserved_millis) - max(0, cpu))
    runner.memory_reserved_bytes = max(0, int(runner.memory_reserved_bytes) - max(0, memory))
    runner.disk_reserved_bytes = max(0, int(runner.disk_reserved_bytes) - max(0, disk))


async def mark_active(db: AsyncSession, sandbox_id: int) -> None:
    reservation = (await db.execute(
        select(SandboxResourceReservation).where(
            SandboxResourceReservation.sandbox_id == sandbox_id,
            SandboxResourceReservation.status.in_(("reserved", "active")),
        )
    )).scalars().first()
    if reservation is not None:
        reservation.status = "active"


async def release_compute(db: AsyncSession, sandbox_id: int) -> None:
    """Sandbox 停止：释放 CPU/RAM，保留磁盘预留（Volume 数据仍在）。"""
    reservation = (await db.execute(
        select(SandboxResourceReservation)
        .where(
            SandboxResourceReservation.sandbox_id == sandbox_id,
            SandboxResourceReservation.status.in_(("reserved", "active")),
        )
        .with_for_update()
    )).scalars().first()
    if reservation is None:
        return
    cpu, mem = int(reservation.cpu_reserved_millis), int(reservation.memory_reserved_bytes)
    reservation.cpu_reserved_millis = 0
    reservation.memory_reserved_bytes = 0
    await _decrement(db, reservation.runner_id, cpu, mem, 0)


async def reserve_compute_for_sandbox(db: AsyncSession, sandbox: SandboxInstance) -> None:
    """停止后重新启动：按 Sandbox 记录重新预留 CPU/RAM（磁盘预留仍保留）。

    资源不足时抛 SandboxResourceUnavailable（启动请求被拒绝），不超卖。
    已持有计算预留时只补差额，重复调用不会重复计数。
    """
    if not sandbox.runner_id:
        raise SandboxResourceUnavailable("Sandbox 未绑定 Runner，无法重新启动")
    runner = (await db.execute(
        select(SandboxRunnerNode)
        .where(SandboxRunnerNode.id == sandbox.runner_id)
        .with_for_update()
    )).scalar_one_or_none()
    if runner is None:
        raise SandboxResourceUnavailable("Sandbox 所属 Runner 不存在")
    reservation = (await db.execute(
        select(SandboxResourceReservation)
        .where(
            SandboxResourceReservation.sandbox_id == sandbox.id,
            SandboxResourceReservation.status.in_(("reserved", "active")),
        )
        .with_for_update()
    )).scalars().first()
    target_cpu = int(sandbox.cpu_request_millis or 0)
    target_memory = int(sandbox.memory_request_bytes or 0)
    current_cpu = int(reservation.cpu_reserved_millis) if reservation else 0
    current_memory = int(reservation.memory_reserved_bytes) if reservation else 0
    delta_cpu = max(0, target_cpu - current_cpu)
    delta_memory = max(0, target_memory - current_memory)
    cpu_available, memory_available, _disk = _available(runner)
    if cpu_available < delta_cpu or memory_available < delta_memory:
        raise SandboxResourceUnavailable(
            "Runner CPU/内存资源不足，Sandbox 启动请求已拒绝，请稍后重试"
        )
    if reservation is None:
        reservation = SandboxResourceReservation(
            runner_id=runner.id,
            workspace_id=sandbox.workspace_id,
            sandbox_id=sandbox.id,
            cpu_reserved_millis=target_cpu,
            memory_reserved_bytes=target_memory,
            disk_reserved_bytes=0,
            status="reserved",
            reservation_token=new_token(16),
        )
        db.add(reservation)
    else:
        reservation.cpu_reserved_millis = target_cpu
        reservation.memory_reserved_bytes = target_memory
    runner.cpu_reserved_millis = int(runner.cpu_reserved_millis) + delta_cpu
    runner.memory_reserved_bytes = int(runner.memory_reserved_bytes) + delta_memory
    await db.flush()


async def lock_owner_for_quota(db: AsyncSession, user_id: int) -> None:
    """串行化同一用户的并发 Provision（锁定用户行，避免配额竞态突破上限）。"""
    from app.models.user import User

    await db.execute(
        select(User).where(User.id == user_id).with_for_update()
    )


async def release_all(db: AsyncSession, sandbox_id: int,
                      *, status: str = "released") -> None:
    """Sandbox 删除：释放全部预留（重复调用幂等）。"""
    reservations = (await db.execute(
        select(SandboxResourceReservation)
        .where(
            SandboxResourceReservation.sandbox_id == sandbox_id,
            SandboxResourceReservation.status.in_(("reserved", "active", "releasing")),
        )
        .with_for_update()
    )).scalars().all()
    for reservation in reservations:
        await _decrement(
            db, reservation.runner_id,
            int(reservation.cpu_reserved_millis),
            int(reservation.memory_reserved_bytes),
            int(reservation.disk_reserved_bytes),
        )
        reservation.cpu_reserved_millis = 0
        reservation.memory_reserved_bytes = 0
        reservation.disk_reserved_bytes = 0
        reservation.status = status
        reservation.released_at = utcnow()


async def recompute_runner_reserved(db: AsyncSession, runner_id: int) -> None:
    """Runner 重启后按数据库预留记录重算计数，避免账目漂移。"""
    rows = (await db.execute(
        select(
            func.coalesce(func.sum(SandboxResourceReservation.cpu_reserved_millis), 0),
            func.coalesce(func.sum(SandboxResourceReservation.memory_reserved_bytes), 0),
            func.coalesce(func.sum(SandboxResourceReservation.disk_reserved_bytes), 0),
        ).where(
            SandboxResourceReservation.runner_id == runner_id,
            SandboxResourceReservation.status.in_(("reserved", "active")),
        )
    )).one()
    runner = (await db.execute(
        select(SandboxRunnerNode).where(SandboxRunnerNode.id == runner_id).with_for_update()
    )).scalar_one_or_none()
    if runner is None:
        return
    runner.cpu_reserved_millis = int(rows[0] or 0)
    runner.memory_reserved_bytes = int(rows[1] or 0)
    runner.disk_reserved_bytes = int(rows[2] or 0)


async def release_reservations_for_workspace(db: AsyncSession, workspace_id: int,
                                             *, status: str = "released") -> None:
    sandbox_ids = (await db.execute(
        select(SandboxInstance.id).where(SandboxInstance.workspace_id == workspace_id)
    )).scalars().all()
    for sandbox_id in sandbox_ids:
        await release_all(db, sandbox_id, status=status)


async def workspace_capacity_summary(db: AsyncSession) -> dict:
    runners = (await db.execute(select(SandboxRunnerNode))).scalars().all()
    return {
        "runners": [
            {
                "id": r.id, "node_name": r.node_name, "status": r.status,
                "cpu_total_millis": r.cpu_total_millis,
                "cpu_allocatable_millis": r.cpu_allocatable_millis,
                "cpu_reserved_millis": r.cpu_reserved_millis,
                "memory_total_bytes": r.memory_total_bytes,
                "memory_allocatable_bytes": r.memory_allocatable_bytes,
                "memory_reserved_bytes": r.memory_reserved_bytes,
                "disk_total_bytes": r.disk_total_bytes,
                "disk_allocatable_bytes": r.disk_allocatable_bytes,
                "disk_reserved_bytes": r.disk_reserved_bytes,
                "active_sandbox_count": r.active_sandbox_count,
                "last_heartbeat_at": r.last_heartbeat_at,
            }
            for r in runners
        ],
        "active_sandboxes": (await db.execute(
            select(func.count(SandboxInstance.id)).where(
                SandboxInstance.observed_state.in_(ACTIVE_OBSERVED_STATES),
                SandboxInstance.desired_state != "deleted",
            )
        )).scalar() or 0,
    }


async def projects_with_active_workspaces(db: AsyncSession, project_ids: Iterable[int]) -> set:
    ids = list(project_ids)
    if not ids:
        return set()
    rows = (await db.execute(
        select(Project.id)
        .join(Workspace, Workspace.project_id == Project.id)
        .join(SandboxInstance, SandboxInstance.workspace_id == Workspace.id)
        .where(
            Project.id.in_(ids),
            SandboxInstance.observed_state.in_(ACTIVE_OBSERVED_STATES),
            SandboxInstance.desired_state != "deleted",
        )
    )).scalars().all()
    return set(rows)
