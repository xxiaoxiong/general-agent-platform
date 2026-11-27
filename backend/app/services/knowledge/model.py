"""统一文档模型（任务书 §4）。

所有 Parser 必须输出同一 ``ParsedDocument`` 语义：元素类型、标题路径、
来源定位（页码 / Sheet / 行列 / Slide / 表格序号）与解析统计。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

PARSER_VERSION = "1"
CHUNKER_VERSION = "1"
PIPELINE_VERSION = "1"

# 元素类型（与 Chunker / 引用展示共用的稳定枚举）
EL_HEADING = "heading"
EL_PARAGRAPH = "paragraph"
EL_LIST = "list_item"
EL_TABLE = "table"
EL_CODE = "code"
EL_FORMULA = "formula"
EL_IMAGE = "image"
EL_CAPTION = "caption"
EL_PAGE_BREAK = "page_break"


@dataclass
class Provenance:
    """来源定位（能定位到 PDF 页码 / Excel Sheet 行列 / PPT Slide）。"""

    page: Optional[int] = None
    page_end: Optional[int] = None
    sheet_name: Optional[str] = None
    row_start: Optional[int] = None
    row_end: Optional[int] = None
    slide_number: Optional[int] = None
    table_index: Optional[int] = None
    bbox: Optional[List[float]] = None

    def to_dict(self) -> Dict[str, Any]:
        return {k: v for k, v in self.__dict__.items() if v is not None}

    @classmethod
    def from_dict(cls, data: Optional[Dict[str, Any]]) -> "Provenance":
        if not data:
            return cls()
        allowed = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        return cls(**allowed)


@dataclass
class ParsedElement:
    element_id: str
    type: str
    text: str
    heading_path: List[str] = field(default_factory=list)
    provenance: Provenance = field(default_factory=Provenance)
    level: Optional[int] = None
    table: Optional[Dict[str, Any]] = None
    ocr_score: Optional[float] = None
    meta: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        data: Dict[str, Any] = {
            "element_id": self.element_id,
            "type": self.type,
            "text": self.text,
            "heading_path": list(self.heading_path),
            "provenance": self.provenance.to_dict(),
        }
        if self.level is not None:
            data["level"] = self.level
        if self.table is not None:
            data["table"] = self.table
        if self.ocr_score is not None:
            data["ocr_score"] = round(float(self.ocr_score), 4)
        if self.meta:
            data["meta"] = self.meta
        return data

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ParsedElement":
        return cls(
            element_id=str(data.get("element_id") or ""),
            type=str(data.get("type") or EL_PARAGRAPH),
            text=str(data.get("text") or ""),
            heading_path=list(data.get("heading_path") or []),
            provenance=Provenance.from_dict(data.get("provenance")),
            level=data.get("level"),
            table=data.get("table"),
            ocr_score=data.get("ocr_score"),
            meta=dict(data.get("meta") or {}),
        )


@dataclass
class ParsedDocument:
    document_id: int
    source_name: str
    source_type: str
    title: str
    elements: List[ParsedElement] = field(default_factory=list)
    parse_stats: Dict[str, Any] = field(default_factory=dict)
    warnings: List[str] = field(default_factory=list)
    parser_name: str = "native"
    parser_version: str = PARSER_VERSION
    ocr_pages: List[int] = field(default_factory=list)
    ocr_scores: List[float] = field(default_factory=list)

    def finalize_stats(self) -> Dict[str, Any]:
        """根据元素重新计算统计（Parser 可在结束时调用）。"""
        tables = [el for el in self.elements if el.type == EL_TABLE]
        images = [el for el in self.elements if el.type == EL_IMAGE]
        pages = [
            el.provenance.page for el in self.elements
            if el.provenance.page is not None
        ]
        slides = [
            el.provenance.slide_number for el in self.elements
            if el.provenance.slide_number is not None
        ]
        sheets = {
            el.provenance.sheet_name for el in self.elements
            if el.provenance.sheet_name
        }
        stats = {
            "element_count": len(self.elements),
            "table_count": len(tables),
            "image_count": len(images),
            "char_count": sum(len(el.text or "") for el in self.elements),
            "page_count": max(pages) if pages else 0,
            "slide_count": max(slides) if slides else 0,
            "sheet_count": len(sheets),
            "ocr_pages": sorted(set(self.ocr_pages)),
            "ocr_mean_score": (
                round(sum(self.ocr_scores) / len(self.ocr_scores), 4)
                if self.ocr_scores else None
            ),
            "warning_count": len(self.warnings),
        }
        self.parse_stats = stats
        return stats

    def to_dict(self) -> Dict[str, Any]:
        return {
            "document_id": self.document_id,
            "source_name": self.source_name,
            "source_type": self.source_type,
            "title": self.title,
            "elements": [el.to_dict() for el in self.elements],
            "parse_stats": dict(self.parse_stats),
            "warnings": list(self.warnings),
            "parser_name": self.parser_name,
            "parser_version": self.parser_version,
            "ocr_pages": sorted(set(self.ocr_pages)),
            "ocr_scores": [round(float(s), 4) for s in self.ocr_scores],
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ParsedDocument":
        return cls(
            document_id=int(data.get("document_id") or 0),
            source_name=str(data.get("source_name") or ""),
            source_type=str(data.get("source_type") or ""),
            title=str(data.get("title") or ""),
            elements=[ParsedElement.from_dict(e) for e in (data.get("elements") or [])],
            parse_stats=dict(data.get("parse_stats") or {}),
            warnings=list(data.get("warnings") or []),
            parser_name=str(data.get("parser_name") or "native"),
            parser_version=str(data.get("parser_version") or PARSER_VERSION),
            ocr_pages=list(data.get("ocr_pages") or []),
            ocr_scores=list(data.get("ocr_scores") or []),
        )


class ParseError(Exception):
    """解析失败（必须映射为 failed，不得降级为普通文本冒充成功）。"""

    def __init__(self, message: str, code: str = "parse_failed"):
        super().__init__(message)
        self.code = code


class UnsupportedFormatError(ParseError):
    def __init__(self, message: str):
        super().__init__(message, code="unsupported_format")


class OCRUnavailableError(ParseError):
    def __init__(self, message: str):
        super().__init__(message, code="ocr_unavailable")
