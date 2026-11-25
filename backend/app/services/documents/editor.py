"""基于章节和内容块锚点应用局部修改并生成前后差异。"""

from __future__ import annotations

import re
from collections import defaultdict, deque
from collections.abc import Collection, Iterator
from difflib import SequenceMatcher
from hashlib import sha256
from itertools import count

from .schemas import ContentBlock, DocumentDraft, DocumentPatch, DraftSection, PatchOperation, block_text


class DocumentPatchError(ValueError):
    """目标不明确、版本不匹配或越出编辑范围。"""


def _with_sources(block, operation: PatchOperation, *, block_id: str | None = None):
    sources = list(dict.fromkeys((*block.source_ids, *operation.source_ids)))
    changes = {"source_ids": sources}
    if block_id is not None:
        changes["id"] = block_id
    return block.model_copy(deep=True, update=changes)


def _content_similar(actual: str, expected: str) -> bool:
    """抄写误差容忍度：空白、标点或少量省略不视为定位失效。

    章节/内容块 ID 才是修改位置的唯一锚点，expected_before 只是模型自检；
    完全按字符比对会把复制误差当成版本冲突，让整轮修改无谓失败。
    """
    left = re.sub(r"\s+", "", actual)
    right = re.sub(r"\s+", "", expected)
    if not left or not right:
        return False
    return SequenceMatcher(None, left, right).ratio() >= 0.6


def _check_before(actual: str, operation: PatchOperation) -> None:
    if operation.expected_before is None:
        return
    if actual.strip() == operation.expected_before.strip():
        return
    if _content_similar(actual, operation.expected_before):
        return
    excerpt = re.sub(r"\s+", " ", actual.strip())[:160]
    raise DocumentPatchError(f"目标内容与原文不一致，请按原文重新定位修改位置。当前原文开头：{excerpt}")


def _content_key(block: ContentBlock) -> str:
    return block.model_dump_json(exclude={"id", "source_ids"})


def _new_anchors(reserved: set[str], namespace: str) -> Iterator[str]:
    # 已删除的原始 ID 也要保留到本次修改结束，避免增删伪装成替换。
    prefix = f"edit_{sha256(namespace.encode()).hexdigest()[:16]}_" if namespace else "b"
    for number in count(1):
        anchor = f"{prefix}{number}"
        if anchor not in reserved:
            yield anchor


def _rewritten_blocks(section: DraftSection, operation: PatchOperation,
                      new_anchors: Iterator[str]) -> list[ContentBlock]:
    _check_before("\n".join(block_text(block) for block in section.blocks), operation)
    original_by_content = defaultdict(deque)
    for block in section.blocks:
        original_by_content[_content_key(block)].append(block)
    blocks = []
    for block in operation.blocks or []:
        same = original_by_content.get(_content_key(block))
        # 整节改写只继承完全相同的正文块锚点。模型给出的新块 ID 不参与关联。
        blocks.append(same.popleft() if same else _with_sources(block, operation, block_id=next(new_anchors)))
    return blocks


def _edited_blocks(section: DraftSection, operations: list[PatchOperation],
                   new_anchors: Iterator[str]) -> list[ContentBlock]:
    original = {block.id: block for block in section.blocks}
    original_content = {_content_key(block) for block in section.blocks}
    updates = {}
    insertions = defaultdict(list)
    for operation in operations:
        anchor = operation.block_id
        if anchor is not None:
            if anchor not in original:
                raise DocumentPatchError("目标内容块不存在，请以原文中的块 ID 定位修改")
            _check_before(block_text(original[anchor]), operation)
        elif operation.expected_before is not None:
            raise DocumentPatchError("末尾插入没有可以比对的锚点")
        if operation.operation == "insert_block":
            insertions[anchor].append(_with_sources(operation.block, operation, block_id=next(new_anchors)))
            continue
        if anchor in updates:
            raise DocumentPatchError("同一原文块只能修改或删除一次")
        if operation.operation == "remove_block":
            updates[anchor] = None
            continue
        if operation.block.id not in {None, anchor}:
            raise DocumentPatchError("替换内容必须保留现有内容锚点")
        # 允许段落/列表/表格之间转换：结构如何表达由模型按用户要求决定，
        # 具体改动由用户审阅确认（不再用固定的"保持原块类型"硬约束）
        new_content = _content_key(operation.block)
        if new_content in original_content and new_content != _content_key(original[anchor]):
            raise DocumentPatchError("待替换文字来自同节其他原文块，请确认引用位置或按小节重写")
        updates[anchor] = _with_sources(operation.block, operation, block_id=anchor)
    if any(updates.get(anchor, True) is None for anchor in insertions):
        raise DocumentPatchError("新增内容需用保留的原文块定位，请调整插入位置")
    blocks = []
    for anchor, block in original.items():
        replacement = updates.get(anchor, block)
        if replacement is not None:
            blocks.append(replacement)
        blocks.extend(insertions[anchor])
    blocks.extend(insertions[None])
    return blocks


