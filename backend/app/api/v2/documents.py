"""对话产物与文档规范的所有权、版本和下载接口。"""

from collections.abc import Awaitable
from typing import Any, Literal
import asyncio

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.core.database import get_db
from app.models.agent import AgentRun
from app.models.document import DocumentAsset, DocumentProfileVersion, DocumentVersion
from app.models.user import User
from app.services.documents import repository as repo, storage
from app.services.documents.errors import DocumentError
from app.services.documents.office_preview import render_docx_pdf
from app.services.documents.schemas import ProfileDefinition

profiles = APIRouter(prefix="/document-profiles", tags=["文档规范"])
documents = APIRouter(prefix="/documents", tags=["文档"])


class ProfileWrite(BaseModel):
    model_config = ConfigDict(extra="forbid")
    definition: ProfileDefinition
    visibility: Literal["private", "organization"] | None = None
    change_summary: str = Field(default="", max_length=500)
    expected_version: int | None = Field(default=None, ge=1)


class ProfilePublish(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(ge=1)


async def _guard(action: Awaitable):
    try:
        return await action
    except DocumentError as error:
        raise HTTPException(error.status_code, error.message) from None


async def _profile_payload(db: AsyncSession, profile, user: User):
    version_no = await _guard(_current_number(profile, user))
    version = await _guard(repo.pinned_profile(db, profile.id, version_no))
    return repo.profile_dict(profile, version, user.id, user.is_superuser)


async def _current_number(profile, user: User):
    return repo.readable_version(profile, user.id, user.is_superuser)


@profiles.get("")
async def list_profiles(page: int = Query(1, ge=1), page_size: int = Query(30, ge=1, le=100),
                        status: Literal["draft", "published", "archived"] | None = None,
                        db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)):
    items, total = await _guard(repo.list_profiles(db, user.id, user.is_superuser, page, page_size, status))
    return {"items": items, "total": total}


@profiles.post("", status_code=201)
async def create_profile(body: ProfileWrite, db: AsyncSession = Depends(get_db),
                         user: User = Depends(get_current_user)):
    profile, version = await _guard(repo.create_profile(
        db, user.id, body.definition, is_superuser=user.is_superuser,
        visibility=body.visibility or "private", change_summary=body.change_summary,
    ))
    await db.commit()
    return repo.profile_dict(profile, version, user.id, user.is_superuser)


@profiles.get("/{profile_id}")
async def profile_detail(profile_id: int, db: AsyncSession = Depends(get_db),
                         user: User = Depends(get_current_user)):
    profile = await _guard(repo.get_profile(db, profile_id, user.id, user.is_superuser))
    return await _profile_payload(db, profile, user)


@profiles.put("/{profile_id}")
async def edit_profile(profile_id: int, body: ProfileWrite, db: AsyncSession = Depends(get_db),
                       user: User = Depends(get_current_user)):
    profile = await _guard(repo.get_profile(db, profile_id, user.id, user.is_superuser, write=True))
    if body.visibility and body.visibility != profile.visibility:
        if not user.is_superuser:
            raise HTTPException(403, "需要组织规范管理权限")
        profile.visibility = body.visibility
        profile.published_version = None
        profile.status = "draft"
    version = await _guard(repo.update_profile(
        db, profile, user.id, body.definition, change_summary=body.change_summary,
        expected_version=body.expected_version,
    ))
    await db.commit()
    return repo.profile_dict(profile, version, user.id, user.is_superuser)


@profiles.get("/{profile_id}/versions")
async def profile_versions(profile_id: int, db: AsyncSession = Depends(get_db),
                           user: User = Depends(get_current_user)):
    profile = await _guard(repo.get_profile(db, profile_id, user.id, user.is_superuser))
    cutoff = await _guard(_current_number(profile, user))
    number = DocumentProfileVersion.version_no
    visible = number <= cutoff if repo.manage_profile(profile, user.id, user.is_superuser) else number == cutoff
    versions = (await db.scalars(select(DocumentProfileVersion).where(
        DocumentProfileVersion.profile_id == profile_id, visible,
    ))).all()
    items = [repo.profile_version_dict(row) for row in sorted(versions, key=lambda row: row.version_no, reverse=True)]
    return {"items": items, "total": len(items)}


@profiles.post("/{profile_id}/publish")
async def publish_profile(profile_id: int, db: AsyncSession = Depends(get_db),
                          user: User = Depends(get_current_user), body: ProfilePublish | None = None):
    profile = await _guard(repo.get_profile(db, profile_id, user.id, user.is_superuser, write=True))
    if body and body.expected_version != profile.current_version:
        raise HTTPException(409, "规范已更新，请重新加载后确认发布")
    version = await _guard(repo.pinned_profile(db, profile.id, profile.current_version))
    ProfileDefinition.model_validate(version.definition)
    profile.status = "published"
    profile.published_version = profile.current_version
    await db.commit()
    return repo.profile_dict(profile, version, user.id, user.is_superuser)


@profiles.post("/{profile_id}/archive")
async def archive_profile(profile_id: int, db: AsyncSession = Depends(get_db),
                          user: User = Depends(get_current_user)):
    profile = await _guard(repo.get_profile(db, profile_id, user.id, user.is_superuser, write=True))
    profile.status = "archived"
    await db.commit()
    return await _profile_payload(db, profile, user)


