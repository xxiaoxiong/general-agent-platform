"""结构感知 + Token-aware Chunking（任务书 §5）。

不按字符截断：
- 以标题 / 段落 / 列表 / 表格行组 / 代码块为边界；
- Parent-Child：小节超过 Chunk 上限时生成 Parent（最多 parent_max_tokens）
  与 Child（精确召回），命中 Child 后按 Token 预算扩展 Parent / 邻居；
- Chunk 来源继承元素 provenance（页码 / Sheet 行列 / Slide / 表格序号）；
- Content Hash 去重，Chunk ID 由内容与版本稳定生成。
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .model import (
    EL_HEADING,
    EL_TABLE,
    ParsedDocument,
    ParsedElement,
    Provenance,
)
from .tokens import count_tokens, split_text_windows

CHUNKER_VERSION = "1"
_WS_RE = re.compile(r"[ \t\u3000]+")


@dataclass
class ChunkingConfig:
    chunk_size_tokens: int = 512
    chunk_overlap_tokens: int = 64
    parent_max_tokens: int = 1536

    def validate(self) -> None:
        if self.chunk_size_tokens <= 0:
            raise ValueError("chunk_size_tokens 必须大于 0")
        if not 0 <= self.chunk_overlap_tokens < self.chunk_size_tokens:
            raise ValueError("chunk_overlap_tokens 必须 >= 0 且小于 chunk_size_tokens")
        if self.parent_max_tokens < self.chunk_size_tokens:
            raise ValueError("parent_max_tokens 不能小于 chunk_size_tokens")


@dataclass
class Chunk:
    chunk_id: str
    chunk_index: int
    content: str
    content_hash: str
    token_count: int
    parent_chunk_id: Optional[str] = None
    is_parent: bool = False
    heading_path: List[str] = field(default_factory=list)
    element_type: str = "paragraph"
    provenance: Dict[str, Any] = field(default_factory=dict)
    source_element_ids: List[str] = field(default_factory=list)

    def to_row(self, *, knowledge_base_id: int, document_id: int, document_version_id: int,
               index_generation_id: int, filename: str, file_type: str, title: str,
               parser_version: str) -> Dict[str, Any]:
        prov = self.provenance or {}
        return {
            "id": self.chunk_id,
            "knowledge_base_id": knowledge_base_id,
            "document_id": document_id,
            "document_version_id": document_version_id,
            "index_generation_id": index_generation_id,
            "parent_chunk_id": self.parent_chunk_id,
            "chunk_index": self.chunk_index,
            "content": self.content,
            "content_hash": self.content_hash,
            "token_count": self.token_count,
            "filename": filename,
            "file_type": file_type,
            "title": title,
            "heading_path": list(self.heading_path),
            "element_type": self.element_type,
            "page_start": prov.get("page"),
            "page_end": prov.get("page_end") or prov.get("page"),
            "sheet_name": prov.get("sheet_name"),
            "row_start": prov.get("row_start"),
            "row_end": prov.get("row_end"),
            "slide_number": prov.get("slide_number"),
            "table_index": prov.get("table_index"),
            "source_element_ids": list(self.source_element_ids),
            "parser_version": parser_version,
            "chunker_version": CHUNKER_VERSION,
            "is_parent": self.is_parent,
        }


def normalize_content(text: str) -> str:
    return _WS_RE.sub(" ", (text or "").strip())


def content_hash(text: str) -> str:
    return hashlib.sha256(normalize_content(text).encode("utf-8")).hexdigest()


def _chunk_id(kb_id: int, doc_id: int, version_id: int, generation_id: int,
              index: int, digest: str) -> str:
    raw = f"{kb_id}:{doc_id}:{version_id}:{generation_id}:{index}:{digest}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:40]


def _merge_provenance(elements: List[ParsedElement]) -> Dict[str, Any]:
    pages = [el.provenance.page for el in elements if el.provenance.page is not None]
    page_ends = [el.provenance.page_end for el in elements if el.provenance.page_end is not None]
    slides = [el.provenance.slide_number for el in elements if el.provenance.slide_number is not None]
    sheets = [el.provenance.sheet_name for el in elements if el.provenance.sheet_name]
    rows = [el.provenance.row_start for el in elements if el.provenance.row_start is not None]
    row_ends = [el.provenance.row_end for el in elements if el.provenance.row_end is not None]
    table_indexes = [el.provenance.table_index for el in elements if el.provenance.table_index is not None]
    prov: Dict[str, Any] = {}
    if pages:
        prov["page"] = min(pages)
        prov["page_end"] = max(page_ends or pages)
    if slides:
        prov["slide_number"] = min(slides)
    if sheets and len(set(sheets)) == 1:
        prov["sheet_name"] = sheets[0]
    if rows:
        prov["row_start"] = min(rows)
        prov["row_end"] = max(row_ends or rows)
    if table_indexes and len(set(table_indexes)) == 1:
        prov["table_index"] = table_indexes[0]
    return prov


def _element_type(elements: List[ParsedElement]) -> str:
    types = {el.type for el in elements}
    return types.pop() if len(types) == 1 else "mixed"


@dataclass
class _Block:
    text: str
    element: ParsedElement


def _table_blocks(element: ParsedElement, config: ChunkingConfig) -> List[_Block]:
    """表格按行组切分，必要时重复表头；超长表仍受 Parent 上限约束。"""
    table = element.table or {}
    headers: List[str] = [str(h or "") for h in table.get("headers") or []]
    rows: List[List[str]] = table.get("rows") or []
    header_line = " | ".join(h for h in headers if h)
    base_prov = element.provenance
    start_row = table.get("start_row") or base_prov.row_start or 1

    if not rows:
        return [_Block(text=element.text, element=element)]

    header_tokens = count_tokens(header_line) if header_line else 0
    blocks: List[_Block] = []
    current: List[str] = []
    current_tokens = header_tokens
    row_cursor = start_row + 1
    group_start = row_cursor

    def flush() -> None:
        nonlocal current, current_tokens, group_start
        if not current:
            return
        lines = ([header_line] if header_line else []) + current
        prov = Provenance(
            page=base_prov.page, page_end=base_prov.page_end,
            sheet_name=base_prov.sheet_name, row_start=group_start,
            row_end=group_start + len(current) - 1,
            slide_number=base_prov.slide_number, table_index=base_prov.table_index,
        )
        synthetic = ParsedElement(
            element_id=element.element_id,
            type=element.type,
            text="\n".join(lines),
            heading_path=list(element.heading_path),
            provenance=prov,
            table=element.table,
        )
        blocks.append(_Block(text="\n".join(lines), element=synthetic))
        current = []
        current_tokens = header_tokens
        group_start = row_cursor

    for row in rows:
        line = " | ".join(str(c or "") for c in row)
        tokens = count_tokens(line)
        if current and current_tokens + tokens > config.parent_max_tokens:
            flush()
            group_start = row_cursor
        current.append(line)
        current_tokens += tokens
        row_cursor += 1
    flush()
    return blocks or [_Block(text=element.text, element=element)]


def _blocks(document: ParsedDocument, config: ChunkingConfig) -> List[_Block]:
    blocks: List[_Block] = []
    for element in document.elements:
        if element.type == EL_TABLE:
            blocks.extend(_table_blocks(element, config))
        elif element.text.strip():
            blocks.append(_Block(text=element.text.strip(), element=element))
    return blocks


def _group_by_heading(blocks: List[_Block]) -> List[List[_Block]]:
    groups: List[List[_Block]] = []
    current: List[_Block] = []
    current_path: tuple = ()
    for block in blocks:
        path = tuple(block.element.heading_path)
        is_heading = block.element.type == EL_HEADING
        if current and (is_heading or path != current_path):
            groups.append(current)
            current = []
        current.append(block)
        current_path = path
    if current:
        groups.append(current)
    return groups


def _window_blocks(blocks: List[_Block], max_tokens: int) -> List[List[_Block]]:
    windows: List[List[_Block]] = []
    current: List[_Block] = []
    tokens = 0
    for block in blocks:
        block_tokens = count_tokens(block.text)
        if current and tokens + block_tokens > max_tokens:
            windows.append(current)
            current = []
            tokens = 0
        current.append(block)
        tokens += block_tokens
    if current:
        windows.append(current)
    return windows


class StructureAwareChunker:
    def __init__(self, config: ChunkingConfig) -> None:
        config.validate()
        self.config = config

    def chunk(
        self,
        document: ParsedDocument,
        *,
        knowledge_base_id: int,
        document_id: int,
        document_version_id: int,
        index_generation_id: int,
    ) -> List[Chunk]:
        blocks = _blocks(document, self.config)
        groups = _group_by_heading(blocks)
        chunks: List[Chunk] = []
        seen_hashes: set = set()
        index = 0

        def _append(text: str, elements: List[ParsedElement], *, parent_id: Optional[str],
                    is_parent: bool) -> Optional[Chunk]:
            nonlocal index
            text = text.strip()
            if not text:
                return None
            digest = content_hash(text)
            if digest in seen_hashes:
                return None
            seen_hashes.add(digest)
            chunk = Chunk(
                chunk_id=_chunk_id(knowledge_base_id, document_id, document_version_id,
                                   index_generation_id, index, digest),
                chunk_index=index,
                content=text,
                content_hash=digest,
                token_count=count_tokens(text),
                parent_chunk_id=parent_id,
                is_parent=is_parent,
                heading_path=list(elements[-1].heading_path) if elements else [],
                element_type=_element_type(elements),
                provenance=_merge_provenance(elements),
                source_element_ids=list(dict.fromkeys(el.element_id for el in elements)),
            )
            index += 1
            chunks.append(chunk)
            return chunk

        for group in groups:
            for window in _window_blocks(group, self.config.parent_max_tokens):
                content = "\n\n".join(block.text for block in window).strip()
                elements = [block.element for block in window]
                tokens = count_tokens(content)
                if tokens <= self.config.chunk_size_tokens:
                    _append(content, elements, parent_id=None, is_parent=False)
                    continue
                parent = _append(content, elements, parent_id=None, is_parent=True)
                if parent is None:
                    continue
                for text in split_text_windows(
                    content,
                    max_tokens=self.config.chunk_size_tokens,
                    overlap_tokens=self.config.chunk_overlap_tokens,
                ):
                    _append(text, elements, parent_id=parent.chunk_id, is_parent=False)
        return chunks
