"""Parser 统一接口与元素构造器（任务书 §2.1 / §4）。"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Protocol

from ..model import (
    EL_CAPTION,
    EL_CODE,
    EL_FORMULA,
    EL_HEADING,
    EL_IMAGE,
    EL_LIST,
    EL_PARAGRAPH,
    EL_TABLE,
    ParsedDocument,
    ParsedElement,
    Provenance,
)


@dataclass
class ParseOptions:
    ocr_enabled: bool = True
    max_pages: int = 500
    timeout_seconds: int = 300
    chunk_size_tokens: int = 512
    chunk_overlap_tokens: int = 64
    extra: Dict[str, Any] = None


class DocumentParser(Protocol):
    name: str
    version: str

    def supported_formats(self) -> List[str]:
        ...

    async def parse(
        self,
        source: Path,
        *,
        file_type: str,
        document_id: int,
        source_name: str,
        options: ParseOptions,
    ) -> ParsedDocument:
        ...


class ElementBuilder:
    """按阅读顺序生成元素：维护标题栈，自动写入 heading_path 与稳定 element_id。"""

    def __init__(self, document: ParsedDocument) -> None:
        self.document = document
        self._seq = 0
        self._stack: List[tuple[int, str]] = []

    def _next_id(self) -> str:
        self._seq += 1
        return f"el_{self._seq:06d}"

    def _path(self) -> List[str]:
        return [text for _level, text in self._stack]

    def heading(self, text: str, *, level: int = 1, provenance: Provenance = None) -> ParsedElement:
        text = (text or "").strip()
        if not text:
            raise ValueError("标题文本为空")
        level = max(1, min(int(level or 1), 6))
        while self._stack and self._stack[-1][0] >= level:
            self._stack.pop()
        self._stack.append((level, text))
        element = ParsedElement(
            element_id=self._next_id(), type=EL_HEADING, text=text,
            heading_path=self._path(), provenance=provenance or Provenance(), level=level,
        )
        self.document.elements.append(element)
        return element

    def paragraph(self, text: str, *, kind: str = EL_PARAGRAPH,
                  provenance: Provenance = None, meta: Dict[str, Any] = None) -> ParsedElement:
        text = (text or "").strip()
        if not text:
            raise ValueError("段落文本为空")
        element = ParsedElement(
            element_id=self._next_id(), type=kind, text=text,
            heading_path=self._path(), provenance=provenance or Provenance(),
            meta=dict(meta or {}),
        )
        self.document.elements.append(element)
        return element

    def list_item(self, text: str, *, provenance: Provenance = None) -> ParsedElement:
        return self.paragraph(text, kind=EL_LIST, provenance=provenance)

    def code(self, text: str, *, provenance: Provenance = None,
             meta: Dict[str, Any] = None) -> ParsedElement:
        return self.paragraph(text, kind=EL_CODE, provenance=provenance, meta=meta)

    def formula(self, text: str, *, provenance: Provenance = None) -> ParsedElement:
        return self.paragraph(text, kind=EL_FORMULA, provenance=provenance)

    def caption(self, text: str, *, provenance: Provenance = None) -> ParsedElement:
        return self.paragraph(text, kind=EL_CAPTION, provenance=provenance)

    def image(self, text: str, *, provenance: Provenance = None,
              ocr_score: float = None) -> ParsedElement:
        element = self.paragraph(
            text or "[图片]", kind=EL_IMAGE, provenance=provenance,
        )
        element.ocr_score = ocr_score
        return element

    def table(self, headers: List[str], rows: List[List[str]], *,
              provenance: Provenance = None, title: str = "",
              start_row: int = None, start_column: int = None,
              merged_cells: List[str] = None) -> ParsedElement:
        headers = [str(h or "").strip() for h in (headers or [])]
        clean_rows = [[str(c or "").strip() for c in row] for row in (rows or [])]
        table = {
            "headers": headers,
            "rows": clean_rows,
            "start_row": start_row,
            "start_column": start_column,
            "merged_cells": list(merged_cells or []),
        }
        lines: List[str] = []
        if title:
            lines.append(title)
        if headers and any(headers):
            lines.append(" | ".join(headers))
        for row in clean_rows:
            lines.append(" | ".join(row))
        text = "\n".join(line for line in lines if line.strip())
        if not text:
            text = "[空表格]"
        element = ParsedElement(
            element_id=self._next_id(), type=EL_TABLE, text=text,
            heading_path=self._path(), provenance=provenance or Provenance(),
            table=table,
        )
        self.document.elements.append(element)
        return element


async def run_sync_in_parser(func, *args, timeout_seconds: int = 300):
    """在线程池执行同步解析，并施加整体超时。"""
    loop = asyncio.get_running_loop()
    future = loop.run_in_executor(None, func, *args)
    return await asyncio.wait_for(future, timeout=max(1, int(timeout_seconds)))
