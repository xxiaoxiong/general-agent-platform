"""有界解析参考资料。解析结果是待处理内容，不能触发宏或外部链接。"""

from __future__ import annotations

import csv
import io
import re
import stat
from collections import Counter
from pathlib import Path, PurePosixPath
from zipfile import ZIP_DEFLATED, ZIP_STORED, BadZipFile, ZipFile

from .schemas import DraftSection, ListBlock, ParagraphBlock, ParsedDocument, TableBlock


_MAX_FILE_BYTES = 25 * 1024**2
_MAX_EXPANDED_BYTES = 90 * 1024**2
_MAX_MEMBER_BYTES = 32 * 1024**2
_MAX_ARCHIVE_MEMBERS = 2048
_MAX_CONTENT_CHARS = 320_000
_MAX_SECTIONS = 60
_MAX_BLOCKS = 300
_MAX_ROWS = 300
_MAX_COLUMNS = 30
_MAX_PAGES = 120
_INVALID_XML = re.compile(r"[^\t\n\r\x20-\ud7ff\ue000-\ufffd\U00010000-\U0010ffff]")
_ANCHOR = r"([A-Za-z0-9][A-Za-z0-9_-]{0,31})"
_MIME_TYPES = {
    ".docx": {"application/vnd.openxmlformats-officedocument.wordprocessingml.document", "application/zip"},
    ".pptx": {"application/vnd.openxmlformats-officedocument.presentationml.presentation", "application/zip"},
    ".xlsx": {"application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "application/zip"},
    ".pdf": {"application/pdf", "application/x-pdf"},
    ".txt": {"text/plain"},
    ".md": {"text/plain", "text/markdown"},
    ".markdown": {"text/plain", "text/markdown"},
    ".csv": {"text/csv", "text/plain", "application/vnd.ms-excel"},
}
_MAIN_PARTS = {".docx": "word/document.xml", ".pptx": "ppt/presentation.xml", ".xlsx": "xl/workbook.xml"}


class DocumentParseError(ValueError):
    """可以向用户展示的解析失败原因。"""


def reference_file_supported(path: str | Path) -> bool:
    return Path(path).suffix.lower() in _MIME_TYPES


def _guard_xml(archive: ZipFile, item) -> None:
    # XLSX 的工作表可能走流式 XML 实现。Office 文档不需要 DTD。
    with archive.open(item) as member:
        tail = b""
        while chunk := member.read(64 * 1024):
            data = tail + chunk.replace(b"\x00", b"")
            if b"<!DOCTYPE" in data:
                raise DocumentParseError("Office 文件包含不受支持的 XML 文档类型声明")
            tail = data[-8:]


def _guard_zip(path: Path, main_part: str) -> None:
    try:
        with ZipFile(path) as archive:
            entries = archive.infolist()
            if len(entries) > _MAX_ARCHIVE_MEMBERS:
                raise DocumentParseError("Office 文件包含过多项目")
            names: set[str] = set()
            total = 0
            for item in entries:
                name = item.filename
                relative = PurePosixPath(name)
                if item.compress_type not in {ZIP_STORED, ZIP_DEFLATED}:
                    raise DocumentParseError("Office 文件使用了不受支持的压缩方式")
                if (
                    relative.is_absolute() or ".." in relative.parts or "\\" in name or ":" in name
                    or stat.S_ISLNK(item.external_attr >> 16) or item.flag_bits & 1
                ):
                    raise DocumentParseError("Office 文件包含非法路径、链接或加密内容")
                canonical = relative.as_posix().lower()
                if canonical in names:
                    raise DocumentParseError("Office 文件包含重复项目")
                names.add(canonical)
                if canonical.endswith(("/vbaproject.bin", "/vbadata.xml")) or "/activex/" in canonical:
                    raise DocumentParseError("此处仅支持无宏的 Office 文件")
                total += item.file_size
                if total > _MAX_EXPANDED_BYTES or item.file_size > _MAX_MEMBER_BYTES:
                    raise DocumentParseError("Office 文件解压大小超限")
                if item.file_size > 2 * 1024**2 and item.file_size > max(item.compress_size, 1) * 400:
                    raise DocumentParseError("Office 文件压缩比异常")
            if "[content_types].xml" not in names or main_part not in names:
                raise DocumentParseError("文件内部结构与声明的 Office 格式不符")
            for item in entries:
                if item.filename.lower().endswith((".xml", ".rels")):
                    _guard_xml(archive, item)
    except (BadZipFile, OSError, NotImplementedError, RuntimeError) as exc:
        raise DocumentParseError("无法打开 Office 文件") from exc


def validate_reference_file(path: str | Path, mime_type: str | None = None) -> Path:
    """在解析前检查 MIME、后缀、文件签名、大小和 Office 容器边界。"""
    file = Path(path)
    extension = file.suffix.lower()
    if extension not in _MIME_TYPES:
        raise DocumentParseError("此处支持 DOCX、PDF、PPTX、XLSX、TXT、Markdown 和 CSV")
    media = (mime_type or "").split(";", 1)[0].strip().lower()
    if media and media != "application/octet-stream" and media not in _MIME_TYPES[extension]:
        raise DocumentParseError("附件类型与文件扩展名不符")
    try:
        if file.is_symlink() or not file.is_file():
            raise DocumentParseError("参考文件不存在或不是普通文件")
        if not 0 < file.stat().st_size <= _MAX_FILE_BYTES:
            raise DocumentParseError("参考文件为空或超过解析大小上限")
        with file.open("rb") as handle:
            signature = handle.read(1024)
    except OSError as exc:
        raise DocumentParseError("无法读取参考文件") from exc
    if extension in _MAIN_PARTS:
        if not signature.startswith(b"PK\x03\x04"):
            raise DocumentParseError("文件内容不是有效的 Office 压缩包")
        _guard_zip(file, _MAIN_PARTS[extension])
    elif extension == ".pdf":
        if b"%PDF-" not in signature:
            raise DocumentParseError("文件内容不是 PDF")
    elif b"\x00" in signature:
        raise DocumentParseError("文本附件包含二进制内容")
    return file


def _text(value: object, limit: int = 12_000) -> str:
    return _INVALID_XML.sub("", str(value)).replace("\r\n", "\n").replace("\r", "\n").strip()[:limit]


def _title(value: object, default: str = "未命名参考资料") -> str:
    return _text(value).replace("\n", " ")[:180].strip() or default


class _Content:
    """限制输出规模，方便后续把参考资料作为有界数据传给模型。"""

    def __init__(self, title: str, kind: str) -> None:
        self.document = ParsedDocument(title=title, metadata={"source_format": kind})
        self.remaining = _MAX_CONTENT_CHARS
        self.truncated = False

    def value(self, value: object, limit: int = 12_000) -> str:
        result = _text(value, self.remaining + 1)
        clipped = result[:min(limit, self.remaining)].strip()
        self.truncated |= len(result) > len(clipped)
        self.remaining -= len(clipped)
        return clipped

    def add_section(self, title: str, level: int = 1, anchor: str | None = None) -> DraftSection | None:
        sections = self.document.sections
        if len(sections) == _MAX_SECTIONS:
            self.truncated = True
            return None
        order = len(sections) + 1
        used = {section.id for section in sections}
        if not anchor or anchor in used:
            anchor = f"section_{order}"
            while anchor in used:
                anchor += "_"
        section = DraftSection(id=anchor, title=title, order=order, level=level)
        sections.append(section)
        return section

    def append(self, section: DraftSection, block) -> None:
        if len(section.blocks) == _MAX_BLOCKS:
            self.truncated = True
        else:
            section.blocks.append(block)

    def table(self, values, *, header: bool, anchor: str | None = None, width: int | None = None):
        rows = []
        for index, row in enumerate(values):
            if index >= _MAX_ROWS or self.remaining <= 0:
                self.truncated = True
                break
            self.truncated |= len(row) > _MAX_COLUMNS
            cells = [self.value(cell, 2_000) if cell is not None else "" for cell in row[:_MAX_COLUMNS]]
            if width is not None:
                cells = (cells + [""] * width)[:width]
            rows.append(cells)
        width = max(map(len, rows), default=0)
        if not width:
            return None
        return TableBlock(id=anchor, rows=[row + [""] * (width - len(row)) for row in rows], header=header)

    def finish(self) -> ParsedDocument:
        if self.truncated:
            self.document.warnings.append("参考资料超出结构或文本上限，解析结果已截取")
        for index, section in enumerate(self.document.sections):
            self.document.sections[index] = DraftSection.model_validate(section.model_dump())
        return self.document


def _bookmark(element, expression: str) -> str | None:
    from docx.oxml.ns import qn

    for bookmark in element.iter(qn("w:bookmarkStart")):
        match = re.fullmatch(expression, bookmark.get(qn("w:name"), ""))
        if match:
            return match.group(1)
    return None


def _heading_level(paragraph) -> int | None:
    from docx.oxml.ns import qn

    style = paragraph.style
    for value in (style.style_id, style.name or ""):
        match = re.fullmatch(r"(?:heading|标题)\s*([1-9])", value or "", re.IGNORECASE)
        if match:
            return min(3, int(match.group(1)))
    outline = paragraph._p.find("./" + qn("w:pPr") + "/" + qn("w:outlineLvl"))
    value = outline.get(qn("w:val"), "") if outline is not None else ""
    return min(3, int(value) + 1) if value in {str(n) for n in range(9)} else None


def _number_formats(document) -> dict[tuple[str, str], bool]:
    from docx.oxml.ns import qn

    try:
        numbering = document.part.numbering_part.element
    except (NotImplementedError, KeyError):
        return {}
    formats: dict[str, dict[str, bool]] = {}
    for definition in numbering.findall(qn("w:abstractNum")):
        levels = {}
        for level in definition.findall(qn("w:lvl")):
            num_format = level.find(qn("w:numFmt"))
            if num_format is not None:
                value = num_format.get(qn("w:val"), "")
                if value not in {"none", ""}:
                    levels[level.get(qn("w:ilvl"), "0")] = value != "bullet"
        formats[definition.get(qn("w:abstractNumId"))] = levels
    result = {}
    for number in numbering.findall(qn("w:num")):
        abstract = number.find(qn("w:abstractNumId"))
        if abstract is not None:
            result.update({
                (number.get(qn("w:numId")), level): ordered
                for level, ordered in formats.get(abstract.get(qn("w:val")), {}).items()
            })
    return result


def _list_type(paragraph, formats) -> bool | None:
    props = paragraph._p.pPr
    numbering = props.numPr if props is not None else None
    if numbering is not None and numbering.numId is not None:
        number_id = str(numbering.numId.val)
        if number_id == "0":
            return None
        level = str(numbering.ilvl.val) if numbering.ilvl is not None else "0"
        if (number_id, level) in formats:
            return formats[number_id, level]
    style = f"{paragraph.style.style_id} {paragraph.style.name}".lower()
    if "listbullet" in style or "list bullet" in style or "项目符号" in style:
        return False
    if "listnumber" in style or "list number" in style or "编号列表" in style:
        return True
    return None


def _parse_word(path: Path) -> ParsedDocument:
    from docx import Document
    from docx.oxml.ns import qn
    from docx.table import Table

    document = Document(path)
    props = document.core_properties
    content = _Content(_title(props.title or path.stem), "docx")
    for key in ("author", "subject", "created", "modified", "language", "keywords"):
        value = getattr(props, key, None)
        if value:
            content.document.metadata[key] = _text(value, 500)
    styles: Counter[str] = Counter()
    section = None
    formats = _number_formats(document)
    for node in document.iter_inner_content():
        if content.remaining <= 0:
            content.truncated = True
            break
        if isinstance(node, Table):
            section = section or content.add_section("正文")
            if section is None:
                break
            first_row = node._tbl.find(qn("w:tr"))
            header = first_row is not None and first_row.find(
                "./" + qn("w:trPr") + "/" + qn("w:tblHeader")
            ) is not None
            table = content.table(
                ([cell.text for cell in row.cells[:_MAX_COLUMNS]] for row in node.rows),
                header=header,
                anchor=_bookmark(node._tbl, rf"di_b\d{{1,2}}_{_ANCHOR}"),
                width=min(len(node.columns), _MAX_COLUMNS),
            )
            content.truncated |= len(node.columns) > _MAX_COLUMNS
            if table:
                content.append(section, table)
            continue

        text = content.value(node.text)
        if not text:
            continue
        style_id = (node.style.style_id or "").lower()
        styles[_text(node.style.name or style_id, 100)] += 1
        if style_id in {"diauxiliaryheading", "diauxiliarybody"}:
            field_id = _bookmark(node._p, rf"di_m_{_ANCHOR}")
            if field_id and field_id not in content.document.missing_fields:
                content.document.missing_fields.append(field_id)
            continue
        if section is None and style_id in {"title", "标题"}:
            content.document.title = _title(text)
            continue
        if section is None and style_id in {"subtitle", "副标题"}:
            content.document.subtitle = text[:500]
            continue
        if section is None and style_id == "diabstract":
            content.document.abstract = text
            continue
        level = _heading_level(node)
        if level:
            section = content.add_section(_title(text), level, _bookmark(node._p, rf"di_s_{_ANCHOR}"))
            if section is None:
                break
            continue
        section = section or content.add_section("正文")
        if section is None:
            break
        anchor = _bookmark(node._p, rf"di_b\d{{1,2}}_{_ANCHOR}")
        ordered = _list_type(node, formats)
        if ordered is not None:
            last = section.blocks[-1] if section.blocks else None
            if anchor is None and isinstance(last, ListBlock) and last.ordered == ordered and len(last.items) < 150:
                last.items.append(text)
            else:
                content.append(section, ListBlock(id=anchor, items=[text], ordered=ordered))
        else:
            style = "quote" if "quote" in style_id else "caption" if "caption" in style_id else "normal"
            content.append(section, ParagraphBlock(id=anchor, text=text, style=style))
    content.document.styles = {name: str(total) for name, total in styles.most_common(40) if name}
    if not content.document.sections:
        content.document.warnings.append("Word 文件中没有可提取的正文结构")
    return content.finish()


def _parse_pdf(path: Path) -> ParsedDocument:
    from pypdf import PdfReader

    content = _Content(_title(path.stem), "pdf")
    with path.open("rb") as stream:
        reader = PdfReader(stream, strict=False)
        if reader.is_encrypted:
            raise DocumentParseError("无法解析加密 PDF，请上传已解密的版本")
        if reader.metadata and reader.metadata.title:
            content.document.title = _title(reader.metadata.title)
        content.document.metadata["pages"] = str(len(reader.pages))
        for index in range(min(len(reader.pages), _MAX_PAGES)):
            text = content.value(reader.pages[index].extract_text() or "")
            if text:
                section = content.add_section(f"第 {index + 1} 页")
                if section is None:
                    break
                content.append(section, ParagraphBlock(text=text))
            if content.remaining <= 0:
                content.truncated = True
                break
        content.truncated |= len(reader.pages) > _MAX_PAGES
    if not content.document.sections:
        content.document.warnings.append("PDF 无可提取文字，可能为扫描件，请提供 OCR 文本或原始文档")
    return content.finish()


def _parse_slides(path: Path) -> ParsedDocument:
    from pptx import Presentation

    deck = Presentation(path)
    content = _Content(_title(deck.core_properties.title or path.stem), "pptx")
    content.document.metadata["slides"] = str(len(deck.slides))
    for index, slide in enumerate(deck.slides, 1):
        if content.remaining <= 0:
            content.truncated = True
            break
        title = slide.shapes.title
        section = content.add_section(_title(title.text if title is not None else "", f"第 {index} 张幻灯片"))
        if section is None:
            break
        for shape_index, shape in enumerate(slide.shapes):
            if shape_index >= _MAX_BLOCKS or content.remaining <= 0:
                content.truncated = True
                break
            if title is not None and shape.shape_id == title.shape_id:
                continue
            if shape.has_table:
                table = content.table(
                    ([cell.text for cell in row.cells] for row in shape.table.rows), header=True
                )
                if table:
                    content.append(section, table)
            elif shape.has_text_frame:
                for paragraph in shape.text_frame.paragraphs:
                    text = content.value(paragraph.text)
                    if text:
                        content.append(section, ParagraphBlock(text=text))
    return content.finish()


def _parse_sheets(path: Path) -> ParsedDocument:
    from openpyxl import load_workbook

    workbook = load_workbook(path, read_only=True, data_only=True, keep_links=False)
    content = _Content(_title(workbook.properties.title or path.stem), "xlsx")
    try:
        for sheet in workbook.worksheets:
            section = content.add_section(_title(sheet.title))
            if section is None or content.remaining <= 0:
                content.truncated = True
                break
            content.truncated |= (sheet.max_row or 0) > _MAX_ROWS or (sheet.max_column or 0) > _MAX_COLUMNS
            rows = sheet.iter_rows(
                max_row=min(sheet.max_row or _MAX_ROWS, _MAX_ROWS),
                max_col=min(sheet.max_column or _MAX_COLUMNS, _MAX_COLUMNS),
                values_only=True,
            )
            table = content.table((row for row in rows if any(cell is not None for cell in row)), header=True)
            if table:
                content.append(section, table)
    finally:
        workbook.close()
    if not any(section.blocks for section in content.document.sections):
        content.document.warnings.append("工作簿没有可提取的单元格值，公式需要已保存的计算结果")
    return content.finish()


def _parse_text(path: Path) -> ParsedDocument:
    content = _Content(_title(path.stem), path.suffix.lstrip(".").lower())
    with path.open("rb") as stream:
        raw = stream.read(_MAX_CONTENT_CHARS * 4 + 1)
    content.truncated = len(raw) > _MAX_CONTENT_CHARS * 4
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            text = raw.decode(encoding, errors="ignore" if content.truncated else "strict")
            break
        except UnicodeDecodeError:
            continue
    else:
        raise DocumentParseError("无法读取文本编码，请保存为 UTF-8 后重新上传")
    if path.suffix.lower() == ".csv":
        table = content.table(csv.reader(io.StringIO(text)), header=True)
        if table:
            section = content.add_section("表格")
            content.append(section, table)
    else:
        section = None
        for line in text.splitlines():
            if not line.strip():
                continue
            if content.remaining <= 0:
                content.truncated = True
                break
            heading = re.fullmatch(r"(#{1,6})\s+(.+)", line.strip()) if path.suffix.lower() in {".md", ".markdown"} else None
            if heading:
                section = content.add_section(_title(content.value(heading.group(2))), min(3, len(heading.group(1))))
                if section is None:
                    break
            else:
                section = section or content.add_section("正文")
                value = content.value(line)
                if value:
                    content.append(section, ParagraphBlock(text=value))
    return content.finish()


def parse_conversation_draft(markdown: str) -> ParsedDocument:
    """把对话中已成形的 Markdown 稿件装入正文、章节、列表和表格。"""
    lines = markdown.splitlines()
    heading = re.compile(r"^(#{1,6})\s+(.+)$")
    list_item = re.compile(r"^\s*(?:(\d+)[.)]|[-+*])\s+(.+)$")
    table_separator = re.compile(r"^\s*\|?\s*:?-+:?\s*(?:\|\s*:?-+:?\s*)+\|?\s*$")

    def plain(value: str) -> str:
        value = re.sub(r"\[([^\]]+)\]\(#[^)]*\)", r"\1", value)
        value = re.sub(r"\[([^\]]+)\]\((https?://[^)]*)\)", r"\1 (\2)", value)
        # 其余链接（如 subagent: 内部引用）只保留标题文本，避免 Markdown 语法残留在 Word 正文
        value = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", value)
        value = re.sub(r"(\*\*|__|\x60)(.+?)\1", r"\2", value)
        return value.strip()

    first = next((i for i, line in enumerate(lines) if heading.fullmatch(line.strip())), None)
    if first is None:
        raise DocumentParseError("之前的对话没有可直接导出的文稿标题")
    match = heading.fullmatch(lines[first].strip())
    content = _Content(_title(plain(match[2])), "conversation")
    level_offset = len(match[1])
    index, level, section = first + 1, 0, None
    while index < len(lines):
        line = lines[index].strip()
        index += 1
        if not line or re.fullmatch(r"[-*_]{3,}", line):
            continue
        match = heading.fullmatch(line)
        if match:
            wanted = max(1, min(3, len(match[1]) - level_offset))
            level = min(wanted, level + 1)
            section = content.add_section(_title(plain(match[2])), level)
            if section is None:
                break
            continue
        if section is None:
            section = content.add_section("正文")
            level = 1
        if line.startswith(("```", "~~~")):
            code = []
            while index < len(lines) and not lines[index].strip().startswith(line[:3]):
                code.append(lines[index])
                index += 1
            index += 1
            if value := content.value("\n".join(code).strip()):
                content.append(section, ParagraphBlock(text=value, style="quote"))
        elif "|" in line and index < len(lines) and table_separator.fullmatch(lines[index]):
            rows = [[plain(cell) for cell in line.strip("|").split("|")]]
            index += 1
            while index < len(lines) and "|" in lines[index]:
                rows.append([plain(cell) for cell in lines[index].strip().strip("|").split("|")])
                index += 1
            if block := content.table(rows, header=True):
                content.append(section, block)
        elif item := list_item.match(line):
            ordered = bool(item[1])
            items = [content.value(plain(item[2]))]
            while index < len(lines) and (item := list_item.match(lines[index])) and bool(item[1]) == ordered:
                items.append(content.value(plain(item[2])))
                index += 1
            items = [value for value in items if value]
            for offset in range(0, len(items), 150):
                content.append(section, ListBlock(items=items[offset:offset + 150], ordered=ordered))
        elif value := content.value(plain(line)):
            content.append(section, ParagraphBlock(text=value))
    result = content.finish()
    if content.truncated or not any(section.blocks for section in result.sections):
        raise DocumentParseError("对话文稿没有可导出正文，或超出一次导出的文档容量")
    return result


_PARSERS = {".docx": _parse_word, ".pdf": _parse_pdf, ".pptx": _parse_slides, ".xlsx": _parse_sheets}


def parse_document(path: str | Path, mime_type: str | None = None) -> ParsedDocument:
    file = validate_reference_file(path, mime_type)
    try:
        return _PARSERS.get(file.suffix.lower(), _parse_text)(file)
    except DocumentParseError:
        raise
    except Exception as exc:
        raise DocumentParseError("参考文件损坏或内容无法解析，请检查格式后重新上传") from exc


def parse_docx(path: str | Path) -> ParsedDocument:
    file = Path(path)
    if file.suffix.lower() != ".docx":
        raise DocumentParseError("目标文件不是 DOCX")
    return parse_document(file)
