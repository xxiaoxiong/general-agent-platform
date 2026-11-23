"""Run 管理端点：人工核对（reconcile）与项目返工（project-repair）。"""
from __future__ import annotations

import asyncio
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Response
from pydantic import BaseModel
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_superuser, get_current_user
from app.core.database import get_db
from app.models.agent import Agent, AgentRun, AgentRunEvent, AgentSession
from app.models.user import User

from app.api.common.schemas import ProjectRepairRequest

from .common import _drain_legacy_stream

router = APIRouter(prefix="/runs", tags=["runs-v2"])


class RunReconcileRequest(BaseModel):
    """人工核对请求（P1-03 §11 D）：needs_reconciliation 执行的管理员处置。"""
    tool_call_id: Optional[str] = None
    idempotency_key: Optional[str] = None
    disposition: str = "unresolved"  # confirmed_succeeded / confirmed_not_executed / unresolved
    external_operation_id: Optional[str] = None
    note: Optional[str] = None


@router.post("/{run_id}/reconcile")
async def reconcile_run(
    run_id: str,
    body: RunReconcileRequest,
    current_user: User = Depends(get_current_superuser),
):
    """人工核对（P1-03 §11 D）：needs_reconciliation 执行的管理员处置。

    仅超管可调用。三处置 confirmed_succeeded / confirmed_not_executed / unresolved；
    条件状态迁移 + 审计（核对人/时间/外部 operation ID/说明）；Checkpoint 丢失时
    不得自动重新推理（本接口从不驱动图执行）。
    """
    from app.services.agent_runtime import get_runtime_service
    from app.services.agent_runtime.service import ApprovalInvalid

    try:
        result = await get_runtime_service().reconcile_run(
            run_id,
            tool_call_id=body.tool_call_id,
            idempotency_key=body.idempotency_key,
            disposition=body.disposition,
            external_operation_id=body.external_operation_id,
            note=body.note,
            reconciled_by=current_user.id,
        )
    except ApprovalInvalid as e:
        raise HTTPException(status_code=400, detail=str(e))
    return result


@router.post("/{run_id}/project-repair")
async def repair_project_run(
    run_id: str,
    body: ProjectRepairRequest,
    response: Response,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key"),
):
    """在同一会话开始一次独立的返工，工作区和运行租约均重新分配。"""
    from app.services.agent_runtime import get_runtime_service
    from app.services.agent_runtime.repositories import IdempotencyConflict, SessionBusy
    from app.services.agent_runtime.service import compute_request_hash

    owner = current_user.id
    source = (await db.execute(select(
        AgentRun.agent_id, AgentRun.session_id, AgentRun.status, AgentRun.input_payload, AgentRun.agent_config_snapshot,
    ).join(AgentSession, AgentSession.id == AgentRun.session_id).join(Agent, Agent.id == AgentRun.agent_id).where(
        AgentRun.id == run_id, AgentRun.user_id == owner,
        AgentSession.user_id == owner, AgentSession.agent_id == AgentRun.agent_id,
        Agent.is_active.is_(True), or_(Agent.owner_id.is_(None), Agent.owner_id == owner),
    ))).one_or_none()
    if source is None:
        raise HTTPException(404, "运行不存在")

    route = (source.input_payload or {}).get("execution_context") or {}
    scope = route.get("project_scope")
    if (source.status != "failed" or route.get("team_mode") is not True
            or route.get("task_type") != "software_project" or scope not in {"fullstack", "frontend", "backend"}):
        raise HTTPException(409, "仅已结束的失败项目草稿可继续修复")
    latest = select(func.max(AgentRunEvent.sequence)).where(
        AgentRunEvent.run_id == run_id, AgentRunEvent.event_type == "project.delivery",
    ).scalar_subquery()
    draft = await db.scalar(select(AgentRunEvent.payload).where(
        AgentRunEvent.run_id == run_id, AgentRunEvent.sequence == latest,
    ))
    if not draft or draft.get("state") != "failed" or not draft.get("download_url"):
        raise HTTPException(409, "这次运行没有可修复的源码草稿")

    context = {"team_mode": True, "task_type": "software_project", "project_scope": scope, "project_base_run_id": run_id}
    config = {**(source.agent_config_snapshot or {}), "id": source.agent_id}
    if body.model_config_id:
        config["model_config_id"] = body.model_config_id
    text = f"继续修复「{str(draft.get('title') or '团队项目')[:140]}」草稿"
    request_hash = compute_request_hash(
        endpoint="project-repair", user_id=owner, agent_id=source.agent_id, session_id=source.session_id,
        user_input=text, agent_config=config, task_request=context,
    )
    await db.commit()
    service = get_runtime_service()
    message = {"role": "user", "content": text}
    try:
        existing = await service.lookup_idempotent(owner, idempotency_key, request_hash) if idempotency_key else None
        if existing is not None:
            new_id, session_id, status = existing.id, existing.session_id, existing.status
        else:
            new_id, session_id, stream = await service.start_run_with_session(
                agent_config=config, messages=[message], user_id=owner, session_id=source.session_id,
                endpoint="project-repair", user_message=message, execution_context=context,
                idempotency_key=idempotency_key, request_hash=request_hash,
            )
            asyncio.ensure_future(_drain_legacy_stream(stream))
            status = "running"
    except (IdempotencyConflict, SessionBusy) as exc:
        raise HTTPException(409, str(exc)) from None
    response.headers["X-Run-Id"] = new_id
    return {"run_id": new_id, "conversation_id": session_id, "status": status,
            "events_url": f"/api/v2/runs/{new_id}/events", "user_message": text}
