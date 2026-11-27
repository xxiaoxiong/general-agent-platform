"""原生结构化解析器（无 Docling 环境的默认实现）。

目标：不依赖重型模型，也保留结构而不是"纯文本导出一坨"：
- PDF：逐页文本 + 页码 + 页眉页脚去重 + 缺文本层页面 OCR；
- DOCX：标题层级 / 列表 / 表格 / 内嵌图片 OCR；
- XLSX：Sheet / 行列坐标 / 表头 / 合并单元格 / 公式与缓存值；
- PPTX：Slide 序号 / 标题 / 文本框 / 表格 / 图片 OCR / 备注；
- TXT/Markdown/CSV/HTML：编码探测、标题、代码块、表格、列表；
- 图片 / 扫描件：RapidOCR；
- 旧版 DOC/XLS/PPT：LibreOffice 转换后按新格式解析，缺少转换器时明确失败；
- ODT/ODS/ODP、RTF：基础结构提取。

局限（如实记录在交付文档）：复杂 PDF 的阅读顺序、跨页表格、公式语义不如
Docling；生产 Worker 镜像安装 Docling 后由 ``KNOWLEDGE_PARSER_PROVIDER=docling``
启用。
"""
from __future__ import annotations

import csv
import datetime as dt
import io
import logging
import os
import re
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from xml.etree import ElementTree

from app.core.config import settings

from ..model import (
    EL_CAPTION,
    EL_CODE,
    EL_IMAGE,
    EL_LIST,
    EL_PARAGRAPH,
    EL_TABLE,
    OCRUnavailableError,
    ParseError,
    ParsedDocument,
    Provenance,
    UnsupportedFormatError,
)
from ..ocr import OCRResult, ocr_available, ocr_image_bytes, render_pdf_page_png
from ..security import detect_encoding, read_text_file
from .base import DocumentParser, ElementBuilder, ParseOptions, run_sync_in_parser

logger = logging.getLogger(__name__)

_HEADING_PATTERNS = [
    (re.compile(r"^第[一二三四五六七八九十百千0-9０-９]+[章节篇部回].{0,60}$"), 1),
    (re.compile(r"^附录[A-Za-z0-9一二三四五六七八九十].{0,60}$"), 1),
    (re.compile(r"^[0-9]+(\.[0-9]+){0,3}[\s、.．].{0,60}$"), 2),
    (re.compile(r"^[一二三四五六七八九十]+[、.．].{0,60}$"), 2),
]

_MD_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
_MD_LIST_RE = re.compile(r"^\s*([-*+]|\d+[.)])\s+")
_MD_FENCE_RE = re.compile(r"^```")


def _heading_level(line: str) -> Optional[int]:
    text = (line or "").strip()
    if not text or len(text) > 80:
        return None
    for pattern, level in _HEADING_PATTERNS:
        if pattern.match(text):
            if pattern.pattern.startswith("^[0-9]+"):
                return min(6, text.count(".") + 1)
            return level
    return None


def _dedupe_page_headers_footers(pages: List[List[str]]) -> None:
    """删除在多数页面重复出现的页眉页脚行（原地修改）。"""
    total = len([p for p in pages if p])
    if total < 3:
        return
    threshold = max(3, int(total * 0.6))

    def _count(lines: List[str], from_start: bool) -> Dict[str, int]:
        counter: Dict[str, int] = {}
        for page in pages:
            candidates = [ln.strip() for ln in (page[:2] if from_start else page[-2:]) if ln.strip()]
            for line in set(candidates):
                if len(line) <= 100:
                    counter[line] = counter.get(line, 0) + 1
        return counter

    def _remove(page: List[str], counter: Dict[str, int], from_start: bool) -> List[str]:
        repeated = {line for line, count in counter.items() if count >= threshold}
        result = list(page)
        indexes = range(min(2, len(result))) if from_start else range(max(0, len(result) - 2), len(result))
        drop = {i for i in indexes if result[i].strip() in repeated}
        return [line for i, line in enumerate(result) if i not in drop]

    head_counter = _count(pages, True)
    foot_counter = _count(pages, False)
    for i, page in enumerate(pages):
        page = _remove(page, head_counter, True)
        page = _remove(page, foot_counter, False)
        pages[i] = page


