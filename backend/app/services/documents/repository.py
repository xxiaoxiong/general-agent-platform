"""文档及规范的权限查询与不可变版本事务。"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent import AgentRun
from app.models.document import DocumentAsset, DocumentProfile, DocumentProfileVersion, DocumentVersion
from app.models.user import User
from app.services.documents import storage
from app.services.documents.errors import DocumentError
from app.services.documents.schemas import ProfileDefinition


logger = logging.getLogger(__name__)


def _date(value: datetime) -> str:
    return (value if value.tzinfo else value.replace(tzinfo=timezone.utc)).isoformat()


def manage_profile(row: DocumentProfile, user_id: int, is_superuser: bool) -> bool:
    if row.visibility == "organization":
        return is_superuser
    return row.owner_id == user_id


def _visible(user_id: int, is_superuser: bool):
    private = and_(DocumentProfile.visibility == "private", DocumentProfile.owner_id == user_id)
    org = DocumentProfile.visibility == "organization"
    if not is_superuser:
        org = and_(org, DocumentProfile.status == "published", DocumentProfile.published_version.is_not(None))
    return and_(DocumentProfile.deleted_at.is_(None), or_(private, org))


async def get_profile(db: AsyncSession, profile_id: int, user_id: int, is_superuser: bool = False,
                      *, write: bool = False) -> DocumentProfile:
    query = select(DocumentProfile).where(DocumentProfile.id == profile_id, _visible(user_id, is_superuser))
    if write:
        query = query.with_for_update()
    profile = await db.scalar(query)
    if profile is None:
        raise DocumentError(404, "文档规范不存在")
    if write and not manage_profile(profile, user_id, is_superuser):
        raise DocumentError(403, "需要文档规范管理权限")
    return profile


async def pinned_profile(db: AsyncSession, profile_id: int, version_no: int) -> DocumentProfileVersion:
    row = await db.scalar(select(DocumentProfileVersion).where(
        DocumentProfileVersion.profile_id == profile_id, DocumentProfileVersion.version_no == version_no,
    ))
    if row is None:
        raise DocumentError(404, "文档规范版本不存在")
    return row


def readable_version(profile: DocumentProfile, user_id: int, is_superuser: bool) -> int:
    if manage_profile(profile, user_id, is_superuser):
        return profile.current_version
    if profile.published_version:
        return profile.published_version
    raise DocumentError(404, "文档规范版本不存在")


async def create_profile(
    db: AsyncSession, user_id: int, definition: ProfileDefinition, *, visibility: str = "private",
    is_superuser: bool = False, change_summary: str = "", source_summary: str = "",
    source_metadata: list[dict[str, Any]] | None = None, extraction_confidence: float | None = None,
    run_id: str | None = None,
) -> tuple[DocumentProfile, DocumentProfileVersion]:
    if visibility == "organization" and not is_superuser:
        raise DocumentError(403, "需要组织规范管理权限")
    if run_id:
        previous = await db.scalar(select(DocumentProfileVersion).where(
            DocumentProfileVersion.created_by_run_id == run_id,
        ))
        if previous is not None:
            existing = await db.get(DocumentProfile, previous.profile_id)
            if existing is not None and existing.owner_id == user_id:
                return existing, previous
            raise DocumentError(409, "规范草稿已由此运行处理")
    profile = DocumentProfile(
        owner_id=user_id, name=definition.name, description=definition.description,
        document_type=definition.document_type, visibility=visibility,
    )
    db.add(profile)
    await db.flush()
    version = DocumentProfileVersion(
        profile_id=profile.id, version_no=1, definition=definition.model_dump(mode="json"),
        source_summary=source_summary, source_metadata=source_metadata or [],
        extraction_confidence=extraction_confidence, change_summary=change_summary, created_by=user_id,
        created_by_run_id=run_id,
    )
    db.add(version)
    await db.flush()
    return profile, version


async def update_profile(
    db: AsyncSession, profile: DocumentProfile, user_id: int, definition: ProfileDefinition,
    *, change_summary: str, expected_version: int | None,
) -> DocumentProfileVersion:
    if expected_version is not None and profile.current_version != expected_version:
        raise DocumentError(409, "规范已更新，请重新加载后编辑")
    if profile.status == "archived":
        raise DocumentError(409, "请先重新发布归档规范，再进行修改")
    old = await pinned_profile(db, profile.id, profile.current_version)
    version = DocumentProfileVersion(
        profile_id=profile.id, version_no=profile.current_version + 1, definition=definition.model_dump(mode="json"),
        source_summary=old.source_summary, source_metadata=old.source_metadata,
        extraction_confidence=old.extraction_confidence, change_summary=change_summary, created_by=user_id,
    )
    profile.current_version = version.version_no
    profile.name = definition.name
    profile.description = definition.description
    profile.document_type = definition.document_type
    db.add(version)
    await db.flush()
    return version


def profile_version_dict(version: DocumentProfileVersion) -> dict[str, Any]:
    return {
        "id": version.id, "profile_id": version.profile_id, "version_no": version.version_no,
        "definition": version.definition, "source_summary": version.source_summary,
        "source_metadata": version.source_metadata, "extraction_confidence": version.extraction_confidence,
        "change_summary": version.change_summary, "created_by": version.created_by,
        "created_by_run_id": version.created_by_run_id, "created_at": _date(version.created_at),
    }


def profile_dict(profile: DocumentProfile, version: DocumentProfileVersion,
                 user_id: int, is_superuser: bool) -> dict[str, Any]:
    definition = version.definition
    return {
        "id": profile.id, "owner_id": profile.owner_id, "name": definition["name"],
        "description": definition.get("description", ""), "document_type": definition["document_type"],
        "visibility": profile.visibility, "status": profile.status,
        "current_version": version.version_no, "published_version": profile.published_version,
        "definition": definition, "source_summary": version.source_summary, "source_metadata": version.source_metadata,
        "extraction_confidence": version.extraction_confidence,
        "can_manage": manage_profile(profile, user_id, is_superuser),
        "created_at": _date(profile.created_at), "updated_at": _date(profile.updated_at),
    }


async def list_profiles(db: AsyncSession, user_id: int, is_superuser: bool,
                        page: int, page_size: int, status: str | None = None):
    filters = _visible(user_id, is_superuser)
    if status:
        filters = and_(filters, DocumentProfile.status == status)
    total = await db.scalar(select(func.count()).select_from(DocumentProfile).where(filters))
    rows = (await db.scalars(select(DocumentProfile).where(filters)
        .order_by(DocumentProfile.updated_at.desc(), DocumentProfile.id.desc())
        .offset((page - 1) * page_size).limit(page_size))).all()
    keys = [(row.id, readable_version(row, user_id, is_superuser)) for row in rows]
    # 按页批量加载定义，列表排序始终只使用轻量的规范表。
    versions = {}
    if keys:
        from sqlalchemy import tuple_
        parts = (await db.scalars(select(DocumentProfileVersion).where(
            tuple_(DocumentProfileVersion.profile_id, DocumentProfileVersion.version_no).in_(keys),
        ))).all()
        versions = {(item.profile_id, item.version_no): item for item in parts}
    return [profile_dict(row, versions[key], user_id, is_superuser)
            for row, key in zip(rows, keys) if key in versions], total or 0


async def owned_document(db: AsyncSession, document_id: int, user_id: int, *,
                         lock: bool = False, allow_deleted: bool = False) -> DocumentAsset:
    query = select(DocumentAsset).where(DocumentAsset.id == document_id, DocumentAsset.owner_id == user_id)
    if not allow_deleted:
        query = query.where(DocumentAsset.status == "active")
    document = await db.scalar(query.with_for_update() if lock else query)
    if document is None:
        raise DocumentError(404, "文档不存在")
    return document


async def pinned_document_version(db: AsyncSession, document_id: int, version_no: int) -> DocumentVersion:
    row = await db.scalar(select(DocumentVersion).where(
        DocumentVersion.document_id == document_id, DocumentVersion.version_no == version_no,
    ))
    if row is None:
        raise DocumentError(404, "文档版本不存在")
    return row


def document_dict(document: DocumentAsset) -> dict[str, Any]:
    return {
        "id": document.id, "title": document.title, "document_type": document.document_type,
        "profile_id": document.profile_id, "current_version": document.current_version,
        "status": document.status, "session_id": document.session_id,
        "created_by_run_id": document.created_by_run_id,
        "created_at": _date(document.created_at), "updated_at": _date(document.updated_at),
    }


def version_dict(version: DocumentVersion) -> dict[str, Any]:
    return {
        "id": version.id, "document_id": version.document_id, "version_no": version.version_no,
        "filename": version.filename, "mime_type": version.mime_type, "size": version.size,
        "content_hash": version.content_hash, "profile_id": version.profile_id,
        "profile_version_no": version.profile_version_no, "created_by_run_id": version.created_by_run_id,
        "change_summary": version.change_summary, "validation_report": version.validation_report,
        "version_diff": version.version_diff, "created_at": _date(version.created_at),
        **version_urls(version.document_id, version.version_no),
    }


def version_urls(document_id: int, version_no: int) -> dict[str, str]:
    base = f"/api/v2/documents/{document_id}/versions/{version_no}"
    return {"download_url": f"{base}/download", "preview_url": f"{base}/preview"}


async def lock_run_for_publication(
    db: AsyncSession, run_id: str, user_id: int, fence: Callable[[AsyncSession], Awaitable[None]],
) -> None:
    row = (await db.execute(select(AgentRun.id, AgentRun.status, AgentRun.cancel_requested).where(
        AgentRun.id == run_id, AgentRun.user_id == user_id,
    ).with_for_update())).one_or_none()
    if row is None or row.status != "running" or row.cancel_requested:
        raise DocumentError(409, "运行已结束或正在取消")
    await fence(db)


async def version_by_run(db: AsyncSession, run_id: str, user_id: int):
    result = (await db.execute(select(DocumentAsset, DocumentVersion).join(
        DocumentVersion, DocumentVersion.document_id == DocumentAsset.id,
    ).where(DocumentVersion.created_by_run_id == run_id, DocumentAsset.owner_id == user_id))).one_or_none()
    if result and result[0].status != "active":
        raise DocumentError(409, "此运行生成的文档已删除")
    return result


async def lock_storage_owner(db: AsyncSession, user_id: int) -> None:
    # 新文档提交前没有既存资产行。共同锁定所有者，避免清理时同一存储位置被重新使用。
    await db.scalar(select(User.id).where(User.id == user_id).with_for_update())


async def locked_version_record(db: AsyncSession, location: storage.VersionFile):
    """在独立事务中读取已提交的版本，并保持与发布相同的行锁。"""
    await lock_storage_owner(db, location.owner_id)
    row = await db.scalar(select(DocumentAsset).where(
        DocumentAsset.id == location.document_id, DocumentAsset.owner_id == location.owner_id,
    ).with_for_update())
    if row is None:
        return None
    version = await db.scalar(select(DocumentVersion).where(
        DocumentVersion.document_id == location.document_id,
        DocumentVersion.version_no == location.version_no,
    ).with_for_update())
    return (row, version) if version is not None else None


async def publish_document(
    db: AsyncSession, *, user_id: int, run_id: str, session_id: int | None,
    supplied_document_id: int | None, expected_current_version: int | None,
    title: str, document_type: str, profile_id: int | None, profile_version_no: int | None,
    filename: str, staged: Path, preview: dict, validation_report: dict,
    change_summary: str, version_diff: dict, fence: Callable[[AsyncSession], Awaitable[None]],
) -> tuple[DocumentAsset, DocumentVersion]:
    # 文件验证在进入事务前完成。发布时在同一个短事务中锁住 Run 和资产。
    digest, size = storage.fingerprint(staged)
    await lock_run_for_publication(db, run_id, user_id, fence)
    existing = await version_by_run(db, run_id, user_id)
    if existing:
        return existing[0], existing[1]
    await lock_storage_owner(db, user_id)
    if supplied_document_id is None:
        asset = DocumentAsset(owner_id=user_id, title=title, document_type=document_type, session_id=session_id,
                              profile_id=profile_id, created_by_run_id=run_id)
        db.add(asset)
        await db.flush()
    else:
        asset = await owned_document(db, supplied_document_id, user_id, lock=True)
        if asset.current_version != expected_current_version:
            raise DocumentError(409, "文档已有新版本，请从最新版本重新发起修改")
        asset.current_version += 1
        asset.title = title
        asset.profile_id = profile_id

    number = asset.current_version
    version = DocumentVersion(
        document_id=asset.id, version_no=number, filename=filename,
        relative_path=storage.relative_path(user_id, asset.id, number), mime_type=storage.DOCX_MIME,
        size=size, content_hash=digest, profile_id=profile_id, profile_version_no=profile_version_no,
        created_by_run_id=run_id, change_summary=change_summary[:500], validation_report=validation_report,
        version_diff=version_diff, preview=preview,
    )
    db.add(version)
    await db.flush()
    location = storage.VersionFile(user_id, asset.id, number)
    storage.put_version(staged, user_id, asset.id, number)
    try:
        await db.commit()
    except (Exception, asyncio.CancelledError) as error:
        try:
            await db.rollback()
            async with AsyncSession(bind=db.bind, expire_on_commit=False) as recovery, recovery.begin():
                saved = await locked_version_record(recovery, location)
                if saved is None:
                    storage.discard_version_candidate(location, apply=True, expected=(digest, size))
                elif (not isinstance(error, asyncio.CancelledError) and saved[0].status == "active"
                      and saved[1].created_by_run_id == run_id and (saved[1].content_hash, saved[1].size) == (digest, size)):
                    # 数据库已提交、客户端未收到确认。继续交付已经存在的版本。
                    storage.verified_version(user_id, location.document_id, number, saved[1].relative_path, digest, size)
                    return saved
        except (Exception, asyncio.CancelledError):
            # 数据库事实无法判定时保留文件，后续由维护工具在行锁下复核。
            logger.warning("文档发布需要核对文件归属，document_id=%s version=%s", location.document_id, number)
        raise
    return asset, version


async def session_profile(
    db: AsyncSession, candidates: list[DocumentProfile], session_id: int | None,
) -> DocumentProfile | None:
    """多份可用规范时优先本会话刚保存/发布的那份（会话上下文延续）。"""
    if not session_id or not candidates:
        return None
    row = (await db.execute(
        select(DocumentProfileVersion.profile_id)
        .join(AgentRun, AgentRun.id == DocumentProfileVersion.created_by_run_id)
        .where(DocumentProfileVersion.profile_id.in_([item.id for item in candidates]),
               AgentRun.session_id == session_id)
        .order_by(DocumentProfileVersion.created_at.desc()).limit(1)
    )).scalar_one_or_none()
    return next((item for item in candidates if item.id == row), None) if row else None


async def resolve_task_resources(
    db: AsyncSession, *, user_id: int, is_superuser: bool = False, session_id: int | None = None,
    task_type: str, message: str, profile_id: int | None = None, document_id: int | None = None,
    task_options: dict | None = None, prefer_attachment: bool = False, has_attachment: bool = False,
) -> dict[str, Any]:
    options = task_options or {}
    document = await owned_document(db, document_id, user_id) if document_id is not None else None
    if task_type == "document_edit" and document is None and not prefer_attachment:
        recents = (await db.scalars(select(DocumentAsset).where(
            DocumentAsset.owner_id == user_id, DocumentAsset.status == "active",
        ).order_by(DocumentAsset.updated_at.desc(), DocumentAsset.id.desc()).limit(100))).all()
        named = [item for item in recents if item.title and item.title in message]
        if named:
            document = max(named, key=lambda item: len(item.title))
        elif session_id:
            document = next((item for item in recents if item.session_id == session_id), None)
        if document is None and any(word in message for word in ("上次", "最近", "上一份", "previous", "latest")):
            document = recents[0] if recents else None
        if document is None and not has_attachment:
            raise DocumentError(422, "请先选择要修改的文档，或从文档中心打开该文档")

    document_version = current_version = None
    source = None
    if document is not None:
        current_version = document.current_version
        document_version = options.get("document_version_no") or current_version
        if type(document_version) is not int or document_version < 1:
            raise DocumentError(422, "文档版本必须为正整数")
        source = await pinned_document_version(db, document.id, document_version)

    profile = await get_profile(db, profile_id, user_id, is_superuser) if profile_id is not None else None
    profile_version = None
    if profile is None and source and task_type == "document_edit":
        # 既有文档携带冻结的规范版本，归档规范也可以继续追溯。
        profile_id, profile_version = source.profile_id, source.profile_version_no
    if profile is None and task_type == "document_generate":
        profile_name = DocumentProfileVersion.definition["name"].as_string()
        candidates = (await db.execute(select(DocumentProfile, profile_name).join(
            DocumentProfileVersion, and_(
                DocumentProfileVersion.profile_id == DocumentProfile.id,
                DocumentProfileVersion.version_no == DocumentProfile.published_version,
            ),
        ).where(
            _visible(user_id, is_superuser), DocumentProfile.status == "published",
        ).order_by(DocumentProfile.updated_at.desc(), DocumentProfile.id.desc()).limit(100))).all()
        named = [(item, name) for item, name in candidates if name and name in message]
        if named:
            profile, _ = max(named, key=lambda item: len(item[1]))
        elif any(word in message for word in ("之前的规范", "上次的规范", "已有规范", "这套规范")):
            accessible = [item for item, _ in candidates if item.owner_id == user_id]
            if len(accessible) == 1:
                profile = accessible[0]
            else:
                # 多份可用规范时，优先复用本会话刚提炼并发布的那一份
                profile = await session_profile(db, accessible, session_id)
    if profile is not None:
        if profile.status == "archived":
            raise DocumentError(422, "该文档规范已归档")
        profile_id = profile.id
        profile_version = options.get("profile_version_no") or profile.published_version or readable_version(
            profile, user_id, is_superuser,
        )
        cap = readable_version(profile, user_id, is_superuser)
        if type(profile_version) is not int or profile_version < 1 or profile_version > cap:
            raise DocumentError(404, "文档规范版本不存在")
        if not manage_profile(profile, user_id, is_superuser) and profile_version != profile.published_version:
            raise DocumentError(404, "文档规范版本不存在")
        await pinned_profile(db, profile.id, profile_version)
    return {
        "document_profile_id": profile_id, "document_profile_version": profile_version,
        "document_id": document.id if document else None, "document_version": document_version,
        "document_current_version": current_version, "doc_format": "docx",
    }
