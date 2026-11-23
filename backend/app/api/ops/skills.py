"""Skills 管理 API v2（扩展性治理：技能管理模块）。

- 读：任意登录用户（Builder/工作台可见技能面）；
- 写（创建/修改/启停/删除）：仅超管；
- 内置（builtin）与目录（AGENT_SKILLS_DIR）技能只读展示，不可经 API 修改；
- 技能 = SKILL.md + 附属文件（scripts/references 等，标准 Agent Skills 形态），
  只做 /skills/** 文件注入，**不携带任何工具授权**（fail-closed 口径不变）。
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
    MAX_SKILL_FILE_BYTES,
    MAX_SKILL_FILE_COUNT,
    MAX_SKILL_TOTAL_BYTES,
    SkillRegistry,
    _parse_frontmatter,
    ensure_skill_frontmatter,
    validate_skill_name,
    validate_skill_relpath,
)

router = APIRouter(prefix="/skills", tags=["skills-v2"])


class SkillCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    description: str = ""
    version: str = "1.0"
    body: str = ""
    # 附属文件：{相对 POSIX 路径: 文本内容}（标准 Agent Skills 形态）
    files: Dict[str, str] = Field(default_factory=dict)
    enabled: bool = True
    # None = 全局注入；[] = 不注入；[1,2] = 仅注入指定 Agent
    agent_ids: Optional[List[int]] = None
    source: str = "managed"


class SkillUpdate(BaseModel):
    description: Optional[str] = None
    version: Optional[str] = None
    body: Optional[str] = None
    # None = 不改；{} = 清空附属文件；{...} = 整体替换
    files: Optional[Dict[str, str]] = None
    enabled: Optional[bool] = None
    agent_ids: Optional[List[int]] = None


def _validate_files(files: Dict[str, str]) -> Dict[str, str]:
    """附属文件写入校验：路径合法 + 单文件/总量/数量上限；失败 400。"""
    out: Dict[str, str] = {}
    if len(files) > MAX_SKILL_FILE_COUNT:
        raise HTTPException(
            status_code=400, detail=f"附属文件数量超限（>{MAX_SKILL_FILE_COUNT}）")
    total = 0
    for path, content in files.items():
        try:
            rel = validate_skill_relpath(path)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=f"附属文件路径非法: {e}") from e
        if not isinstance(content, str):
            raise HTTPException(status_code=400, detail=f"附属文件内容必须是文本: {rel}")
        size = len(content.encode("utf-8"))
        if size > MAX_SKILL_FILE_BYTES:
            raise HTTPException(
                status_code=400,
                detail=f"附属文件超出单文件上限（{MAX_SKILL_FILE_BYTES} 字节）: {rel}")
        total += size
        out[rel] = content
    if total > MAX_SKILL_TOTAL_BYTES:
        raise HTTPException(
            status_code=400,
            detail=f"附属文件总量超限（>{MAX_SKILL_TOTAL_BYTES} 字节）")
    return out


def _db_skill_to_dict(s: Skill) -> Dict[str, Any]:
    fm = _parse_frontmatter(s.body or "")
    return {
        "id": s.id,
        "name": s.name,
        "description": s.description or fm.get("description", ""),
        "version": s.version or fm.get("version", "1.0"),
        "body": s.body,
        "files": s.files or {},
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
        "files": {},
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
    # 窄口径排序（陷阱 #2）：DB 行含 JSON/大文本列，禁止 order_by 全行排序
    rows = (await db.execute(select(Skill))).scalars().all()
    items.extend(_db_skill_to_dict(s) for s in sorted(rows, key=lambda r: r.id))
    return {"items": items}


@router.post("")
async def create_skill(
    payload: SkillCreate,
    current_user: User = Depends(get_current_superuser),
    db: AsyncSession = Depends(get_db),
):
    try:
        name = validate_skill_name(payload.name)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"技能名非法: {e}") from e
    files = _validate_files(payload.files or {})
    exists = (await db.execute(select(Skill).where(Skill.name == name))).scalar_one_or_none()
    if exists:
        raise HTTPException(status_code=400, detail=f"技能名已存在: {name}")
    registry = SkillRegistry.from_settings()
    if any(s.name == name for s in registry.list_skills()):
        raise HTTPException(status_code=400, detail=f"技能名与内置/目录技能冲突: {name}")
    body = ensure_skill_frontmatter(
        payload.body or f"# {name}\n\n（技能说明待补充）\n",
        name=name,
        description=payload.description,
        version=payload.version,
    )
    row = Skill(
        name=name,
        description=payload.description,
        version=payload.version,
        body=body,
        files=files or None,
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
    if payload.files is not None:
        row.files = _validate_files(payload.files) or None
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
