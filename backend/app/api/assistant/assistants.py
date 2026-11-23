"""Assistants API v2（§7）：助手列表/创建/草稿/查询/修改/发布。

产品语义（§2.4）：
- POST /assistants/draft：自然语言 → 草稿（UX-02），用户预览确认；
- POST /assistants：确认后创建并立即可用（发布 v1 版本）；
- PATCH /assistants/{id}：自然语言/字段调整（高级配置折叠）；
- POST /assistants/{id}/publish：发布不可变版本（P2-01）。

普通用户不强制填 System Prompt / Temperature / Max Iterations / Tool List
（UX-03）——缺省值由平台决定，模型选择由 ModelRouter 完成。
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.core.database import get_db
from app.models.agent import Agent, AgentVersion
from app.models.user import User
from app.services.agent_runtime.source_bindings import SourceBindingError, check_source_bindings

from app.api.common.schemas import AssistantCreate, AssistantDraftRequest, AssistantPatch
from app.api.common.serializers import assistant_to_dict

router = APIRouter(prefix="/assistants", tags=["assistants-v2"])


async def _get_owned_agent(db: AsyncSession, agent_id: int, user: User) -> Agent:
    res = await db.execute(
        select(Agent).where(Agent.id == agent_id, Agent.is_active == True)  # noqa: E712
    )
    agent = res.scalar_one_or_none()
    if agent is None or agent.owner_id not in (None, user.id):
        raise HTTPException(status_code=404, detail="助手不存在")
    return agent


def _validate_capability_ids(
    capability_ids: Optional[list], resource_bindings: Optional[dict] = None
) -> list:
    """创建/修改时 fail-closed 校验能力包。

    - 未登记 ID 直接拒绝；
    - 资源型能力（data_analysis/knowledge）必须提供资源绑定
      （§2.5：缺失即拒绝，不静默放行）。
    """
    from app.services.assistant.capability import CAPABILITY_PACKS, CapabilityError, resolve_capabilities

    ids = [i for i in (capability_ids or []) if i]
    if not ids:
        return []
    try:
        resolve_capabilities(ids, resource_bindings or {})
    except CapabilityError as e:
        raise HTTPException(status_code=400, detail=str(e))
    unknown = [i for i in ids if i not in CAPABILITY_PACKS]
    if unknown:
        raise HTTPException(status_code=400, detail=f"未登记的能力包: {unknown}")
    return ids


def _bindings(knowledge_base_id, database_connection_id) -> dict:
    return {
        "knowledge_base": knowledge_base_id,
        "database_connection": database_connection_id,
    }


async def _check_bindings(db: AsyncSession, user_id: int, config: dict) -> None:
    try:
        await check_source_bindings(db, user_id, config)
    except SourceBindingError as e:
        raise HTTPException(404, str(e)) from None


@router.get("")
async def list_assistants(
    page: int = 1,
    page_size: int = 20,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """助手列表（A22 用户隔离：自己的 + 公开的；超管全量）。"""
    visibility = Agent.is_active == True  # noqa: E712
    if not current_user.is_superuser:
        visibility = and_(visibility, or_(Agent.owner_id == current_user.id, Agent.owner_id.is_(None)))
    total = (await db.execute(select(func.count(Agent.id)).where(visibility))).scalar()
    res = await db.execute(
        select(Agent).where(visibility)
        .order_by(Agent.is_default.desc(), Agent.updated_at.desc())
        .offset((max(page, 1) - 1) * page_size).limit(page_size)
    )
    agents = res.scalars().all()
    return {
        "items": [assistant_to_dict(a) for a in agents],
        "total": total, "page": page, "page_size": page_size,
    }


@router.post("/draft")
async def draft_assistant(
    body: AssistantDraftRequest,
    current_user: User = Depends(get_current_user),
):
    """自然语言 Builder（UX-02）：描述 → 草稿（不落库，确认后创建）。"""
    from app.services.assistant.builder import build_draft

    try:
        return build_draft(body.description, body.name)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("")
async def create_assistant(
    body: AssistantCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """创建助手（草稿确认后一键创建并立即使用）。"""
    capability_ids = _validate_capability_ids(
        body.capability_ids,
        _bindings(body.knowledge_base_id, body.database_connection_id),
    )
    await _check_bindings(db, current_user.id, {
        "knowledge_base_id": body.knowledge_base_id,
        "database_connection_id": body.database_connection_id,
    })
    agent = Agent(
        name=body.name,
        description=body.description,
        icon=body.icon,
        system_prompt=body.system_prompt or f"你是「{body.name}」，请帮助用户完成任务。",
        capability_ids=capability_ids,
        tools=body.tools,
        model_config_id=body.model_config_id,
        knowledge_base_id=body.knowledge_base_id,
        database_connection_id=body.database_connection_id,
        max_iterations=body.max_iterations,
        temperature=str(body.temperature),
        max_tokens=body.max_tokens,
        owner_id=current_user.id,
        is_default=False,
        is_active=True,
    )
    db.add(agent)
    await db.flush()
    # 创建即发布 v1（Run 绑定版本语义，P2-01）
    from app.services.assistant.versions import publish_version

    version = await publish_version(db, agent.id, created_by=current_user.id,
                                    capability_ids=capability_ids)
    await db.commit()
    await db.refresh(agent)
    out = assistant_to_dict(agent, version)
    out["message"] = "助手已创建并可立即使用"
    return out


@router.get("/{agent_id}")
async def get_assistant(
    agent_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    agent = await _get_owned_agent(db, agent_id, current_user)
    from app.services.assistant.versions import latest_version

    version = await latest_version(db, agent_id)
    return assistant_to_dict(agent, version)


@router.patch("/{agent_id}")
async def patch_assistant(
    agent_id: int,
    body: AssistantPatch,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    agent = await _get_owned_agent(db, agent_id, current_user)
    data = body.dict(exclude_unset=True)
    if "capability_ids" in data:
        data["capability_ids"] = _validate_capability_ids(
            data["capability_ids"],
            _bindings(
                data.get("knowledge_base_id", agent.knowledge_base_id),
                data.get("database_connection_id", agent.database_connection_id),
            ),
        )
    if "temperature" in data and data["temperature"] is not None:
        data["temperature"] = str(data["temperature"])
    if {"knowledge_base_id", "database_connection_id"}.intersection(data):
        await _check_bindings(db, current_user.id, {
            key: data.get(key, getattr(agent, key))
            for key in ("knowledge_base_id", "database_connection_id")
        })
    for field, val in data.items():
        setattr(agent, field, val)
    await db.commit()
    await db.refresh(agent)
    out = assistant_to_dict(agent)
    out["message"] = "已保存（下次运行生效新配置）"
    return out


class PublishRequest(BaseModel):
    capability_ids: Optional[list] = None


@router.delete("/{agent_id}")
async def delete_assistant(
    agent_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """软删除助手（is_active=False）；仅所有者可删。"""
    agent = await _get_owned_agent(db, agent_id, current_user)
    agent.is_active = False
    await db.commit()
    return {"message": "已删除"}


@router.post("/{agent_id}/publish")
async def publish_assistant(
    agent_id: int,
    body: PublishRequest = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """发布不可变版本（P2-01）：快照固化当前配置。"""
    agent = await _get_owned_agent(db, agent_id, current_user)
    from app.services.assistant.versions import publish_version

    await _check_bindings(db, current_user.id, {
        "knowledge_base_id": agent.knowledge_base_id,
        "database_connection_id": agent.database_connection_id,
    })
    capability_ids = None
    if body and body.capability_ids is not None:
        capability_ids = _validate_capability_ids(
            body.capability_ids,
            _bindings(agent.knowledge_base_id, agent.database_connection_id),
        )
    version = await publish_version(db, agent_id, created_by=current_user.id,
                                    capability_ids=capability_ids)
    await db.commit()
    return {
        "id": version.id,
        "agent_id": agent_id,
        "version_no": version.version_no,
        "published_at": version.published_at.isoformat() if version.published_at else None,
    }
