"""将已校验的结构化草稿写入真实 Word 文件。"""

from __future__ import annotations

import re
from itertools import count
from pathlib import Path

from docx import Document
from docx.enum.style import WD_STYLE_TYPE
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm, Pt, RGBColor

from .schemas import DocumentDraft, ListBlock, ParagraphBlock, ProfileDefinition, ProfileFormatting


_RESERVED_NAMES = re.compile(r"^(?:CON|PRN|AUX|NUL|COM[0-9¹²³]|LPT[0-9¹²³])(?:\.|$)", re.IGNORECASE)


def safe_docx_filename(title: str) -> str:
    """生成下载文件名。实际存储路径不采用此名称。"""
    stem = re.sub(r'[\x00-\x1f\x7f<>:"/\\|?*]', "_", title).strip(" .")
    if stem.lower().endswith(".docx"):
        stem = stem[:-5].rstrip(" .")
    stem = stem[:96].rstrip(" .") or "文档"
    if _RESERVED_NAMES.match(stem):
        stem = "_" + stem
    return stem + ".docx"


def _word_element(tag: str, **attributes):
    element = OxmlElement("w:" + tag)
    for key, value in attributes.items():
        element.set(qn("w:" + key), str(value))
    return element


def _font(style, name: str, size: float, color: str, *, bold: bool = False) -> None:
    style.font.name = name
    style.font.size = Pt(size)
    style.font.color.rgb = RGBColor.from_string(color)
    style.font.bold = bold
    fonts = style.element.get_or_add_rPr().rFonts
    for attribute in ("asciiTheme", "hAnsiTheme", "eastAsiaTheme", "cstheme"):
        fonts.attrib.pop(qn("w:" + attribute), None)
    fonts.set(qn("w:eastAsia"), name)


def _define_styles(document, formatting: ProfileFormatting) -> None:
    styles = document.styles
    family = formatting.font_family
    normal = styles["Normal"]
    _font(normal, family, formatting.body_font_size, "20242B")
    normal.paragraph_format.line_spacing = formatting.line_spacing
    normal.paragraph_format.space_after = Pt(7)
    normal.paragraph_format.widow_control = True
    _font(styles["Title"], family, 26, "171922", bold=True)
    title_props = styles["Title"].element.get_or_add_pPr()
    border = title_props.find(qn("w:pBdr"))
    if border is not None:
        title_props.remove(border)
    styles["Title"].paragraph_format.space_after = Pt(9)
    styles["Title"].paragraph_format.keep_with_next = True
    _font(styles["Subtitle"], family, 13, "465467")
    styles["Subtitle"].paragraph_format.space_after = Pt(10)
    styles["Subtitle"].paragraph_format.keep_with_next = True
    for level, size in enumerate((17, 14, 12), 1):
        heading = styles[f"Heading {level}"]
        _font(heading, family, size if formatting.heading_hierarchy else 14, "20242B", bold=True)
        heading.paragraph_format.space_before = Pt(12 if level == 1 else 8)
        heading.paragraph_format.space_after = Pt(4)
        heading.paragraph_format.keep_with_next = True
        heading.paragraph_format.keep_together = True
    for name, size, color, bold in [
        ("DI Abstract", formatting.body_font_size, "354356", False),
        ("DI Auxiliary Heading", 11, "354356", True),
        ("DI Auxiliary Body", 9, "465467", False),
        ("DI Table Text", min(formatting.body_font_size, 10), "20242B", False),
    ]:
        style = styles.add_style(name, WD_STYLE_TYPE.PARAGRAPH)
        style.base_style = normal
        _font(style, family, size, color, bold=bold)
        style.paragraph_format.space_after = Pt(4)
    styles["DI Auxiliary Heading"].paragraph_format.space_before = Pt(8)
    styles["DI Auxiliary Heading"].paragraph_format.keep_with_next = True
    for name in ("List Bullet", "List Number", "Quote", "Caption"):
        _font(styles[name], family, formatting.body_font_size, "20242B")
        styles[name].paragraph_format.space_after = Pt(5)
    for name in ("Header", "Footer"):
        _font(styles[name], family, 9, "465467")


def _configure_page(document, formatting: ProfileFormatting) -> None:
    section = document.sections[0]
    width, height = (21.0, 29.7) if formatting.page_size == "A4" else (21.59, 27.94)
    section.page_width, section.page_height = Cm(width), Cm(height)
    for side in ("top", "right", "bottom", "left"):
        setattr(section, f"{side}_margin", Cm(getattr(formatting.margins_cm, side)))
    section.header_distance = Cm(min(1.15, formatting.margins_cm.top / 2))
    section.footer_distance = Cm(min(1.15, formatting.margins_cm.bottom / 2))
    if formatting.header:
        section.header.paragraphs[0].text = formatting.header
    if formatting.footer or formatting.page_numbers:
        footer = section.footer.paragraphs[0]
        footer.alignment = WD_ALIGN_PARAGRAPH.CENTER
        if formatting.footer:
            footer.add_run(formatting.footer + ("  ·  " if formatting.page_numbers else ""))
        if formatting.page_numbers:
            footer.add_run("第 ")
            field = _word_element("fldSimple", instr="PAGE")
            run = _word_element("r")
            cached = _word_element("t")
            cached.text = "1"
            run.append(cached)
            field.append(run)
            footer._p.append(field)
            footer.add_run(" 页")


def _mark(paragraph, name: str, sequence) -> None:
    mark_id = next(sequence)
    start = _word_element("bookmarkStart", name=name, id=mark_id)
    props = paragraph._p.pPr
    paragraph._p.insert(1 if props is not None else 0, start)
    paragraph._p.append(_word_element("bookmarkEnd", id=mark_id))


