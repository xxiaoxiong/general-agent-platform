"""从同一 Run 的文档检查点读取提交前可审阅的正文。"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from pathlib import Path
from tempfile import TemporaryDirectory

from app.services.agent_runtime.persistence import CheckpointerFactory

from .errors import DocumentError
from .native_workflow import apply_plan, office_base_path
from .office_edit import OfficeDocument, OfficeEditPlan, OfficeEditError, inspect_office_document
from .schemas import DocumentDraft, DraftSection


async def saved_proposal(thread_id: str) -> dict:
    """部分应用后回读决策前的草稿，历史审阅仍显示当时收到的建议。"""
    checkpointer = await CheckpointerFactory.get().get_checkpointer()
    config = {"configurable": {"thread_id": thread_id}}
    snapshot = await checkpointer.aget_tuple(config)
    current = (snapshot.checkpoint.get("channel_values") or {}) if snapshot else {}
    if not current.get("edit_changes"):
        raise DocumentError(409, "修改稿正在准备或已不可用，请刷新后查看。")
    if current.get("native_edit") or not current.get("accepted_ids"):
        return current
    async for earlier in checkpointer.alist(config, limit=12):
        values = earlier.checkpoint.get("channel_values") or {}
        if values.get("edit_changes") and values.get("draft") and "accepted_ids" not in values:
            return values
    raise DocumentError(410, "此历史任务的修改稿已不可恢复。")


def _section_path(section: DraftSection, parents: list[DraftSection]) -> list[str]:
    while parents and parents[-1].level >= section.level:
        parents.pop()
    parents.append(section)
    return [parent.title for parent in parents]


def _section_view(section: DraftSection, marks: dict[str, list[str]]) -> dict:
    return {
        "id": section.id, "title": section.title, "level": section.level,
        "blocks": [{**block.model_dump(mode="json", exclude={"source_ids"}),
                    "change_ids": marks.get(block.id, [])} for block in section.blocks],
    }


def structured_context(before: DocumentDraft, proposed: DocumentDraft, changes: list[dict]) -> list[dict]:
    updated = {section.id: section for section in proposed.sections}
    by_section = defaultdict(list)
    for item in changes:
        by_section[item["section_id"]].append(item)
    groups, parents = [], []
    for section in sorted(before.sections, key=lambda part: part.order):
        path = _section_path(section, parents)
        items = by_section.get(section.id)
        if not items:
            continue
        after = updated[section.id]
        rewrite = any(item.get("scope") == "section" or not item.get("block_id") for item in items)
        # 旧任务没有记录整节替换的操作类型。大量旧锚点被重新分配时，按整节提供上下文。
        if not rewrite and all("scope" not in item for item in items):
            rewrite = len(items) >= 4 and len(items) * 2 >= max(len(section.blocks), len(after.blocks))
        marks = defaultdict(list)
        if not rewrite:
            for item in items:
                marks[item["block_id"]].append(item["id"])
        groups.append({
            "id": section.id, "title": section.title, "path": path, "mode": "rewrite" if rewrite else "edits",
            "change_ids": [item["id"] for item in items],
            "before": [_section_view(section, marks)], "after": [_section_view(after, marks)],
        })
    return groups


def _office_headings(inventory: OfficeDocument) -> tuple[dict, dict]:
    groups, locations, parents = {}, {}, []
    active = "body"
    for paragraph in inventory.paragraphs:
        if paragraph.location in {"body", "table"}:
            if paragraph.heading_level and paragraph.location == "body":
                while parents and parents[-1].heading_level >= paragraph.heading_level:
                    parents.pop()
                parents.append(paragraph)
                active = paragraph.id
                groups[active] = {"path": [item.section for item in parents], "level": paragraph.heading_level,
                                  "paragraphs": [], "heading_id": paragraph.id}
            key = active
        else:
            key = paragraph.location
        group = groups.setdefault(key, {"path": [paragraph.section], "level": 1, "paragraphs": [], "heading_id": None})
        locations[paragraph.id] = key
        group["paragraphs"].append(paragraph.id)
    return groups, locations


def office_context(run, route: dict, source: Path, plan: OfficeEditPlan, event_changes: list[dict]) -> list[dict]:
    before = inspect_office_document(source)
    with TemporaryDirectory(prefix="document-review-") as directory:
        result = apply_plan(run, route, source, plan, Path(directory) / "proposal.docx")
    after = result.document
    groups, locations = _office_headings(before)
    ids, marks = defaultdict(list), defaultdict(list)
    image_views = {"before": defaultdict(dict), "after": defaultdict(dict)}
    event_by_id = {change["id"]: change for change in event_changes}
    for change in result.changes:
        if change.operation == "replace_text":
            paragraph_id = change.target_id
            marks[paragraph_id].append(change.id)
        else:
            paragraph_id = (change.before or change.after).paragraph_id
            event = event_by_id.get(change.id) or {}
            for side in ("before", "after"):
                picture = getattr(change, side)
                if picture is None:
                    continue
                block_id = f"image_{change.target_id}" if change.before is not None else change.id
                block = image_views[side][paragraph_id].setdefault(block_id, {
                    "id": block_id, "type": "image", "text": picture.caption or picture.alt or "文档图片",
                    "image_url": event.get(f"{side}_image_url"), "change_ids": [],
                })
                block["change_ids"].append(change.id)
                if event.get("replacement_asset_id") and side == "after":
                    block["image_url"] = event.get("after_image_url")
        ids[locations[paragraph_id]].append(change.id)
    paragraphs = {"before": {item.id: item for item in before.paragraphs},
                  "after": {item.id: item for item in after.paragraphs}}

    def section_view(group_key: str, group: dict, side: str) -> dict:
        blocks = []
        for key in group["paragraphs"]:
            paragraph = paragraphs[side][key]
            if paragraph.text.strip() and key != group["heading_id"]:
                blocks.append({"id": key, "type": "paragraph", "text": paragraph.text,
                               "change_ids": marks.get(key, [])})
            blocks.extend(image_views[side].get(key, {}).values())
        heading = paragraphs[side].get(group["heading_id"])
        return {"id": group_key, "title": heading.text if heading else group["path"][-1],
                "level": group["level"], "blocks": blocks}

    return [{"id": key, "title": group["path"][-1], "path": group["path"], "mode": "edits", "change_ids": ids[key],
             "before": [section_view(key, group, "before")], "after": [section_view(key, group, "after")]}
            for key, group in groups.items() if key in ids]


async def build_context(run, route: dict, state: dict) -> dict:
    changes = state["edit_changes"]
    if state.get("native_edit"):
        path = await office_base_path(run, route)
        try:
            groups = await asyncio.to_thread(office_context, run, route, path,
                                             OfficeEditPlan.model_validate(state["office_plan"]), changes)
        except (OfficeEditError, OSError):
            raise DocumentError(409, "无法读取原 Word 的审阅上下文，请检查源文件或放弃本次修改。") from None
    else:
        groups = structured_context(DocumentDraft.model_validate(state["previous"]),
                                    DocumentDraft.model_validate(state["draft"]), changes)
    return {"request": state["request"], "groups": groups, "changes": changes}