@profiles.delete("/{profile_id}", status_code=204)
async def delete_profile(profile_id: int, db: AsyncSession = Depends(get_db),
                         user: User = Depends(get_current_user)):
    from datetime import datetime, timezone
    profile = await _guard(repo.get_profile(db, profile_id, user.id, user.is_superuser, write=True))
    profile.status = "archived"
    profile.deleted_at = datetime.now(timezone.utc)
    await db.commit()
    return Response(status_code=204)


@documents.get("")
async def list_documents(page: int = Query(1, ge=1), page_size: int = Query(30, ge=1, le=100),
                         db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)):
    clause = (DocumentAsset.owner_id == user.id, DocumentAsset.status == "active")
    total = await db.scalar(select(func.count()).select_from(DocumentAsset).where(*clause))
    rows = (await db.scalars(select(DocumentAsset).where(*clause)
        .order_by(DocumentAsset.updated_at.desc(), DocumentAsset.id.desc())
        .offset((page - 1) * page_size).limit(page_size))).all()
    return {"items": [repo.document_dict(row) for row in rows], "total": total or 0}


@documents.get("/tasks")
async def list_document_tasks(page: int = Query(1, ge=1), page_size: int = Query(30, ge=1, le=100),
                              db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)):
    task = AgentRun.input_payload["execution_context"]["task_type"].as_string()
    clause = (AgentRun.user_id == user.id, task.in_(("profile_extract", "document_generate", "document_edit")))
    total = await db.scalar(select(func.count()).select_from(AgentRun).where(*clause))
    rows = (await db.execute(select(
        AgentRun.id, AgentRun.status, AgentRun.session_id, AgentRun.created_at,
        AgentRun.finished_at, task.label("task_type"),
    ).where(*clause).order_by(AgentRun.created_at.desc(), AgentRun.id.desc())
        .offset((page - 1) * page_size).limit(page_size))).mappings().all()
    return {"items": [dict(row) for row in rows], "total": total or 0}


@documents.get("/{document_id}")
async def document_detail(document_id: int, db: AsyncSession = Depends(get_db),
                          user: User = Depends(get_current_user)):
    return repo.document_dict(await _guard(repo.owned_document(db, document_id, user.id)))


@documents.get("/{document_id}/versions")
async def document_versions(document_id: int, db: AsyncSession = Depends(get_db),
                            user: User = Depends(get_current_user)):
    await _guard(repo.owned_document(db, document_id, user.id))
    rows = (await db.scalars(select(DocumentVersion).where(DocumentVersion.document_id == document_id))).all()
    items = [repo.version_dict(row) for row in sorted(rows, key=lambda row: row.version_no, reverse=True)]
    return {"items": items, "total": len(items)}


@documents.get("/{document_id}/versions/{version_no}/preview")
async def preview_document(document_id: int, version_no: int, response: Response,
                           db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)):
    doc = await _guard(repo.owned_document(db, document_id, user.id))
    version = await _guard(repo.pinned_document_version(db, document_id, version_no))
    await _guard(asyncio.to_thread(storage.verified_version, user.id, doc.id, version.version_no,
                                   version.relative_path, version.content_hash, version.size))
    response.headers["Cache-Control"] = "private, no-store"
    return version.preview


@documents.get("/{document_id}/versions/{version_no}/preview.pdf", response_class=Response)
async def preview_document_layout(document_id: int, version_no: int, db: AsyncSession = Depends(get_db),
                                  user: User = Depends(get_current_user)):
    await _guard(repo.owned_document(db, document_id, user.id))
    version = await _guard(repo.pinned_document_version(db, document_id, version_no))
    path = await _guard(asyncio.to_thread(storage.verified_version, user.id, document_id, version.version_no,
                                          version.relative_path, version.content_hash, version.size))
    await db.rollback()
    return Response(await _guard(render_docx_pdf(path)), media_type="application/pdf", headers={
        "Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff",
    })


@documents.get("/{document_id}/versions/{version_no}/download")
async def download_document(document_id: int, version_no: int, db: AsyncSession = Depends(get_db),
                            user: User = Depends(get_current_user)):
    await _guard(repo.owned_document(db, document_id, user.id))
    version = await _guard(repo.pinned_document_version(db, document_id, version_no))
    path = await _guard(asyncio.to_thread(storage.verified_version, user.id, document_id, version.version_no,
                                          version.relative_path, version.content_hash, version.size))
    return FileResponse(path, media_type=storage.DOCX_MIME, filename=version.filename,
                        headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"})


@documents.delete("/{document_id}", status_code=204)
async def delete_document(document_id: int, db: AsyncSession = Depends(get_db),
                          user: User = Depends(get_current_user)):
    row = await _guard(repo.owned_document(db, document_id, user.id, lock=True, allow_deleted=True))
    row.status = "deleted"
    # 先隐藏资产并拒绝并发发布。文件清理失败时同一请求可安全重试。
    await db.commit()
    await _guard(asyncio.to_thread(storage.delete_files, user.id, document_id))
    return Response(status_code=204)
