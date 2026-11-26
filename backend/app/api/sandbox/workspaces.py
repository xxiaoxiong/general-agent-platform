"""Workspace / Sandbox 生命周期与事件 API（/api/v2/workspaces）。"""
from __future__ import annotations

import asyncio
import json
from typing import AsyncGenerator

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.core.database import AsyncSessionLocal, get_db
from app.models.sandbox import SandboxEvent, SandboxRunnerNode
from app.models.user import User
from app.services.sandbox import acl, runtime, scheduler, service_manager, write_lease
from app.services.sandbox.common import ensure_enabled

router = APIRouter(prefix="/workspaces", tags=["workspaces-v2"])


@router.get("/{workspace_id}")
async def get_workspace(
    workspace_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    ensure_enabled()
    workspace, project, role = await acl.require_workspace_access(
        db, workspace_id, current_user, "viewer",
    )
    sandbox = await runtime.latest_sandbox(db, workspace.id)
    services = await service_manager.list_services(db, workspace)
    await runtime.touch_workspace(db, workspace.id)
    await db.commit()
    return {
        "workspace": runtime.workspace_to_dict(workspace, sandbox),
        "role": role,
        "project": {"id": project.id, "name": project.name},
        "services": [service_manager.service_to_dict(svc) for svc in services],
        "write_lease": await write_lease.current_write_lease(db, workspace.id),
    }


@router.get("/{workspace_id}/sandbox")
async def get_sandbox(
    workspace_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    ensure_enabled()
    workspace, _project, _role = await acl.require_workspace_access(
        db, workspace_id, current_user, "viewer",
    )
    sandbox = await runtime.latest_sandbox(db, workspace.id)
    return {"sandbox": runtime.sandbox_to_dict(sandbox)}


@router.post("/{workspace_id}/sandbox/start")
async def start_sandbox(
    workspace_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    ensure_enabled()
    workspace, _project, _role = await acl.require_workspace_access(
        db, workspace_id, current_user, "operator",
    )
    sandbox = await runtime.active_sandbox(db, workspace.id)
    if sandbox is None or sandbox.desired_state == "deleted":
        sandbox = await runtime.provision_workspace(
            db, workspace, requested_by=current_user.id,
        )
    elif sandbox.observed_state in ("stopped", "failed"):
        await runtime.request_lifecycle(db, workspace, sandbox, "start", current_user.id)
    await db.commit()
    return {
        "workspace": runtime.workspace_to_dict(workspace, sandbox),
        "sandbox": runtime.sandbox_to_dict(sandbox),
    }


@router.post("/{workspace_id}/sandbox/stop")
async def stop_sandbox(
    workspace_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    ensure_enabled()
    workspace, _project, _role = await acl.require_workspace_access(
        db, workspace_id, current_user, "operator",
    )
    sandbox = await runtime.active_sandbox(db, workspace.id)
    if sandbox is None:
        return {"status": "no_sandbox"}
    if sandbox.observed_state != "stopped":
        await runtime.request_lifecycle(db, workspace, sandbox, "stop", current_user.id)
    await db.commit()
    return {
        "workspace": runtime.workspace_to_dict(workspace, sandbox),
        "sandbox": runtime.sandbox_to_dict(sandbox),
    }


@router.post("/{workspace_id}/sandbox/restart")
async def restart_sandbox(
    workspace_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    ensure_enabled()
    workspace, _project, _role = await acl.require_workspace_access(
        db, workspace_id, current_user, "operator",
    )
    sandbox = await runtime.active_sandbox(db, workspace.id)
    if sandbox is None or sandbox.desired_state == "deleted":
        sandbox = await runtime.provision_workspace(
            db, workspace, requested_by=current_user.id,
        )
    else:
        await runtime.request_lifecycle(db, workspace, sandbox, "restart", current_user.id)
    await db.commit()
    return {
        "workspace": runtime.workspace_to_dict(workspace, sandbox),
        "sandbox": runtime.sandbox_to_dict(sandbox),
    }


@router.get("/{workspace_id}/events")
async def stream_workspace_events(
    workspace_id: int,
    after_id: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """SSE 事件流（只推送当前用户有权限的 Workspace 事件，按 id 增量）。"""
    ensure_enabled()
    await acl.require_workspace_access(db, workspace_id, current_user, "viewer")

    async def _generate() -> AsyncGenerator[str, None]:
        cursor = int(after_id)
        while True:
            async with AsyncSessionLocal() as session:
                rows = (await session.execute(
                    select(SandboxEvent)
                    .where(
                        SandboxEvent.workspace_id == workspace_id,
                        SandboxEvent.id > cursor,
                    )
                    .order_by(SandboxEvent.id.asc())
                    .limit(100)
                )).scalars().all()
            if rows:
                for event in rows:
                    cursor = event.id
                    payload = {
                        "id": event.id,
                        "type": event.event_type,
                        "project_id": event.project_id,
                        "workspace_id": event.workspace_id,
                        "sandbox_id": event.sandbox_id,
                        "generation": event.generation,
                        "job_id": event.job_id,
                        "run_id": event.run_id,
                        "payload": event.payload or {},
                        "created_at": event.created_at.isoformat() if event.created_at else None,
                    }
                    yield f"id: {event.id}\ndata: {json.dumps(payload, ensure_ascii=False, default=str)}\n\n"
            else:
                yield ": keepalive\n\n"
            await asyncio.sleep(1.0)

    return StreamingResponse(
        _generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ── 管理面（超管）：容量与 Runner 状态 ────────────────────────────────────

@router.get("/admin/capacity")
async def sandbox_capacity(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    ensure_enabled()
    if not bool(getattr(current_user, "is_superuser", False)):
        from app.services.sandbox.common import SandboxAccessDenied

        raise SandboxAccessDenied("仅超级管理员可查看 Sandbox 容量")
    return await scheduler.workspace_capacity_summary(db)


@router.post("/admin/runners/{runner_id}/drain")
async def drain_runner(
    runner_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    ensure_enabled()
    if not bool(getattr(current_user, "is_superuser", False)):
        from app.services.sandbox.common import SandboxAccessDenied

        raise SandboxAccessDenied("仅超级管理员可排空 Runner")
    runner = (await db.execute(
        select(SandboxRunnerNode).where(SandboxRunnerNode.id == runner_id)
    )).scalar_one_or_none()
    if runner is None:
        from app.services.sandbox.common import SandboxNotFoundError

        raise SandboxNotFoundError("Runner 不存在")
    runner.status = "draining"
    await db.commit()
    return {"id": runner.id, "node_name": runner.node_name, "status": runner.status}
