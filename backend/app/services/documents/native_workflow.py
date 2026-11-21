"""原始 Word 文件的编辑来源、模型锚点与审阅缩略图。"""

from __future__ import annotations

import asyncio
import base64
import re
from pathlib import Path

from pydantic import Field

from app.core.database import AsyncSessionLocal

from . import repository, storage
from .errors import DocumentError
from .office_edit import (
    OfficeDocument, OfficeEditError, OfficeEditPlan, OfficeOperation,
    apply_office_edits, office_vision_images,
)
from .parser import DocumentParseError
from .schemas import DocumentSchema
from .workflow_support import model_json, verified_attachment

IMAGE_REQUEST = re.compile(
    r"图片|插图|配图|图题|替代文字|图像|图表|第.{0,8}[张幅]图|图\s*[一二三四五六七八九十\d]+"
    r"|\b(?:image|images|picture|illustration|figure|chart)s?\b", re.IGNORECASE,
)
_IMAGE_FORMATS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff"}
_WINDOW_SIZE = 14_000


class _OfficeProposals(DocumentSchema):
    operations: list[OfficeOperation] = Field(default_factory=list, max_length=60)


def image_attachment_refs(route: dict) -> dict[str, dict]:
    return {f"attachment_{i}": ref for i, ref in enumerate(route.get("attachment_refs") or (), 1)
            if Path(ref["name"]).suffix.casefold() in _IMAGE_FORMATS}


async def office_base_path(run, route: dict) -> Path:
    source = route.get("source_attachment")
    if source:
        return await asyncio.to_thread(verified_attachment, run, source)
    async with AsyncSessionLocal() as db:
        asset = await repository.owned_document(db, route["document_id"], run.user_id)
        version = await repository.pinned_document_version(db, asset.id, route["document_version"])
        locator = (asset.owner_id, asset.id, version.version_no, version.relative_path, version.content_hash, version.size)
    return await asyncio.to_thread(storage.verified_version, *locator)


def vision_content(assets: list[tuple[str, bytes, str]]) -> list[dict]:
    result = []
    for anchor, data, mime in assets:
        result.append({"type": "text", "text": f"图片锚点 {anchor}"})
        result.append({"type": "image_url", "image_url": {
            "url": f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}",
        }})
    return result


def _windows(inventory: OfficeDocument, allowed: set[str] | None, include_images: bool) -> list[list[dict]]:
    pictures = {image.paragraph_id for image in inventory.images} if include_images else set()
    windows: list[list[dict]] = [[]]
    size = 0
    for paragraph in inventory.paragraphs:
        if (allowed is not None and paragraph.id not in allowed) or (
            (not paragraph.text.strip() or not paragraph.editable) and paragraph.id not in pictures
        ):
            continue
        cost = len(paragraph.text) + len(paragraph.section) + 100
        if size and size + cost > _WINDOW_SIZE:
            windows.append([])
            size = 0
        windows[-1].append(paragraph.model_dump(mode="json"))
        size += cost
    return windows


async def plan_office_edits(context, config, *, path: Path, inventory: OfficeDocument,
                            route: dict, request: str, allowed: set[str] | None,
                            picture_ids: set[str] | None = None) -> OfficeEditPlan:
    edit_images = bool(IMAGE_REQUEST.search(request) or image_attachment_refs(route))
    candidates = [{key: value for key, value in image.model_dump(mode="json").items() if key != "checksum"}
                  for image in inventory.images
                  if (allowed is None or image.paragraph_id in allowed)
                  and (picture_ids is None or image.id in picture_ids)]
    if picture_ids and {row["id"] for row in candidates} != picture_ids:
        raise DocumentError(422, "指定图片不在本次修改的章节内")
    visuals = {}
    if edit_images and context.supports_vision and candidates:
        requested = {row["id"] for row in candidates}
        thumbnails = await asyncio.to_thread(office_vision_images, path, requested, limit=4)
        visuals = {item[0]: item for item in thumbnails}
    replacements = [{"id": asset_id, "name": ref["name"]}
                    for asset_id, ref in image_attachment_refs(route).items()]
    operations = []
    for window in _windows(inventory, allowed, edit_images):
        await context.step("plan_office_edits")
        anchors = {paragraph["id"] for paragraph in window}
        images = [image for image in candidates if image["paragraph_id"] in anchors] if edit_images else []
        response = await model_json(context, config, "plan_office_edits",
            "为原 DOCX 提出修改。只瞄准本窗口出现的段落 pN 或图片 imgN。"
            "replace_text 的 before 必须逐字符复制原文，after 为替换文本；不要跨段或跨图片标记。"
            "不相关的窗口返回空 operations。每个操作必须可单独接受或拒绝。"
            "仅在用户要求时编辑图片。替换和插入使用用户上传的附件 ID。"
            "若只有图片元数据，只据此定位并处理尺寸、替代文字或替换，不声称看到了像素。",
            {"request": request, "paragraphs": window, "pictures": images, "replacement_images": replacements},
            _OfficeProposals, images=vision_content([visuals[row["id"]] for row in images if row["id"] in visuals]))
        allowed_images = {row["id"] for row in images if row["editable"]}
        paragraphs = {row["id"] for row in window if row["editable"]}
        for operation in response.operations:
            if operation.operation == "replace_text":
                allowed_targets = paragraphs
            elif operation.operation == "insert_image" and edit_images:
                allowed_targets = paragraphs
            else:
                allowed_targets = allowed_images
            if operation.target_id not in allowed_targets:
                raise DocumentError(422, "模型建议超出用户指定的 Word 修改范围")
            operations.append(operation)
    if not operations:
        raise DocumentError(422, "未找到可应用的改动，请指明段落或图片")
    if len(operations) > 100:
        raise DocumentError(422, "文档改动过多，请分批审阅")
    return OfficeEditPlan(operations=operations)


def apply_plan(run, route: dict, source: Path, plan: OfficeEditPlan, target: Path,
               accepted_ids: set[str] | None = None):
    keys = {getattr(item, "asset_id", None) for item in plan.operations} - {None}
    available = image_attachment_refs(route)
    if not keys <= available.keys():
        raise DocumentError(422, "图片替换须选择本次上传的图片附件")
    try:
        return apply_office_edits(
            source, plan, target,
            image_sources={key: verified_attachment(run, available[key]) for key in keys},
            accepted_ids=accepted_ids,
        )
    except (OfficeEditError, DocumentParseError) as exc:
        raise DocumentError(422, str(exc)) from None
