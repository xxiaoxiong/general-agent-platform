"""Docling Parser Adapter（任务书 §3.1）。

生产 Worker 镜像安装 ``docling`` 后由 ``KNOWLEDGE_PARSER_PROVIDER=docling``
启用；``auto`` 模式下可用则优先、不可用回退 NativeParser（显式 docling 时
不可用是错误，不静默降级）。"""
from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.core.config import settings

from ..model import (
    EL_CAPTION,
    EL_CODE,
    EL_FORMULA,
    EL_IMAGE,
    EL_LIST,
    EL_PARAGRAPH,
    EL_TABLE,
    ParseError,
    ParsedDocument,
    Provenance,
)
from .base import DocumentParser, ElementBuilder, ParseOptions

logger = logging.getLogger(__name__)

_CONVERTER = None
_CONVERTER_KEY: Optional[str] = None


def docling_available() -> bool:
    try:
        import docling  # noqa: F401
    except Exception:  # noqa: BLE001
        return False
    return True


def _get_converter(ocr_enabled: bool, table_structure: bool = True):
    """按 OCR / 表格结构配置复用 DocumentConverter（模型首次使用时加载）。"""
    global _CONVERTER, _CONVERTER_KEY
    key = f"ocr={ocr_enabled};table={table_structure}"
    if _CONVERTER is not None and _CONVERTER_KEY == key:
        return _CONVERTER
    try:
        from docling.datamodel.base_models import InputFormat
        from docling.datamodel.pipeline_options import PdfPipelineOptions
        from docling.document_converter import DocumentConverter, PdfFormatOption
    except Exception as exc:  # noqa: BLE001
        raise ParseError(f"Docling 不可用：{exc}", code="docling_unavailable") from exc

    pipeline_options = PdfPipelineOptions()
    pipeline_options.do_ocr = bool(ocr_enabled)
    pipeline_options.do_table_structure = bool(table_structure)
    # 离线部署：使用镜像内预下载的 Docling artifacts（layout / tableformer / OCR）
    artifacts_path = os.environ.get("DOCLING_ARTIFACTS_PATH", "").strip()
    if artifacts_path and Path(artifacts_path).is_dir():
        pipeline_options.artifacts_path = artifacts_path
    converter = DocumentConverter(
        format_options={
            InputFormat.PDF: PdfFormatOption(pipeline_options=pipeline_options),
        }
    )
    _CONVERTER = converter
    _CONVERTER_KEY = key
    return converter


