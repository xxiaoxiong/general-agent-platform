"""既有 DOCX 的样式/排版优化：只调整格式，不改动文字内容。

用于「优化文档样式、排版」这类诉求：在用户原文档的副本上统一字体、字号、
行距、标题层级、表格样式与页边距，保证事实内容与章节结构完全不变。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm, Pt, RGBColor

# 默认样式方案：中文报告常用规范（标题黑体、正文宋体小四、1.5 倍行距）
_DEFAULT_PLAN: dict[str, Any] = {
    "body_font": "宋体",
    "body_size_pt": 12.0,
    "line_spacing": 1.5,
    "first_line_indent_chars": 2.0,
    "space_before_pt": 0.0,
    "space_after_pt": 6.0,
    "justify": True,
    "title_font": "黑体",
    "title_size_pt": 18.0,
    "heading_font": "黑体",
    "heading_sizes_pt": [16.0, 14.0, 12.0],
    "table_header_bold": True,
    "table_header_shading": "D9D9D9",
    "table_border_pt": 0.5,
    "page_margin_cm": 2.54,
}

_CN_SIZES = {
    "小二": 18.0, "二号": 22.0, "三号": 16.0, "小三": 15.0,
    "四号": 14.0, "小四": 12.0, "五号": 10.5, "小五": 9.0,
}
_FONTS = ("宋体", "仿宋", "楷体", "黑体", "微软雅黑", "思源宋体", "思源黑体", "方正小标宋")
_HEADING_STYLE = re.compile(r"^(?:heading|标题)\s*([1-9])$", re.I)


def style_plan_from_request(request: str) -> dict[str, Any]:
    """把用户口述的格式要求映射为样式参数；未指定的沿用默认方案。"""
    plan = {**_DEFAULT_PLAN, "heading_sizes_pt": list(_DEFAULT_PLAN["heading_sizes_pt"])}
    text = request or ""
    for name, size in _CN_SIZES.items():
        if name in text:
            plan["body_size_pt"] = size
            break
    match = re.search(r"(\d+(?:\.\d+)?)\s*倍行距", text) or re.search(r"行距\s*(\d+(?:\.\d+)?)", text)
    if match:
        plan["line_spacing"] = float(match.group(1))
    match = re.search(r"首行缩进\s*(\d+)\s*字符", text)
    if match:
        plan["first_line_indent_chars"] = float(match.group(1))
    match = re.search(r"页边距[^。；，,]{0,8}(\d+(?:\.\d+)?)\s*(?:cm|厘米)", text, re.I)
    if match:
        plan["page_margin_cm"] = float(match.group(1))
    for font in _FONTS:
        if re.search(rf"正文[^。；，,]{{0,12}}{font}", text):
            plan["body_font"] = font
        if re.search(rf"标题[^。；，,]{{0,12}}{font}", text):
            plan["heading_font"] = font
    return plan


def _heading_level(paragraph) -> int | None:
    """返回标题级别：0=文档标题，1..n=标题层级，None=正文。"""
    name = (getattr(paragraph.style, "name", "") or "").strip()
    if name.casefold() in {"title", "标题"}:
        return 0
    match = _HEADING_STYLE.match(name)
    if match:
        return int(match.group(1))
    properties = paragraph._p.pPr
    outline = properties.find(qn("w:outlineLvl")) if properties is not None else None
    if outline is not None:
        try:
            level = int(outline.get(qn("w:val")))
        except (TypeError, ValueError):
            return None
        return level + 1 if 0 <= level <= 8 else None
    return None


def _is_list(paragraph) -> bool:
    name = (getattr(paragraph.style, "name", "") or "").casefold()
    if name.startswith("list") or "列表" in name:
        return True
    properties = paragraph._p.pPr
    return bool(properties is not None and properties.find(qn("w:numPr")) is not None)


def _apply_font(paragraph, font: str, size: float, *, bold: bool | None = None,
                color: str | None = None) -> None:
    for run in paragraph.runs:
        run.font.name = font
        run.font.size = Pt(size)
        if bold is not None:
            run.font.bold = bold
        if color:
            run.font.color.rgb = RGBColor.from_string(color)
        properties = run._element.get_or_add_rPr()
        fonts = properties.find(qn("w:rFonts"))
        if fonts is None:
            fonts = OxmlElement("w:rFonts")
            properties.append(fonts)
        fonts.set(qn("w:eastAsia"), font)


def _apply_body(paragraph, plan: dict[str, Any]) -> None:
    _apply_font(paragraph, plan["body_font"], plan["body_size_pt"])
    fmt = paragraph.paragraph_format
    fmt.line_spacing = plan["line_spacing"]
    fmt.space_before = Pt(plan["space_before_pt"])
    fmt.space_after = Pt(plan["space_after_pt"])
    if _is_list(paragraph):
        fmt.first_line_indent = None
        return
    fmt.first_line_indent = Pt(plan["body_size_pt"] * plan["first_line_indent_chars"])
    if plan["justify"]:
        fmt.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY


def _apply_heading(paragraph, level: int, plan: dict[str, Any]) -> None:
    sizes = plan["heading_sizes_pt"]
    size = float(sizes[min(level, len(sizes)) - 1])
    _apply_font(paragraph, plan["heading_font"], size, bold=True, color="000000")
    fmt = paragraph.paragraph_format
    fmt.line_spacing = plan["line_spacing"]
    fmt.space_before = Pt(12 if level == 1 else 8)
    fmt.space_after = Pt(4)
    fmt.keep_with_next = True
    fmt.first_line_indent = None


def _apply_title(paragraph, plan: dict[str, Any]) -> None:
    _apply_font(paragraph, plan["title_font"], plan["title_size_pt"], bold=True, color="000000")
    fmt = paragraph.paragraph_format
    fmt.alignment = WD_ALIGN_PARAGRAPH.CENTER
    fmt.space_before = Pt(0)
    fmt.space_after = Pt(12)
    fmt.first_line_indent = None


def _apply_table(table, plan: dict[str, Any]) -> None:
    properties = table._tbl.tblPr
    borders = properties.find(qn("w:tblBorders"))
    if borders is None:
        borders = OxmlElement("w:tblBorders")
        properties.append(borders)
    size = str(max(2, int(round(plan["table_border_pt"] * 8))))
    for edge in ("top", "left", "bottom", "right", "insideH", "insideV"):
        element = borders.find(qn("w:" + edge))
        if element is None:
            element = OxmlElement("w:" + edge)
            borders.append(element)
        element.set(qn("w:val"), "single")
        element.set(qn("w:sz"), size)
        element.set(qn("w:space"), "0")
        element.set(qn("w:color"), "000000")
    if not table.rows:
        return
    for cell in table.rows[0].cells:
        cell_properties = cell._tc.get_or_add_tcPr()
        shading = cell_properties.find(qn("w:shd"))
        if shading is None:
            shading = OxmlElement("w:shd")
            cell_properties.append(shading)
        shading.set(qn("w:val"), "clear")
        shading.set(qn("w:color"), "auto")
        shading.set(qn("w:fill"), plan["table_header_shading"])
        for paragraph in cell.paragraphs:
            for run in paragraph.runs:
                run.font.bold = bool(plan["table_header_bold"])


def apply_style_plan(source: str | Path, target: str | Path, plan: dict[str, Any] | None = None) -> dict[str, Any]:
    """在 source 的副本上应用样式方案并写入 target；文字内容保持不变。"""
    resolved = {**_DEFAULT_PLAN, **(plan or {})}
    document = Document(str(source))
    body = headings = 0
    for paragraph in document.paragraphs:
        level = _heading_level(paragraph)
        if level == 0:
            _apply_title(paragraph, resolved)
            headings += 1
        elif level is not None:
            _apply_heading(paragraph, level, resolved)
            headings += 1
        elif paragraph.text.strip():
            _apply_body(paragraph, resolved)
            body += 1
    for table in document.tables:
        _apply_table(table, resolved)
    margin = Cm(resolved["page_margin_cm"])
    for section in document.sections:
        section.top_margin = section.bottom_margin = margin
        section.left_margin = section.right_margin = margin
    document.save(str(target))
    heading_sizes = "/".join(f"{size:g}" for size in resolved["heading_sizes_pt"])
    return {
        "body_paragraphs": body,
        "headings": headings,
        "tables": len(document.tables),
        # 供交付说明展示的实际改动（文字内容未变）
        "description": (
            f"正文{resolved['body_font']} {resolved['body_size_pt']:g}pt、"
            f"{resolved['line_spacing']:g} 倍行距、首行缩进 {resolved['first_line_indent_chars']:g} 字符；"
            f"标题{resolved['heading_font']} {heading_sizes}pt；"
            f"{len(document.tables)} 个表格统一表头与边框；页边距 {resolved['page_margin_cm']:g}cm"
        ),
        "plan": {key: resolved[key] for key in ("body_font", "body_size_pt", "line_spacing",
                                                "heading_font", "heading_sizes_pt",
                                                "table_border_pt", "page_margin_cm")},
    }