def _list_number(document, ordered: bool) -> int:
    """为每个列表创建独立的编号实例，列表不会承接上一段编号。"""
    numbering = document.part.numbering_part.element
    definitions = numbering.findall(qn("w:abstractNum"))
    abstract_id = 1 + max((int(item.get(qn("w:abstractNumId"))) for item in definitions), default=-1)
    abstract = _word_element("abstractNum", abstractNumId=abstract_id)
    abstract.append(_word_element("multiLevelType", val="singleLevel"))
    level = _word_element("lvl", ilvl=0)
    level.append(_word_element("start", val=1))
    level.append(_word_element("numFmt", val="decimal" if ordered else "bullet"))
    level.append(_word_element("lvlText", val="%1." if ordered else "•"))
    level.append(_word_element("lvlJc", val="left"))
    props = _word_element("pPr")
    props.append(_word_element("ind", left=480, hanging=270))
    level.append(props)
    abstract.append(level)
    concrete_numbers = numbering.findall(qn("w:num"))
    if concrete_numbers:
        numbering.insert(numbering.index(concrete_numbers[0]), abstract)
    else:
        numbering.append(abstract)
    return numbering.add_num(abstract_id).numId


def _render_block(document, block, mark_name, sequence) -> None:
    if isinstance(block, ParagraphBlock):
        style_name = {"normal": "Normal", "quote": "Quote", "caption": "Caption"}[block.style]
        _mark(document.add_paragraph(block.text, style_name), mark_name, sequence)
        return
    if isinstance(block, ListBlock):
        number_id = _list_number(document, block.ordered)
        style_name = "List Number" if block.ordered else "List Bullet"
        for index, item in enumerate(block.items):
            paragraph = document.add_paragraph(item, style_name)
            props = paragraph._p.get_or_add_pPr().get_or_add_numPr()
            props.get_or_add_ilvl().val = 0
            props.get_or_add_numId().val = number_id
            if index == 0:
                _mark(paragraph, mark_name, sequence)
        return
    table = document.add_table(rows=0, cols=len(block.rows[0]))
    table.style = "Table Grid"
    for row_index, row_items in enumerate(block.rows):
        row = table.add_row()
        header = block.header and row_index == 0
        if header:
            row._tr.get_or_add_trPr().append(_word_element("tblHeader"))
        row._tr.get_or_add_trPr().append(_word_element("cantSplit"))
        for cell_index, (cell, text) in enumerate(zip(row.cells, row_items, strict=True)):
            paragraph = cell.paragraphs[0]
            paragraph.style = "DI Table Text"
            run = paragraph.add_run(text)
            if header:
                run.bold = True
                cell._tc.get_or_add_tcPr().append(_word_element("shd", fill="EEF2F6"))
            if row_index == cell_index == 0:
                _mark(paragraph, mark_name, sequence)


def render_docx(
    draft: DocumentDraft,
    target: str | Path,
    formatting: ProfileFormatting | ProfileDefinition | None = None,
) -> Path:
    """把 Word 写入服务端预留的新路径，禁止覆盖已有文件。"""
    path = Path(target)
    if path.suffix.lower() != ".docx":
        raise ValueError("暂存文档必须使用 .docx 扩展名")
    profile = formatting if isinstance(formatting, ProfileDefinition) else None
    if profile:
        formatting = profile.formatting
    formatting = formatting or ProfileFormatting()
    document = Document()
    document.core_properties.title = draft.title
    document.core_properties.author = "General Agent Platform"
    _define_styles(document, formatting)
    _configure_page(document, formatting)
    document.add_paragraph(draft.title, "Title")
    if draft.subtitle:
        document.add_paragraph(draft.subtitle, "Subtitle")
    if draft.metadata:
        for label, value in draft.metadata.items():
            document.add_paragraph(f"{label}：{value}", "DI Auxiliary Body")
    if draft.abstract:
        document.add_paragraph(draft.abstract, "DI Abstract")

    bookmark_ids = count(1)
    for index, section in enumerate(sorted(draft.sections, key=lambda item: item.order), 1):
        heading = document.add_heading(section.title, section.level)
        if formatting.section_page_break and index > 1 and section.level == 1:
            heading.paragraph_format.page_break_before = True
        _mark(heading, f"di_s_{section.id}", bookmark_ids)
        for block in section.blocks:
            _render_block(document, block, f"di_b{index}_{block.id}", bookmark_ids)
    if draft.missing_fields:
        document.add_paragraph("待补充信息", "DI Auxiliary Heading")
        labels = {item.id: item.label for item in profile.input_fields()} if profile else {}
        for field_id in draft.missing_fields:
            paragraph = document.add_paragraph(labels.get(field_id, field_id), "DI Auxiliary Body")
            _mark(paragraph, f"di_m_{field_id}", bookmark_ids)
    if draft.sources:
        document.add_paragraph("资料来源", "DI Auxiliary Heading")
        for index, source in enumerate(draft.sources, 1):
            details = " · ".join(piece for piece in (source.locator, source.detail) if piece)
            document.add_paragraph(
                f"{index}. {source.label}" + (f"（{details}）" if details else ""),
                "DI Auxiliary Body",
            )

    handle = path.open("xb")
    try:
        with handle:
            document.save(handle)
        # 只有 Word 包能被独立打开时才交付暂存文件。
        Document(path)
    except Exception:
        path.unlink(missing_ok=True)
        raise
    return path
