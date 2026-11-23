"""Run 文档审阅端点：审阅上下文恢复与审阅图片读取。"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.core.database import get_db
from app.models.agent import AgentRun, AgentRunEvent
from app.models.user import User

router = APIRouter(prefix="/runs", tags=["runs-v2"])


@router.get("/{run_id}/document-review/context")
async def document_review_context(
    run_id: str, response: Response, db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """使用文档检查点而非事件摘要恢复原文、建议稿及接受范围。"""
    from app.models.document import RunInputRequest
    from app.services.documents.errors import DocumentError
    from app.services.documents.review_context import build_context, saved_proposal

    run = (await db.execute(select(AgentRun.agent_id, AgentRun.session_id, AgentRun.thread_id, AgentRun.input_payload).where(
        AgentRun.id == run_id, AgentRun.user_id == current_user.id,
    ))).one_or_none()
    route = ((run.input_payload or {}).get("execution_context") or {}) if run else {}
    if not run or not run.thread_id or route.get("task_type") != "document_edit":
        raise HTTPException(404, "文档修改稿不存在")
    decision = await db.scalar(select(RunInputRequest.response).where(
        RunInputRequest.run_id == run_id, RunInputRequest.ordinal == 3, RunInputRequest.status == "submitted",
    ))
    scope = SimpleNamespace(user_id=current_user.id, agent_id=run.agent_id, session_id=run.session_id)
    await db.rollback()
    try:
        state = await saved_proposal(run.thread_id)
        result = await build_context(scope, route, state)
    except DocumentError as exc:
        raise HTTPException(exc.status_code, exc.message) from None
    if isinstance(decision, dict) and isinstance(decision.get("accepted_change_ids"), list):
        accepted = decision["accepted_change_ids"]
        result["decision"] = {"accepted_change_ids": accepted, "rejected_change_ids": [
            item["id"] for item in state["edit_changes"] if item["id"] not in accepted
        ]}
    response.headers["Cache-Control"] = "private, no-store"
    return result


@router.get("/{run_id}/document-review/{change_id}/{side}")
async def document_review_image(
    run_id: str, change_id: str, side: Literal["before", "after"],
    db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user),
):
    """读取审阅清单中存在的图片缩略图，锚点和资料都来自持久化的 Run。"""
    from app.services.documents.errors import DocumentError
    from app.services.documents.native_workflow import image_attachment_refs, office_base_path
    from app.services.documents.office_edit import OfficeEditError, office_asset_thumbnail, office_image_bytes
    from app.services.documents.parser import DocumentParseError
    from app.services.documents.review_context import saved_proposal
    from app.services.documents.workflow_support import verified_attachment

    run = (await db.execute(select(AgentRun.agent_id, AgentRun.session_id, AgentRun.thread_id, AgentRun.input_payload).where(
        AgentRun.id == run_id, AgentRun.user_id == current_user.id,
    ))).one_or_none()
    if run is None:
        raise HTTPException(404, "文档审阅图片不存在")
    proposal = await db.scalar(select(AgentRunEvent.payload).where(
        AgentRunEvent.run_id == run_id, AgentRunEvent.event_type == "document.edit.review_required",
    ).order_by(AgentRunEvent.sequence.desc()).limit(1))
    route = (run.input_payload or {}).get("execution_context") or {}
    context = SimpleNamespace(user_id=current_user.id, agent_id=run.agent_id, session_id=run.session_id)
    await db.rollback()
    try:
        candidates = (proposal or {}).get("changes", [])
        if len(candidates) == 20 and not any(item["id"] == change_id for item in candidates) and run.thread_id:
            candidates = (await saved_proposal(run.thread_id))["edit_changes"]
        change = next((item for item in candidates if item["id"] == change_id and item.get(f"{side}_image_url")), None)
        if change is None:
            raise DocumentError(404, "文档审阅图片不存在")
        replacement_id = change.get("replacement_asset_id")
        if side == "after" and replacement_id:
            ref = image_attachment_refs(route).get(replacement_id)
            if not ref:
                raise DocumentError(404, "图片附件不可用")
            path = await asyncio.to_thread(verified_attachment, context, ref)
            data, mime = await asyncio.to_thread(office_asset_thumbnail, path)
        elif change.get("image_anchor"):
            path = await office_base_path(context, route)
            data, mime = await asyncio.to_thread(
                office_image_bytes, path, change["image_anchor"], thumbnail=True,
            )
        else:
            raise DocumentError(404, "文档审阅图片不存在")
    except (DocumentError, OfficeEditError, DocumentParseError, OSError):
        raise HTTPException(404, "文档审阅图片不存在或源文件已变更") from None
    return Response(data, media_type=mime, headers={"Cache-Control": "private, no-store",
                                                    "X-Content-Type-Options": "nosniff"})