def _patch_section(section: DraftSection, operations: list[PatchOperation], namespace: str) -> DraftSection:
    new_anchors = _new_anchors({block.id for block in section.blocks}, namespace)
    rewrites = [operation for operation in operations if operation.operation == "replace_section"]
    if rewrites and len(operations) != 1:
        raise DocumentPatchError("整节重写需要一次提交该节的完整正文")
    blocks = (_rewritten_blocks(section, rewrites[0], new_anchors) if rewrites
              else _edited_blocks(section, operations, new_anchors))
    return DraftSection.model_validate({
        **section.model_dump(exclude={"blocks"}), "blocks": [block.model_dump() for block in blocks],
    })


def apply_patch(
    draft: DocumentDraft, patch: DocumentPatch, allowed_section_ids: Collection[str] | None = None,
    *, anchor_namespace: str = "",
) -> DocumentDraft:
    """应用指定范围内的修改，不改变其他章节及原草稿。"""
    sections = {section.id: section for section in draft.sections}
    allowed = set(sections) if allowed_section_ids is None else set(allowed_section_ids)
    targets = {operation.section_id for operation in patch.operations}
    if not targets.issubset(sections):
        raise DocumentPatchError("修改目标章节不存在，请明确章节")
    if not targets.issubset(allowed):
        raise DocumentPatchError("修改操作超出用户确认的章节范围")
    operations_by_section = defaultdict(list)
    for operation in patch.operations:
        operations_by_section[operation.section_id].append(operation)
    for section_id, operations in operations_by_section.items():
        sections[section_id] = _patch_section(sections[section_id], operations, anchor_namespace)
    result = DocumentDraft.model_validate({
        **draft.model_dump(exclude={"sections"}), "sections": [section.model_dump() for section in sections.values()],
    })
    if not diff_drafts(draft, result)["sections"]:
        raise DocumentPatchError("修改没有产生内容变化")
    return result


def diff_drafts(before: DocumentDraft, after: DocumentDraft) -> dict:
    """返回仅包含实际改动章节和内容块的结构差异。"""
    previous = {section.id: section for section in before.sections}
    current = {section.id: section for section in after.sections}
    changed = []
    for section_id in dict.fromkeys((*previous, *current)):
        old_section, new_section = previous.get(section_id), current.get(section_id)
        if old_section == new_section:
            continue
        old_blocks = {block.id: block for block in old_section.blocks} if old_section else {}
        new_blocks = {block.id: block for block in new_section.blocks} if new_section else {}
        changed.append({
            "section_id": section_id,
            "title_before": old_section.title if old_section else None,
            "title_after": new_section.title if new_section else None,
            "block_order_before": list(old_blocks),
            "block_order_after": list(new_blocks),
            "blocks": [
                {
                    "block_id": block_id,
                    "before": old_blocks[block_id].model_dump(mode="json") if block_id in old_blocks else None,
                    "after": new_blocks[block_id].model_dump(mode="json") if block_id in new_blocks else None,
                }
                for block_id in dict.fromkeys((*old_blocks, *new_blocks))
                if old_blocks.get(block_id) != new_blocks.get(block_id)
            ],
        })
    return {"sections": changed}