class NativeParser(DocumentParser):
    name = "native"
    version = "1"

    def supported_formats(self) -> List[str]:
        return [
            "pdf", "docx", "doc", "xlsx", "xls", "pptx", "ppt",
            "txt", "md", "markdown", "csv", "html", "htm",
            "png", "jpg", "jpeg", "tif", "tiff",
            "rtf", "odt", "ods", "odp",
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
        ext = (file_type or "").lower().lstrip(".")
        if ext not in self.supported_formats():
            raise UnsupportedFormatError(f"原生解析器不支持格式 .{ext}")
        document = ParsedDocument(
            document_id=document_id,
            source_name=source_name,
            source_type=ext,
            title=Path(source_name).stem or source_name,
            parser_name=self.name,
            parser_version=self.version,
        )
        return await run_sync_in_parser(
            self._parse_sync, source, ext, document, options,
            timeout_seconds=options.timeout_seconds,
        )

    # ── 同步分派 ──────────────────────────────────────────────────────────

    def _parse_sync(self, source: Path, ext: str, document: ParsedDocument,
                    options: ParseOptions) -> ParsedDocument:
        try:
            if ext == "pdf":
                self._parse_pdf(source, document, options)
            elif ext == "docx":
                self._parse_docx(source, document, options)
            elif ext in {"doc", "xls", "ppt"}:
                converted = self._convert_legacy(source, ext)
                try:
                    target = {"doc": "docx", "xls": "xlsx", "ppt": "pptx"}[ext]
                    if target == "docx":
                        self._parse_docx(converted, document, options)
                    elif target == "xlsx":
                        self._parse_xlsx(converted, document, options)
                    else:
                        self._parse_pptx(converted, document, options)
                    document.warnings.append(f"旧版 .{ext} 已经 LibreOffice 转换为 .{target} 后解析")
                finally:
                    shutil.rmtree(converted.parent, ignore_errors=True)
            elif ext == "xlsx":
                self._parse_xlsx(source, document, options)
            elif ext == "pptx":
                self._parse_pptx(source, document, options)
            elif ext in {"txt", "md", "markdown"}:
                self._parse_text(source, document, options, markdown=ext in {"md", "markdown"})
            elif ext == "csv":
                self._parse_csv(source, document, options)
            elif ext in {"html", "htm"}:
                self._parse_html(source, document, options)
            elif ext in {"png", "jpg", "jpeg", "tif", "tiff"}:
                self._parse_image(source, document, options)
            elif ext in {"odt", "ods", "odp"}:
                self._parse_odf(source, document, options)
            elif ext == "rtf":
                self._parse_rtf(source, document, options)
            else:
                raise UnsupportedFormatError(f"未实现的格式 .{ext}")
        except (ParseError, OCRUnavailableError):
            raise
        except Exception as exc:  # noqa: BLE001
            logger.exception("解析失败：%s", source)
            raise ParseError(f"解析失败：{exc}", code="parse_failed") from exc
        if not document.elements:
            raise ParseError("未提取到任何有效内容（空文档或格式不支持）", code="empty_extraction")
        document.finalize_stats()
        if document.parse_stats.get("char_count", 0) < 8:
            raise ParseError("提取内容过少，无法构建可检索知识", code="empty_extraction")
        return document

    # ── PDF ───────────────────────────────────────────────────────────────

    def _parse_pdf(self, source: Path, document: ParsedDocument, options: ParseOptions) -> None:
        try:
            from pypdf import PdfReader
        except Exception as exc:  # noqa: BLE001
            raise ParseError("缺少 pypdf，无法解析 PDF") from exc

        reader = PdfReader(str(source))
        page_count = len(reader.pages)
        if page_count > options.max_pages:
            raise ParseError(
                f"PDF 页数 {page_count} 超过上限 {options.max_pages}", code="too_many_pages"
            )
        pages_lines: List[List[str]] = []
        page_texts: List[str] = []
        for page in reader.pages:
            try:
                text = page.extract_text() or ""
            except Exception as exc:  # noqa: BLE001
                document.warnings.append(f"PDF 页面文本提取失败：{exc}")
                text = ""
            page_texts.append(text)
            pages_lines.append(text.splitlines())
        _dedupe_page_headers_footers(pages_lines)

        builder = ElementBuilder(document)
        for index, lines in enumerate(pages_lines, start=1):
            text = "\n".join(lines).strip()
            provenance = Provenance(page=index)
            if len(text) < int(settings.KNOWLEDGE_OCR_MIN_TEXT_CHARS or 0):
                ocr_text = ""
                if options.ocr_enabled and ocr_available():
                    try:
                        png = render_pdf_page_png(str(source), index - 1, scale=2.0)
                        result = ocr_image_bytes(png)
                        ocr_text = result.text
                        if ocr_text:
                            document.ocr_pages.append(index)
                            document.ocr_scores.append(result.mean_score)
                            if result.low_quality:
                                document.warnings.append(
                                    f"第 {index} 页 OCR 置信度较低（{result.mean_score:.2f}），标记 needs_review"
                                )
                    except OCRUnavailableError as exc:
                        document.warnings.append(f"第 {index} 页 OCR 不可用：{exc}")
                if not ocr_text:
                    if text:
                        document.warnings.append(
                            f"第 {index} 页文本层过少（{len(text)} 字符），OCR 未能补全，保留原始文本"
                        )
                    else:
                        document.warnings.append(f"第 {index} 页无可提取文本，且 OCR 未能补全")
                        continue
                else:
                    text = ocr_text
            self._emit_pdf_blocks(builder, text, provenance)

    @staticmethod
    def _emit_pdf_blocks(builder: ElementBuilder, text: str, provenance: Provenance) -> None:
        blocks: List[List[str]] = []
        current: List[str] = []
        for line in text.splitlines():
            if line.strip():
                current.append(line.rstrip())
            elif current:
                blocks.append(current)
                current = []
        if current:
            blocks.append(current)
        for block in blocks:
            first = block[0].strip()
            level = _heading_level(first)
            if level is not None:
                builder.heading(first, level=level, provenance=provenance)
                rest = "\n".join(block[1:]).strip()
                if rest:
                    builder.paragraph(rest, provenance=provenance)
                continue
            content = "\n".join(block).strip()
            if content:
                builder.paragraph(content, provenance=provenance)

    # ── DOCX ──────────────────────────────────────────────────────────────

    def _parse_docx(self, source: Path, document: ParsedDocument, options: ParseOptions) -> None:
        try:
            from docx import Document as DocxDocument
            from docx.table import Table as DocxTable
            from docx.text.paragraph import Paragraph as DocxParagraph
        except Exception as exc:  # noqa: BLE001
            raise ParseError("缺少 python-docx，无法解析 Word") from exc

        doc = DocxDocument(str(source))
        builder = ElementBuilder(document)
        table_index = 0

        def _iter_blocks():
            try:
                yield from doc.iter_inner_content()
            except AttributeError:
                body = doc.element.body
                for child in body.iterchildren():
                    if child.tag.endswith("}p"):
                        yield DocxParagraph(child, doc)
                    elif child.tag.endswith("}tbl"):
                        yield DocxTable(child, doc)

        for block in _iter_blocks():
            if isinstance(block, DocxParagraph):
                text = (block.text or "").strip()
                if not text:
                    continue
                style_name = ""
                try:
                    style_name = block.style.name if block.style is not None else ""
                except Exception:  # noqa: BLE001
                    style_name = ""
                heading_match = re.search(r"(?:Heading|标题)\s*(\d+)", style_name, re.IGNORECASE)
                is_list = bool(
                    re.search(r"list", style_name, re.IGNORECASE)
                    or (block._p.pPr is not None and block._p.pPr.numPr is not None)
                )
                if heading_match:
                    builder.heading(text, level=int(heading_match.group(1)), provenance=Provenance())
                elif is_list:
                    builder.list_item(text, provenance=Provenance())
                else:
                    builder.paragraph(text, provenance=Provenance())
            elif isinstance(block, DocxTable):
                headers, rows, merged = self._docx_table_rows(block)
                if not headers and not rows:
                    continue
                table_index += 1
                prov = Provenance(table_index=table_index, row_start=1, row_end=len(rows) + 1)
                builder.table(headers, rows, provenance=prov, merged_cells=merged)

        # 内嵌图片 OCR（受数量限制）
        if options.ocr_enabled and ocr_available():
            image_count = 0
            for shape in getattr(doc, "inline_shapes", []) or []:
                if image_count >= 50:
                    document.warnings.append("内嵌图片超过 50 张，后续图片未 OCR")
                    break
                image_count += 1
                try:
                    blob = self._docx_image_blob(doc, shape)
                except Exception:  # noqa: BLE001
                    continue
                if not blob:
                    continue
                try:
                    result = ocr_image_bytes(blob)
                except OCRUnavailableError as exc:
                    document.warnings.append(f"内嵌图片 OCR 失败：{exc}")
                    continue
                if result.text:
                    document.ocr_scores.append(result.mean_score)
                    builder.image(result.text, provenance=Provenance(), ocr_score=result.mean_score)
                else:
                    builder.image("[图片（未识别到文字）]", provenance=Provenance())

    @staticmethod
    def _docx_table_rows(table) -> Tuple[List[str], List[List[str]], List[str]]:
        merged: List[str] = []
        headers: List[str] = []
        rows: List[List[str]] = []
        for row_index, row in enumerate(table.rows):
            seen = set()
            values: List[str] = []
            for cell in row.cells:
                key = id(cell._tc)
                if key in seen:
                    continue
                seen.add(key)
                values.append((cell.text or "").strip())
            if row_index == 0:
                headers = values
            else:
                rows.append(values)
        return headers, rows, merged

    @staticmethod
    def _docx_image_blob(doc, shape) -> Optional[bytes]:
        blip = shape._inline.graphic.graphicData.pic.blipFill.blip
        r_id = blip.embed
        if not r_id:
            return None
        part = doc.part.related_parts.get(r_id)
        if part is None:
            return None
        blob = getattr(part, "blob", None)
        if not blob or len(blob) > 20 * 1024 * 1024:
            return None
        return blob

    # ── XLSX ──────────────────────────────────────────────────────────────

    def _parse_xlsx(self, source: Path, document: ParsedDocument, options: ParseOptions) -> None:
        try:
            import openpyxl
        except Exception as exc:  # noqa: BLE001
            raise ParseError("缺少 openpyxl，无法解析 Excel") from exc

        formula_wb = openpyxl.load_workbook(str(source), read_only=False, data_only=False)
        value_wb = openpyxl.load_workbook(str(source), read_only=False, data_only=True)
        builder = ElementBuilder(document)
        table_index = 0
        for sheet_index, ws in enumerate(formula_wb.worksheets):
            ws_values = value_wb.worksheets[sheet_index]
            builder.heading(ws.title or f"Sheet{sheet_index + 1}", level=1, provenance=Provenance())
            max_row = ws.max_row or 0
            max_col = ws.max_column or 0
            if max_row == 0 or max_col == 0:
                document.warnings.append(f"Sheet {ws.title} 为空")
                continue
            rows: List[List[str]] = []
            for row in ws.iter_rows(min_row=1, max_row=max_row, max_col=max_col):
                values: List[str] = []
                empty = True
                for cell in row:
                    value = self._xlsx_cell_text(cell, ws_values[cell.coordinate].value)
                    if value:
                        empty = False
                    values.append(value)
                if not empty:
                    rows.append(values)
            if not rows:
                continue
            headers = rows[0]
            data_rows = rows[1:]
            merged = [str(rng) for rng in ws.merged_cells.ranges]
            groups = [data_rows[i:i + 200] for i in range(0, len(data_rows), 200)] or [[]]
            row_offset = 2
            for group in groups:
                table_index += 1
                prov = Provenance(
                    sheet_name=ws.title, table_index=table_index,
                    row_start=row_offset, row_end=row_offset + max(0, len(group) - 1),
                )
                builder.table(
                    headers, group, provenance=prov, title=f"Sheet: {ws.title}",
                    start_row=row_offset, start_column=1, merged_cells=merged,
                )
                row_offset += len(group)
        formula_wb.close()
        value_wb.close()

    @staticmethod
    def _xlsx_cell_text(cell, cached_value) -> str:
        formula = cell.value
        if isinstance(formula, str) and formula.startswith("="):
            cached = cached_value
            if cached is None:
                return formula
            return f"{formula} => {NativeParser._cell_value_text(cached)}"
        return NativeParser._cell_value_text(formula)

    @staticmethod
    def _cell_value_text(value) -> str:
        if value is None:
            return ""
        if isinstance(value, dt.datetime):
            return value.isoformat(sep=" ", timespec="seconds")
        if isinstance(value, dt.date):
            return value.isoformat()
        if isinstance(value, float):
            text = f"{value:.10g}"
            return text
        return str(value).strip()

    # ── PPTX ──────────────────────────────────────────────────────────────

    def _parse_pptx(self, source: Path, document: ParsedDocument, options: ParseOptions) -> None:
        try:
            from pptx import Presentation
            from pptx.enum.shapes import MSO_SHAPE_TYPE
        except Exception as exc:  # noqa: BLE001
            raise ParseError("缺少 python-pptx，无法解析 PowerPoint") from exc

        prs = Presentation(str(source))
        builder = ElementBuilder(document)
        table_index = 0
        for slide_number, slide in enumerate(prs.slides, start=1):
            title = ""
            try:
                if slide.shapes.title is not None and slide.shapes.title.text:
                    title = slide.shapes.title.text.strip()
            except Exception:  # noqa: BLE001
                title = ""
            prov = Provenance(slide_number=slide_number)
            builder.heading(title or f"Slide {slide_number}", level=1, provenance=prov)
            for shape in slide.shapes:
                if shape.has_text_frame:
                    text = "\n".join(
                        p.text.strip() for p in shape.text_frame.paragraphs if p.text and p.text.strip()
                    ).strip()
                    if text and text != title:
                        builder.paragraph(text, provenance=prov)
                if getattr(shape, "has_table", False) and shape.has_table:
                    table_index += 1
                    headers, rows = self._pptx_table_rows(shape.table)
                    builder.table(
                        headers, rows, provenance=Provenance(slide_number=slide_number,
                                                             table_index=table_index),
                        title=f"Slide {slide_number} 表格",
                    )
                if shape.shape_type == MSO_SHAPE_TYPE.PICTURE:
                    ocr_text = ""
                    if options.ocr_enabled and ocr_available():
                        try:
                            result = ocr_image_bytes(shape.image.blob)
                            ocr_text = result.text
                            if result.text:
                                document.ocr_scores.append(result.mean_score)
                        except OCRUnavailableError as exc:
                            document.warnings.append(f"Slide {slide_number} 图片 OCR 失败：{exc}")
                    builder.image(
                        ocr_text or "[图片（未识别到文字）]",
                        provenance=Provenance(slide_number=slide_number),
                    )
                if getattr(shape, "has_chart", False) and shape.has_chart:
                    try:
                        chart = shape.chart
                        categories = [str(c) for c in chart.plots[0].categories]
                        series_texts = []
                        for series in chart.series:
                            values = [str(v) for v in series.values]
                            series_texts.append(f"{series.name}: " + ", ".join(values))
                        builder.paragraph(
                            "图表数据：\n" + "\n".join(
                                [f"分类: {', '.join(categories)}"] + series_texts
                            ),
                            provenance=prov,
                        )
                    except Exception:  # noqa: BLE001
                        document.warnings.append(f"Slide {slide_number} 图表数据提取失败")
            try:
                notes = slide.notes_slide.notes_text_frame.text.strip()
            except Exception:  # noqa: BLE001
                notes = ""
            if notes:
                builder.paragraph(notes, provenance=prov, meta={"notes": True})

    @staticmethod
    def _pptx_table_rows(table) -> Tuple[List[str], List[List[str]]]:
        headers: List[str] = []
        rows: List[List[str]] = []
        for row_index, row in enumerate(table.rows):
            values = [(cell.text or "").strip() for cell in row.cells]
            if row_index == 0:
                headers = values
            else:
                rows.append(values)
        return headers, rows

    # ── TXT / Markdown ────────────────────────────────────────────────────

    def _parse_text(self, source: Path, document: ParsedDocument,
                    options: ParseOptions, *, markdown: bool) -> None:
        text = read_text_file(source)
        builder = ElementBuilder(document)
        if not markdown:
            self._emit_pdf_blocks(builder, text, Provenance())
            return
        lines = text.splitlines()
        index = 0
        paragraph: List[str] = []
        list_buffer: List[str] = []

        def flush_paragraph() -> None:
            nonlocal paragraph
            content = "\n".join(paragraph).strip()
            paragraph = []
            if content:
                builder.paragraph(content)

        def flush_list() -> None:
            nonlocal list_buffer
            if list_buffer:
                builder.list_item("\n".join(list_buffer))
                list_buffer = []

        while index < len(lines):
            line = lines[index]
            fence = _MD_FENCE_RE.match(line.strip())
            if fence:
                flush_paragraph()
                flush_list()
                code_lines: List[str] = []
                info = line.strip()[3:].strip()
                index += 1
                while index < len(lines) and not _MD_FENCE_RE.match(lines[index].strip()):
                    code_lines.append(lines[index])
                    index += 1
                index += 1
                builder.code("\n".join(code_lines).strip(), meta={"language": info})
                continue
            heading = _MD_HEADING_RE.match(line)
            if heading:
                flush_paragraph()
                flush_list()
                builder.heading(heading.group(2).strip(), level=len(heading.group(1)))
                index += 1
                continue
            if line.strip().startswith("|") and "|" in line.strip()[1:]:
                flush_paragraph()
                flush_list()
                table_lines = []
                while index < len(lines) and lines[index].strip().startswith("|"):
                    table_lines.append(lines[index].strip())
                    index += 1
                self._emit_markdown_table(builder, table_lines)
                continue
            if _MD_LIST_RE.match(line):
                flush_paragraph()
                list_buffer.append(_MD_LIST_RE.sub("", line).strip())
                index += 1
                continue
            if not line.strip():
                flush_paragraph()
                flush_list()
            else:
                flush_list()
                paragraph.append(line.rstrip())
            index += 1
        flush_paragraph()
        flush_list()

    @staticmethod
    def _emit_markdown_table(builder: ElementBuilder, lines: List[str]) -> None:
        rows = []
        for line in lines:
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if all(re.fullmatch(r":?-{2,}:?", c) for c in cells if c):
                continue
            rows.append(cells)
        if not rows:
            return
        builder.table(rows[0], rows[1:], provenance=Provenance(), merged_cells=[])

    # ── CSV ───────────────────────────────────────────────────────────────

    def _parse_csv(self, source: Path, document: ParsedDocument, options: ParseOptions) -> None:
        encoding = detect_encoding(source)
        with open(source, "r", encoding=encoding, errors="strict", newline="") as fh:
            sample = fh.read(16 * 1024)
            fh.seek(0)
            try:
                dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
            except csv.Error:
                dialect = csv.excel
            reader = csv.reader(fh, dialect)
            rows = [row for row in reader if any(str(c).strip() for c in row)]
        if not rows:
            raise ParseError("CSV 没有有效数据行", code="empty_extraction")
        builder = ElementBuilder(document)
        builder.heading(Path(document.source_name).stem or "CSV", level=1)
        headers = [str(c).strip() for c in rows[0]]
        data_rows = [[str(c).strip() for c in row] for row in rows[1:]]
        groups = [data_rows[i:i + 200] for i in range(0, len(data_rows), 200)] or [[]]
        row_offset = 2
        for group in groups:
            builder.table(
                headers, group, provenance=Provenance(row_start=row_offset,
                                                      row_end=row_offset + max(0, len(group) - 1)),
                start_row=row_offset, start_column=1,
            )
            row_offset += len(group)
        document.warnings.append(f"CSV 编码识别为 {encoding}，分隔符 {getattr(dialect, 'delimiter', ',')}")

    # ── HTML ──────────────────────────────────────────────────────────────

    def _parse_html(self, source: Path, document: ParsedDocument, options: ParseOptions) -> None:
        try:
            from bs4 import BeautifulSoup
        except Exception as exc:  # noqa: BLE001
            raise ParseError("缺少 beautifulsoup4，无法解析 HTML") from exc
        html_text = read_text_file(source)
        soup = BeautifulSoup(html_text, "html.parser")
        for tag in soup(["script", "style", "nav", "header", "footer", "noscript", "form", "svg"]):
            tag.decompose()
        title = (soup.title.get_text(strip=True) if soup.title else "") or Path(document.source_name).stem
        document.title = title
        builder = ElementBuilder(document)
        builder.heading(title, level=1)
        body = soup.body or soup

        def walk(node) -> None:
            for child in node.children:
                name = getattr(child, "name", None)
                if not name:
                    continue
                if name in {"h1", "h2", "h3", "h4", "h5", "h6"}:
                    text = child.get_text(" ", strip=True)
                    if text:
                        builder.heading(text, level=int(name[1]))
                    continue
                if name == "p":
                    text = child.get_text(" ", strip=True)
                    if text:
                        builder.paragraph(text)
                    continue
                if name == "li":
                    text = child.get_text(" ", strip=True)
                    if text:
                        builder.list_item(text)
                    continue
                if name == "pre":
                    text = child.get_text("\n", strip=True)
                    if text:
                        builder.code(text)
                    continue
                if name == "table":
                    rows = []
                    for tr in child.find_all("tr"):
                        cells = [td.get_text(" ", strip=True) for td in tr.find_all(["th", "td"])]
                        if any(cells):
                            rows.append(cells)
                    if rows:
                        builder.table(rows[0], rows[1:], provenance=Provenance())
                    continue
                walk(child)

        walk(body)
        if len(document.elements) <= 1:
            # 纯文本 / 非标准结构：回退为整体正文（仍按段落拆分）
            fallback = soup.get_text("\n", strip=True)
            for line in fallback.splitlines():
                text = line.strip()
                if len(text) > 1 and text != title:
                    builder.paragraph(text)

    # ── 图片 OCR ──────────────────────────────────────────────────────────

    def _parse_image(self, source: Path, document: ParsedDocument, options: ParseOptions) -> None:
        if not options.ocr_enabled:
            raise OCRUnavailableError("图片知识需要 OCR，当前 KNOWLEDGE_OCR_ENABLED=false")
        if not ocr_available():
            raise OCRUnavailableError("图片知识需要 OCR，但当前环境未安装 rapidocr-onnxruntime")
        result: OCRResult = ocr_image_bytes(source.read_bytes())
        if not result.text.strip():
            raise ParseError("图片 OCR 未识别到文字", code="empty_extraction")
        document.ocr_pages.append(1)
        document.ocr_scores.append(result.mean_score)
        builder = ElementBuilder(document)
        builder.heading(Path(document.source_name).stem, level=1)
        for line in result.lines:
            builder.paragraph(line, meta={"ocr": True})
        if result.low_quality:
            document.warnings.append(
                f"图片 OCR 平均置信度较低（{result.mean_score:.2f}），结果标记 needs_review"
            )

    # ── ODF / RTF ─────────────────────────────────────────────────────────

    def _parse_odf(self, source: Path, document: ParsedDocument, options: ParseOptions) -> None:
        ns = {
            "office": "urn:oasis:names:tc:opendocument:xmlns:office:1.0",
            "text": "urn:oasis:names:tc:opendocument:xmlns:text:1.0",
            "table": "urn:oasis:names:tc:opendocument:xmlns:table:1.0",
        }
        with zipfile.ZipFile(source) as zf:
            content = zf.read("content.xml")
        root = ElementTree.fromstring(content)
        builder = ElementBuilder(document)
        builder.heading(Path(document.source_name).stem, level=1)

        def text_of(node) -> str:
            return "".join(node.itertext()).strip()

        body = root.find(".//office:text", ns) or root.find(".//office:body", ns) or root
        for child in list(body):
            tag = child.tag.split("}")[-1]
            if tag == "h":
                level = int(child.attrib.get(f"{{{ns['text']}}}outline-level", "1") or 1)
                builder.heading(text_of(child), level=level)
            elif tag == "p":
                if text_of(child):
                    builder.paragraph(text_of(child))
            elif tag == "list":
                for item in child:
                    if text_of(item):
                        builder.list_item(text_of(item))
            elif tag == "table":
                rows: List[List[str]] = []
                for row in child.findall(".//table:table-row", ns):
                    cells = []
                    for cell in row.findall("table:table-cell", ns):
                        cells.append(text_of(cell))
                        repeat = int(cell.attrib.get(f"{{{ns['table']}}}number-columns-repeated", "1") or 1)
                        if repeat > 1:
                            cells.extend([""] * min(repeat - 1, 50))
                    if any(c.strip() for c in cells):
                        rows.append(cells)
                if rows:
                    builder.table(rows[0], rows[1:], provenance=Provenance())

    def _parse_rtf(self, source: Path, document: ParsedDocument, options: ParseOptions) -> None:
        raw = source.read_bytes().decode("latin-1", errors="ignore")
        for group in ("fonttbl", "colortbl", "stylesheet", "info", "pict", "object"):
            raw = re.sub(r"\{\\\*?\\?" + group + r"\b[\s\S]*?\}", " ", raw)
        raw = re.sub(r"\\'[0-9a-fA-F]{2}", " ", raw)
        raw = re.sub(r"\\u(-?\d+)\??", lambda m: chr(int(m.group(1)) % 65536), raw)
        raw = re.sub(r"\\par[d]?\b", "\n", raw)
        raw = re.sub(r"\\[a-zA-Z]+-?\d* ?", "", raw)
        raw = raw.replace("{", "").replace("}", "")
        builder = ElementBuilder(document)
        builder.heading(Path(document.source_name).stem, level=1)
        for block in re.split(r"\n\s*\n", raw):
            text = "\n".join(line.strip() for line in block.splitlines() if line.strip())
            if text:
                builder.paragraph(text)

    # ── 旧版二进制 Office 转换 ────────────────────────────────────────────

    @staticmethod
    def _convert_legacy(source: Path, ext: str) -> Path:
        soffice = shutil.which("soffice") or shutil.which("libreoffice")
        if not soffice:
            raise ParseError(
                f"旧版 .{ext} 需要 LibreOffice 转换；当前环境未安装 soffice，"
                "请安装 libreoffice 或先将文件另存为新格式",
                code="legacy_conversion_unavailable",
            )
        target = {"doc": "docx", "xls": "xlsx", "ppt": "pptx"}[ext]
        temp_dir = Path(tempfile.mkdtemp(prefix="kb-convert-"))
        try:
            subprocess.run(
                [soffice, "--headless", "--norestore", "--convert-to", target,
                 "--outdir", str(temp_dir), str(source)],
                check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=180,
            )
        except subprocess.TimeoutExpired as exc:
            shutil.rmtree(temp_dir, ignore_errors=True)
            raise ParseError(f"LibreOffice 转换超时（. {ext}）", code="legacy_conversion_timeout") from exc
        except subprocess.CalledProcessError as exc:
            shutil.rmtree(temp_dir, ignore_errors=True)
            raise ParseError(
                f"LibreOffice 转换失败（.{ext}）：{exc.stderr.decode('utf-8', 'ignore')[:300]}",
                code="legacy_conversion_failed",
            ) from exc
        converted = next(temp_dir.glob(f"*.{target}"), None)
        if converted is None:
            shutil.rmtree(temp_dir, ignore_errors=True)
            raise ParseError(f"LibreOffice 未生成 .{target} 文件", code="legacy_conversion_failed")
        return converted