class DoclingParser(DocumentParser):
    name = "docling"
    version = "1"

    def supported_formats(self) -> List[str]:
        return [
            "pdf", "docx", "pptx", "xlsx", "md", "markdown", "html", "htm",
            "csv", "png", "jpg", "jpeg", "tif", "tiff", "asciidoc", "xml",
            "doc", "xls", "ppt", "odt", "ods", "odp", "rtf",
        ]

    async def parse(
        self,
        source: Path,
        *,
        file_type: str,
        document_id: int,
        source_name: str,
        options: ParseOptions,
    ) -> ParsedDocument:
        if not docling_available():
            raise ParseError(
                "KNOWLEDGE_PARSER_PROVIDER=docling 但运行环境未安装 docling"
                "（Worker 镜像需安装 docling；Backend 可用 native）",
                code="docling_unavailable",
            )
        document = ParsedDocument(
            document_id=document_id,
            source_name=source_name,
            source_type=(file_type or "").lower().lstrip("."),
            title=Path(source_name).stem or source_name,
            parser_name=self.name,
            parser_version=self.version,
        )
        converter = await asyncio.to_thread(_get_converter, options.ocr_enabled)
        try:
            result = await asyncio.wait_for(
                asyncio.to_thread(converter.convert, str(source)),
                timeout=max(1, int(options.timeout_seconds)),
            )
        except asyncio.TimeoutError as exc:
            raise ParseError("Docling 解析超时", code="parse_timeout") from exc
        doc = getattr(result, "document", None)
        if doc is None:
            raise ParseError("Docling 未返回文档对象", code="parse_failed")

        # Docling 的 doc.name 是源文件名（上传时带 uuid 前缀），标题优先使用用户文件名
        document.title = Path(source_name).stem or document.title
        builder = ElementBuilder(document)
        table_index = 0
        picture_count = 0
        try:
            iterator = list(doc.iterate_items())
        except Exception as exc:  # noqa: BLE001
            raise ParseError(f"Docling 文档遍历失败：{exc}", code="parse_failed") from exc

        for item, level in iterator:
            label = str(getattr(item, "label", "") or "").lower()
            text = str(getattr(item, "text", "") or "").strip()
            prov = self._provenance(item)
            if label in {"section_header", "title"}:
                heading_level = int(getattr(item, "level", 0) or level or 1)
                if text:
                    builder.heading(text, level=heading_level, provenance=prov)
                continue
            if label in {"table"}:
                table_index += 1
                headers, rows = self._table_rows(item)
                if headers or rows:
                    builder.table(
                        headers, rows,
                        provenance=Provenance(
                            page=prov.page, page_end=prov.page_end,
                            table_index=table_index,
                        ),
                    )
                continue
            if label in {"picture", "image"}:
                picture_count += 1
                caption = self._picture_caption(item)
                if caption:
                    builder.image(caption, provenance=prov)
                continue
            if not text:
                continue
            if label in {"list_item"}:
                builder.list_item(text, provenance=prov)
            elif label in {"code"}:
                builder.code(text, provenance=prov)
            elif label in {"formula"}:
                builder.formula(text, provenance=prov)
            elif label in {"caption"}:
                builder.caption(text, provenance=prov)
            else:
                builder.paragraph(text, provenance=prov)

        if not document.elements:
            raise ParseError("Docling 未提取到任何有效内容", code="empty_extraction")
        if picture_count:
            document.warnings.append(f"文档包含 {picture_count} 张图片（Docling 未输出图片 OCR 文本）")
        stats = document.finalize_stats()
        try:
            stats["page_count"] = max(stats.get("page_count", 0), len(getattr(doc, "pages", {}) or {}))
        except Exception:  # noqa: BLE001
            pass
        if stats.get("char_count", 0) < 8:
            raise ParseError("提取内容过少，无法构建可检索知识", code="empty_extraction")
        return document

    @staticmethod
    def _provenance(item) -> Provenance:
        items = list(getattr(item, "prov", None) or [])
        if not items:
            return Provenance()
        first = items[0]
        last = items[-1]
        page = getattr(first, "page_no", None)
        page_end = getattr(last, "page_no", None)
        bbox = getattr(first, "bbox", None)
        bbox_values = None
        if bbox is not None:
            try:
                bbox_values = [float(getattr(bbox, "l", 0)), float(getattr(bbox, "t", 0)),
                               float(getattr(bbox, "r", 0)), float(getattr(bbox, "b", 0))]
            except Exception:  # noqa: BLE001
                bbox_values = None
        return Provenance(page=page, page_end=page_end, bbox=bbox_values)

    @staticmethod
    def _table_rows(item) -> tuple:
        data = getattr(item, "data", None)
        grid = getattr(data, "grid", None) if data is not None else None
        rows: List[List[str]] = []
        if grid:
            for row in grid:
                cells: List[str] = []
                for cell in row:
                    if cell is None:
                        cells.append("")
                    else:
                        cells.append(str(getattr(cell, "text", "") or "").strip())
                rows.append(cells)
        if not rows:
            try:
                markdown = item.export_to_markdown()
            except Exception:  # noqa: BLE001
                markdown = ""
            for line in str(markdown or "").splitlines():
                stripped = line.strip()
                if stripped.startswith("|") and stripped.endswith("|"):
                    cells = [c.strip() for c in stripped.strip("|").split("|")]
                    if all(set(c) <= {"-", ":"} for c in cells if c):
                        continue
                    rows.append(cells)
        if not rows:
            return [], []
        return rows[0], rows[1:]

    @staticmethod
    def _picture_caption(item) -> str:
        captions: List[str] = []
        try:
            for caption in getattr(item, "captions", None) or []:
                text = getattr(caption, "text", None)
                if text:
                    captions.append(str(text).strip())
        except Exception:  # noqa: BLE001
            pass
        return "\n".join(c for c in captions if c)
