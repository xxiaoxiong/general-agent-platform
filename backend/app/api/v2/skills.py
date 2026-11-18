"""Skills 管理 API v2（扩展性治理：技能管理模块）。

- 读：任意登录用户（Builder/工作台可见技能面）；
- 写（创建/修改/启停/删除）：仅超管；
- 内置（builtin）与目录（AGENT_SKILLS_DIR）技能只读展示，不可经 API 修改；
- 技能只做指令注入（/skills/**），**不携带任何工具授权**（fail-closed 口径不变）。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_superuser, get_current_user, get_db
from app.models.skill import Skill
from app.models.user import User
from app.services.agent_runtime.skills import (
    SkillRegistry,
    _parse_frontmatter,
    ensure_skill_frontmatter,
)

router = APIRouter(prefix="/skills", tags=["skills-v2"])


class SkillCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    description: str = ""
    version: str = "1.0"
    body: str = ""
    enabled: bool = True
    # None = 全局注入；[] = 不注入；[1,2] = 仅注入指定 Agent
    agent_ids: Optional[List[int]] = None
    source: str = "managed"


class SkillUpdate(BaseModel):
    description: Optional[str] = None
    version: Optional[str] = None
    body: Optional[str] = None
    enabled: Optional[bool] = None
    agent_ids: Optional[List[int]] = None


def _db_skill_to_dict(s: Skill) -> Dict[str, Any]:
    fm = _parse_frontmatter(s.body or "")
    return {
        "id": s.id,
        "name": s.name,
        "description": s.description or fm.get("description", ""),
        "version": s.version or fm.get("version", "1.0"),
        "body": s.body,
        "enabled": bool(s.enabled),
        "agent_ids": s.agent_ids,
        "scope": "org",
        "source": s.source or "managed",
        "managed": True,
    }


def _builtin_skill_to_dict(meta, body: str) -> Dict[str, Any]:
    return {
        "id": None,
        "name": meta.name,
        "description": meta.description,
        "version": meta.version,
        "body": body,
        "enabled": meta.enabled,
        "agent_ids": None,
        "scope": meta.scope,
        "source": "builtin" if meta.source == "builtin" else "directory",
        "managed": False,
    }


@router.get("")
async def list_skills(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """技能全量清单：内置 + 目录（只读）+ 管理面 DB 技能（可管理）。"""
    registry = SkillRegistry.from_settings()
    items: List[Dict[str, Any]] = []
    for meta in registry.list_skills():
        try:
            body = registry.get_skill_body(meta)
        except OSError:
            body = ""
        items.append(_builtin_skill_to_dict(meta, body))
    rows = (await db.execute(select(Skill).order_by(Skill.id))).scalars().all()
    items.extend(_db_skill_to_dict(s) for s in rows)
    return {"items": items}


@router.post("")
async def create_skill(
    payload: SkillCreate,
    current_user: User = Depends(get_current_superuser),
    db: AsyncSession = Depends(get_db),
):
    exists = (await db.execute(select(Skill).where(Skill.name == payload.name))).scalar_one_or_none()
    if exists:
        raise HTTPException(status_code=400, detail=f"技能名已存在: {payload.name}")
    registry = SkillRegistry.from_settings()
    if any(s.name == payload.name for s in registry.list_skills()):
        raise HTTPException(status_code=400, detail=f"技能名与内置/目录技能冲突: {payload.name}")
    body = ensure_skill_frontmatter(
        payload.body or f"# {payload.name}\n\n（技能说明待补充）\n",
        name=payload.name,
        description=payload.description,
        version=payload.version,
    )
    row = Skill(
        name=payload.name,
        description=payload.description,
        version=payload.version,
        body=body,
        enabled=payload.enabled,
        agent_ids=payload.agent_ids,
        source=payload.source or "managed",
        created_by=current_user.id,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return _db_skill_to_dict(row)


@router.put("/{skill_id}")
async def update_skill(
    skill_id: int,
    payload: SkillUpdate,
    current_user: User = Depends(get_current_superuser),
    db: AsyncSession = Depends(get_db),
):
    row = (await db.execute(select(Skill).where(Skill.id == skill_id))).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="技能不存在")
    if payload.description is not None:
        row.description = payload.description
    if payload.version is not None:
        row.version = payload.version
    if payload.body is not None:
        row.body = ensure_skill_frontmatter(
            payload.body,
            name=row.name,
            description=row.description or payload.description or "",
            version=payload.version or row.version or "1.0",
        )
    if payload.enabled is not None:
        row.enabled = payload.enabled
    if payload.agent_ids is not None:
        row.agent_ids = payload.agent_ids
    await db.commit()
    await db.refresh(row)
    return _db_skill_to_dict(row)


@router.post("/{skill_id}/toggle")
async def toggle_skill(
    skill_id: int,
    current_user: User = Depends(get_current_superuser),
    db: AsyncSession = Depends(get_db),
):
    row = (await db.execute(select(Skill).where(Skill.id == skill_id))).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="技能不存在")
    row.enabled = not row.enabled
    await db.commit()
    await db.refresh(row)
    return _db_skill_to_dict(row)


@router.delete("/{skill_id}")
async def delete_skill(
    skill_id: int,
    current_user: User = Depends(get_current_superuser),
    db: AsyncSession = Depends(get_db),
):
    row = (await db.execute(select(Skill).where(Skill.id == skill_id))).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="技能不存在")
    await db.delete(row)
    await db.commit()
    return {"ok": True}
