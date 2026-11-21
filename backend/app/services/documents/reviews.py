"""从实际文档差异生成审阅项，按用户选择收敛最终草稿。"""

from __future__ import annotations

from collections.abc import Collection

from pydantic import TypeAdapter

from .editor import diff_drafts
from .schemas import ContentBlock, DocumentDraft, block_text

_BLOCK = TypeAdapter(ContentBlock)
_IMAGE_ACTIONS = {
    "replace_image": "替换图片", "resize_image": "调整尺寸", "remove_image": "移除图片",
    "set_image_alt": "修改替代文字", "insert_image": "插入图片",
}


def structured_changes(before: DocumentDraft, after: DocumentDraft,
                       rewritten_sections: Collection[str] = ()) -> list[dict]:
    changes = []
    original = {item.id: item for item in before.sections}
    candidate = {item.id: item for item in after.sections}
    for section in diff_drafts(before, after)["sections"]:
        name = section["title_after"] or section["title_before"]
        section_id = section["section_id"]
        metadata = {"section_id": section_id, "section_title": name}
        old_order, new_order = section["block_order_before"], section["block_order_after"]
        common = set(old_order) & set(new_order)
        reordered = [key for key in old_order if key in common] != [key for key in new_order if key in common]
        rewrite = section_id in rewritten_sections
        if rewrite or reordered:
            # 内容重写和重排是一个决策。拆开套用会破坏段落、列表和表格之间的语义。
            changes.append({
                "id": f"change_{len(changes) + 1}", **metadata,
                "scope": "section", "title": name, "kind": "text",
                "action": "重写小节" if rewrite else "重排章节内容",
                "before": "\n\n".join(block_text(block) for block in original[section_id].blocks),
                "after": "\n\n".join(block_text(block) for block in candidate[section_id].blocks),
            })
            continue
        for item in section["blocks"]:
            old, new = item["before"], item["after"]
            action = "更新内容" if old and new else ("删除内容" if old else "添加内容")
            changes.append({
                "id": f"change_{len(changes) + 1}", **metadata, "block_id": item["block_id"],
                "scope": "block",
                "title": "表格" if (new or old).get("type") == "table" else "内容块",
                "kind": "table" if (new or old).get("type") == "table" else "text", "action": action,
                "before": block_text(_BLOCK.validate_python(old)) if old else None,
                "after": block_text(_BLOCK.validate_python(new)) if new else None,
            })
    return changes


def accepted_draft(before: DocumentDraft, after: DocumentDraft, changes: list[dict],
                   accepted: set[str]) -> DocumentDraft:
    selected: dict[str, set[str | None]] = {}
    for item in changes:
        if item["id"] in accepted:
            selected.setdefault(item["section_id"], set()).add(item.get("block_id"))
    new_sections = {section.id: section for section in after.sections}
    sections = []
    for section in before.sections:
        keys = selected.get(section.id)
        if not keys:
            sections.append(section)
            continue
        desired = new_sections[section.id]
        if None in keys:
            sections.append(desired)
            continue
        original = {block.id: block for block in section.blocks}
        updated = {block.id: block for block in desired.blocks}
        sequence = [key for key in original if key not in keys or key in updated]
        for index, key in enumerate(updated):
            if key not in keys or key in original:
                continue
            previous = next((item for item in reversed(list(updated)[:index]) if item in sequence), None)
            sequence.insert(sequence.index(previous) + 1 if previous is not None else 0, key)
        blocks = [updated[key] if key in keys else original[key] for key in sequence]
        sections.append(section.model_copy(update={"blocks": blocks}))
    original_refs = {source.id for source in before.sources}
    used_refs = {source_id for section in sections for block in section.blocks for source_id in block.source_ids}
    sources = [source for source in after.sources if source.id in original_refs | used_refs]
    return before.model_copy(update={"sections": sections, "sources": sources})


def _image_details(item) -> str:
    parts = [f"{item.width_mm:g} × {item.height_mm:g} mm"]
    if item.width_pixels and item.height_pixels:
        parts.append(f"{item.width_pixels} × {item.height_pixels} px")
    if item.alt:
        parts.append(f"替代文字 {item.alt}")
    if item.caption:
        parts.append(f"图题 {item.caption}")
    return "\n".join(parts)


def office_review_changes(changes, run_id: str | None = None) -> list[dict]:
    result = []
    for item in changes:
        image = item.operation in _IMAGE_ACTIONS
        row = {
            "id": item.id, "section_title": item.section, "title": item.target_id,
            "kind": "image" if image else "text", "action": _IMAGE_ACTIONS.get(item.operation, "替换文字"),
            "before": _image_details(item.before) if image and item.before is not None else item.before,
            "after": _image_details(item.after) if image and item.after is not None else item.after,
        }
        if image and run_id:
            base = f"/api/v2/runs/{run_id}/document-review/{item.id}"
            if item.before and item.before.previewable:
                row["before_image_url"] = f"{base}/before"
            if item.after and item.after.previewable:
                row["after_image_url"] = f"{base}/after"
            row["image_anchor"] = item.before.id if item.before else None
            if item.replacement_asset_id:
                row["replacement_asset_id"] = item.replacement_asset_id
        result.append(row)
    return result


def decision_schema(changes: list[dict]) -> dict:
    return {
        "type": "object", "required": ["accepted_change_ids"], "additionalProperties": False,
        "properties": {"accepted_change_ids": {
            "title": "批准的修改", "type": "array", "uniqueItems": True,
            "items": {"type": "string", "enum": [item["id"] for item in changes]},
        }},
    }
