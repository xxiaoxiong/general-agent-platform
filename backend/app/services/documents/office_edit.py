"""定点编辑 Word 原包，保留其余版式、关系和二进制资源。"""

from __future__ import annotations

import hashlib
import io
import posixpath
import re
from collections import defaultdict
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Literal, Mapping
from urllib.parse import unquote, urlsplit
from zipfile import ZIP_DEFLATED, BadZipFile, ZipFile

from lxml import etree
from PIL import Image, ImageOps, UnidentifiedImageError
from pydantic import AfterValidator, ConfigDict, Field, model_validator

from .parser import DocumentParseError, validate_reference_file
from .schemas import Anchor, DocumentSchema, ShortText, ValidationReport, _xml_text

_NS = {
    "w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    "wp": "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing",
    "pic": "http://schemas.openxmlformats.org/drawingml/2006/picture",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "rel": "http://schemas.openxmlformats.org/package/2006/relationships",
    "ct": "http://schemas.openxmlformats.org/package/2006/content-types",
    "mc": "http://schemas.openxmlformats.org/markup-compatibility/2006",
    "dc": "http://purl.org/dc/elements/1.1/",
}
_PARTS = re.compile(r"word/(?:(?:header|footer)\d+|footnotes|endnotes)\.xml")
_HEADINGS = re.compile(r"^(?:heading|标题)\s*([1-9])$", re.IGNORECASE)
_ALLOWED_RASTER = {"PNG", "JPEG", "GIF", "WEBP", "TIFF", "BMP"}
_MAX_IMAGE_BYTES = 12 * 1024**2
_MAX_IMAGE_PIXELS = 25_000_000
_MAX_PARAGRAPHS = 5_000
_MAX_IMAGES = 300
_MAX_TEXT = 280_000
_MAX_NEW_MEDIA = 40 * 1024**2
_EMU_PER_MM = 36_000
_XML_SPACE = "{http://www.w3.org/XML/1998/namespace}space"
_OBJECT = "\ufffc"
_TEXT_NODES = ".//w:r/*[not(self::w:rPr)]"


class OfficeEditError(ValueError):
    """原文锚点、图像和修改操作无法安全应用。"""


def _word_text(value: str) -> str:
    _xml_text(value)
    if any(separator in value for separator in ("\n", "\r", "\t", _OBJECT)):
        raise ValueError("段落内的文本改动不能跨换行、制表符或内嵌对象")
    return value


_RawText = Annotated[str, Field(max_length=12_000), AfterValidator(_word_text)]
_RawMatch = Annotated[str, Field(min_length=1, max_length=12_000), AfterValidator(_word_text)]
_Dimension = Annotated[float, Field(ge=1, le=800)]


class _Operation(DocumentSchema):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, str_strip_whitespace=False)
    target_id: Anchor


class ReplaceText(_Operation):
    operation: Literal["replace_text"]
    before: _RawMatch
    after: _RawText
    occurrence: int | None = Field(default=None, ge=1, le=100)


class ReplaceImage(_Operation):
    operation: Literal["replace_image"]
    asset_id: Anchor


class InsertImage(_Operation):
    operation: Literal["insert_image"]
    asset_id: Anchor
    width_mm: _Dimension
    alt: ShortText = ""


class ResizeImage(_Operation):
    operation: Literal["resize_image"]
    width_mm: _Dimension | None = None
    height_mm: _Dimension | None = None

    @model_validator(mode="after")
    def _dimensions(self):
        if self.width_mm is None and self.height_mm is None:
            raise ValueError("请提供图片的宽度或高度")
        return self


class RemoveImage(_Operation):
    operation: Literal["remove_image"]


class SetImageAlt(_Operation):
    operation: Literal["set_image_alt"]
    alt: ShortText


OfficeOperation = Annotated[
    ReplaceText | ReplaceImage | InsertImage | ResizeImage | RemoveImage | SetImageAlt,
    Field(discriminator="operation"),
]


class OfficeEditPlan(DocumentSchema):
    summary: ShortText = ""
    operations: list[OfficeOperation] = Field(min_length=1, max_length=100)


class OfficeParagraph(DocumentSchema):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False, allow_inf_nan=False)
    id: Anchor
    section: str
    text: str
    style: str = ""
    location: Literal["body", "table", "header", "footer", "note"] = "body"
    heading_level: int | None = None
    editable: bool = True


class OfficeImage(DocumentSchema):
    id: Anchor
    paragraph_id: Anchor
    section: str
    width_mm: float
    height_mm: float
    width_pixels: int | None = None
    height_pixels: int | None = None
    format: str | None = None
    alt: str = ""
    caption: str = ""
    checksum: str
    previewable: bool = False
    editable: bool = True


class OfficeDocument(DocumentSchema):
    title: str
    paragraphs: list[OfficeParagraph]
    images: list[OfficeImage]
    warnings: list[str] = Field(default_factory=list)


class OfficeChange(DocumentSchema):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False, allow_inf_nan=False)
    id: Anchor
    operation: str
    target_id: Anchor
    section: str
    before: str | OfficeImage | None
    after: str | OfficeImage | None
    replacement_asset_id: Anchor | None = None


class OfficeEditResult(DocumentSchema):
    document: OfficeDocument
    changes: list[OfficeChange]
    validation: ValidationReport


def _q(prefix: str, name: str) -> str:
    return f"{{{_NS[prefix]}}}{name}"


def _xml(data: bytes):
    try:
        return etree.fromstring(data, etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False))
    except etree.XMLSyntaxError as exc:
        raise OfficeEditError("Word 文档结构损坏") from exc


def _path_for_rels(name: str) -> str:
    return posixpath.join(posixpath.dirname(name), "_rels", posixpath.basename(name) + ".rels")


def _local_target(part: str, target: str) -> str:
    uri = urlsplit(target)
    if uri.scheme or uri.netloc or uri.query or uri.fragment:
        return ""
    path = unquote(uri.path)
    if "\\" in path or "\x00" in path:
        return ""
    return (posixpath.normpath(path).lstrip("/") if path.startswith("/")
            else posixpath.normpath(posixpath.join(posixpath.dirname(part), path)))


def _raster(file: bytes):
    try:
        image = Image.open(io.BytesIO(file))
    except (UnidentifiedImageError, Image.DecompressionBombError, OSError, ValueError) as exc:
        raise OfficeEditError("图片格式无法识别或尺寸超过上限") from exc
    if image.format not in _ALLOWED_RASTER or image.width * image.height > _MAX_IMAGE_PIXELS:
        image.close()
        raise OfficeEditError("图片格式无法识别或尺寸超过上限")
    return image


def _raster_info(data: bytes) -> tuple[str | None, int | None, int | None]:
    try:
        with _raster(data) as image:
            return image.format, image.width, image.height
    except OfficeEditError:
        return None, None, None


def _reencode(data: bytes, *, thumbnail: bool, replacement: bool = False) -> tuple[bytes, str]:
    if not 0 < len(data) <= _MAX_IMAGE_BYTES:
        raise OfficeEditError("图片大小超过处理上限")
    try:
        with _raster(data) as image:
            if replacement and getattr(image, "n_frames", 1) > 1:
                raise OfficeEditError("替换图片须为单帧图像")
            converted = ImageOps.exif_transpose(image)
            if thumbnail:
                converted.thumbnail((512, 512))
            out = io.BytesIO()
            if image.format == "JPEG" or (thumbnail and "A" not in converted.getbands()
                                          and "transparency" not in converted.info):
                clean = converted.convert("RGB")
                fmt, mime = "JPEG", "image/jpeg"
            else:
                clean = converted.convert("RGBA")
                fmt, mime = "PNG", "image/png"
            # 新建画布拿掉源图片的 EXIF、文本块和颜色配置等附带信息。
            pixels = Image.new(clean.mode, clean.size)
            pixels.paste(clean)
            pixels.save(out, format=fmt, quality=92)
            if out.tell() > _MAX_IMAGE_BYTES:
                raise OfficeEditError("写入 Word 的图片超过处理上限")
            return out.getvalue(), mime
    except (OSError, ValueError) as exc:
        if isinstance(exc, OfficeEditError):
            raise
        raise OfficeEditError("无法完整读取图片像素") from exc


def _occurrence(text: str, word: str, choice: int | None) -> tuple[int, int]:
    matches = list(re.finditer(re.escape(word), text))
    if not matches:
        raise OfficeEditError("目标段落找不到指定原文")
    if (choice is None and len(matches) > 1) or (choice is not None and choice > len(matches)):
        raise OfficeEditError("这段原文的出现位置不唯一，请提供更完整的文字或出现序号")
    return matches[(choice or 1) - 1].span()


def _minimal_text_edit(start: int, end: int, before: str, after: str) -> tuple[int, int, str]:
    # 相同的首尾留在原 run 中，可保留粗体、超链接和其他内联样式。
    limit = min(len(before) - 1, len(after))
    prefix = 0
    while prefix < limit and before[prefix] == after[prefix]:
        prefix += 1
    suffix = 0
    while suffix < limit - prefix and before[-1 - suffix] == after[-1 - suffix]:
        suffix += 1
    return start + prefix, end - suffix, after[prefix:len(after) - suffix]


def _editable_text(paragraph) -> bool:
    # 字段结果、内容控件与修订原文交给 Word，避免改变文档的隐藏语义。
    return not paragraph.xpath(
        ".//w:fldSimple | .//w:fldChar | .//w:instrText | .//w:ins | .//w:del | "
        "ancestor::w:sdt | .//w:sdt | ancestor::w:ins | ancestor::w:del", namespaces=_NS,
    )


def _text_nodes(paragraph):
    parts = []
    for node in paragraph.xpath(_TEXT_NODES, namespaces=_NS):
        if next(node.iterancestors(_q("w", "p")), None) is not paragraph:
            continue
        if node.tag == _q("w", "t"):
            parts.append((node.text or "", node))
        elif node.tag == _q("w", "tab"):
            parts.append(("\t", None))
        elif node.tag in {_q("w", "br"), _q("w", "cr")}:
            parts.append(("\n", None))
        else:
            parts.append((_OBJECT, None))
    return parts


def _replace(span: tuple[int, int], after: str, nodes: list) -> None:
    start, end = span
    offset = 0
    inserted = False
    for original, node in nodes:
        left, right = max(0, start - offset), min(len(original), end - offset)
        offset += len(original)
        if left >= right:
            continue
        if node is None:
            raise OfficeEditError("修改不能穿过文档中的内嵌对象或控制字符")
        text = node.text or ""
        node.text = text[:left] + (after if not inserted else "") + text[right:]
        if node.text != node.text.strip():
            node.set(_XML_SPACE, "preserve")
        inserted = True
    if not inserted:
        raise OfficeEditError("无法定位段落的实际 XML 文本")


@dataclass
class _Paragraph:
    data: OfficeParagraph
    part: str
    text_nodes: list
    node: etree._Element


@dataclass
class _Picture:
    data: OfficeImage
    part: str
    media: str
    drawing: etree._Element
    frame: etree._Element
    embed: etree._Element
    properties: etree._Element | None
    geometry: etree._Element | None


class _Package:
    def __init__(self, zipfile: ZipFile):
        self.archive = zipfile
        self.names = set(zipfile.namelist())
        self.parts: dict[str, etree._Element] = {}
        self.dirty: set[str] = set()
        self.media: dict[str, bytes] = {}
        self.replaced_relationships: dict[str, set[str]] = defaultdict(set)
        self.removed_media: set[str] = set()
        self.media_info: dict[str, tuple[str, str | None, int | None, int | None]] = {}
        self.paragraphs: dict[str, _Paragraph] = {}
        self.pictures: dict[str, _Picture] = {}
        self.warnings: list[str] = []
        self.title = ""
        self.next_shape_id: int | None = None
        self._inspect()

    def xml(self, name: str):
        if name not in self.parts:
            self.parts[name] = _xml(self.archive.read(name))
        return self.parts[name]

    def _picture_info(self, media: str):
        if media not in self.media_info:
            data = self.archive.read(media)
            dims = _raster_info(data) if len(data) <= _MAX_IMAGE_BYTES else (None, None, None)
            self.media_info[media] = (hashlib.sha256(data).hexdigest(), *dims)
        return self.media_info[media]

    def _inspect(self):
        total_chars = 0
        unsupported = 0
        if "word/document.xml" not in self.names:
            raise OfficeEditError("Word 正文部件无法定位")
        if self.xml("word/document.xml").find("w:body", namespaces=_NS) is None:
            raise OfficeEditError("Word 正文格式无法编辑，请用 Word 另存为 DOCX")
        styles = self._paragraph_styles()
        for part in ["word/document.xml", *sorted(name for name in self.names if _PARTS.fullmatch(name))]:
            section = "正文" if part == "word/document.xml" else (
                "页眉" if "header" in part else "页脚" if "footer" in part else "注释")
            path_rels = _path_for_rels(part)
            rels = self.xml(path_rels) if path_rels in self.names else []
            rel_targets = {
                rel.get("Id"): _local_target(part, rel.get("Target", ""))
                for rel in rels if rel.get("TargetMode", "Internal") == "Internal"
                and rel.get("Type", "").endswith("/image")
            }
            root = self.xml(part)
            for node in root.xpath(".//w:p[not(ancestor::w:txbxContent)]", namespaces=_NS):
                parts = _text_nodes(node)
                text = "".join(value for value, _ in parts)
                total_chars += len(text)
                if len(self.paragraphs) >= _MAX_PARAGRAPHS or total_chars > _MAX_TEXT:
                    raise OfficeEditError("原始 Word 正文超过可编辑范围，请缩小文档后重试")
                style = node.find("w:pPr/w:pStyle", namespaces=_NS)
                style_id = style.get(_q("w", "val"), "") if style is not None else ""
                style_name, level = styles.get(style_id, (style_id, None))
                heading = _HEADINGS.fullmatch(style_name)
                outline = node.find("w:pPr/w:outlineLvl", namespaces=_NS)
                if heading:
                    level = int(heading[1])
                if outline is not None and outline.get(_q("w", "val"), "") in tuple(str(i) for i in range(9)):
                    level = int(outline.get(_q("w", "val"))) + 1
                if part == "word/document.xml" and (style_name.lower() in {"title", "标题"} or (level == 1 and not self.title)):
                    self.title = text.strip()[:180]
                if level is not None:
                    section = text.strip()[:180] or section
                place = ("table" if next(node.iterancestors(_q("w", "tc")), None) is not None else
                         "header" if "header" in part else "footer" if "footer" in part else
                         "note" if "notes" in part else "body")
                paragraph = OfficeParagraph(id=f"p{len(self.paragraphs) + 1}", section=section, text=text,
                                            style=style_id, location=place, heading_level=level,
                                            editable=_editable_text(node))
                self.paragraphs[paragraph.id] = _Paragraph(paragraph, part, parts, node)
                for drawing in node.xpath("./w:r/w:drawing | ./w:hyperlink/w:r/w:drawing", namespaces=_NS):
                    if not self._add_picture(part, paragraph, node, drawing, rel_targets):
                        unsupported += 1
                unsupported += len(node.xpath("./w:r/w:pict | ./w:r/mc:AlternateContent", namespaces=_NS))
        if "docProps/core.xml" in self.names and not self.title:
            self.title = self.xml("docProps/core.xml").findtext("dc:title", "", namespaces=_NS)[:180]
        if unsupported:
            self.warnings.append(f"保留 {unsupported} 个不支持直接编辑的绘图（如图表、链接图片或旧式绘图）")

    def _paragraph_styles(self) -> dict[str, tuple[str, int | None]]:
        result = {}
        if "word/styles.xml" not in self.names:
            return result
        for style in self.xml("word/styles.xml").xpath(".//w:style[@w:type='paragraph']", namespaces=_NS):
            name = style.find("w:name", namespaces=_NS)
            level = style.find("w:pPr/w:outlineLvl", namespaces=_NS)
            value = level.get(_q("w", "val"), "") if level is not None else ""
            result[style.get(_q("w", "styleId"), "")] = (
                name.get(_q("w", "val"), "") if name is not None else "",
                int(value) + 1 if value in "012345678" and len(value) == 1 else None,
            )
        return result

    def _add_picture(self, part, paragraph, node, drawing, rel_targets) -> bool:
        frame = drawing.find("wp:inline", namespaces=_NS)
        if frame is None:
            frame = drawing.find("wp:anchor", namespaces=_NS)
        if frame is None:
            return False
        blips = frame.xpath(".//pic:pic/pic:blipFill/a:blip", namespaces=_NS)
        extent = frame.find("wp:extent", namespaces=_NS)
        if len(blips) != 1 or extent is None:
            return False
        embed = blips[0]
        media = rel_targets.get(embed.get(_q("r", "embed")), "")
        if not media.startswith("word/media/") or media not in self.names:
            return False
        try:
            width, height = int(extent.get("cx", "0")), int(extent.get("cy", "0"))
        except ValueError:
            return False
        if width <= 0 or height <= 0:
            return False
        if len(self.pictures) >= _MAX_IMAGES:
            raise OfficeEditError("图片数量超过可编辑范围，请拆分文档后重试")
        picture = frame.find(".//pic:pic", namespaces=_NS)
        geometry = picture.find("pic:spPr/a:xfrm/a:ext", namespaces=_NS)
        props = frame.find("wp:docPr", namespaces=_NS)
        caption_node = node.getnext()
        caption = self._caption(caption_node)
        digest, fmt, px_w, px_h = self._picture_info(media)
        data = OfficeImage(
            id=f"img{len(self.pictures) + 1}", paragraph_id=paragraph.id, section=paragraph.section,
            width_mm=round(width / _EMU_PER_MM, 3), height_mm=round(height / _EMU_PER_MM, 3),
            width_pixels=px_w, height_pixels=px_h, format=fmt, previewable=bool(fmt),
            alt=(props.get("descr") or props.get("title") or "")[:500] if props is not None else "",
            caption=caption, checksum=digest, editable=paragraph.editable,
        )
        self.pictures[data.id] = _Picture(data, part, media, drawing, frame, embed, props, geometry)
        return True

    @staticmethod
    def _caption(paragraph) -> str:
        if paragraph is None or paragraph.tag != _q("w", "p"):
            return ""
        text = "".join(value for value, _ in _text_nodes(paragraph)).strip()
        style = paragraph.find("w:pPr/w:pStyle", namespaces=_NS)
        if (style is not None and style.get(_q("w", "val"), "").lower() in {"caption", "题注"}
                or re.match(r"^(?:图\s*[\d一二三四五六七八九十]+|figure\s+\d+)\b", text, re.I)):
            return text[:500]
        return ""

    def describe(self, filename: str):
        return OfficeDocument(
            title=self.title or filename[:180], paragraphs=[item.data for item in self.paragraphs.values()],
            images=[item.data for item in self.pictures.values()], warnings=self.warnings,
        )

    def _add_image_relationship(self, part: str, data: bytes, mime: str) -> str:
        extension = "png" if mime == "image/png" else "jpeg"
        media_path = f"word/media/document-edit-{hashlib.sha256(data).hexdigest()}.{extension}"
        if media_path in self.names and self.archive.read(media_path) != data:
            raise OfficeEditError("原始 Word 包中包含冲突的图片部件")
        rel_path = _path_for_rels(part)
        if rel_path not in self.names and rel_path not in self.parts:
            self.parts[rel_path] = etree.Element(_q("rel", "Relationships"), nsmap={None: _NS["rel"]})
        rels = self.xml(rel_path)
        target = posixpath.relpath(media_path, start=posixpath.dirname(part))
        existing = next((rel for rel in rels if rel.get("Target") == target
                         and rel.get("Type", "").endswith("/image")), None)
        if existing is None:
            used = {rel.get("Id") for rel in rels}
            number = len(used) + 1
            while f"rId{number}" in used:
                number += 1
            rid = f"rId{number}"
            etree.SubElement(rels, _q("rel", "Relationship"), Id=rid, Target=target,
                             Type=f'{_NS["r"]}/image')
            self.dirty.add(rel_path)
        else:
            rid = existing.get("Id")
        if media_path not in self.names:
            self.media.setdefault(media_path, data)
            if sum(map(len, self.media.values())) > _MAX_NEW_MEDIA:
                raise OfficeEditError("替换图片的总大小超出单次编辑上限")
        types = self.xml("[Content_Types].xml")
        declarations = types.findall("ct:Default", namespaces=_NS)
        known = next((item for item in declarations if item.get("Extension", "").lower() == extension), None)
        if known is None:
            item = etree.Element(_q("ct", "Default"), Extension=extension, ContentType=mime)
            types.insert(0, item)
            self.dirty.add("[Content_Types].xml")
        elif known.get("ContentType") != mime and not any(
            item.get("PartName") == f"/{media_path}" for item in types.findall("ct:Override", namespaces=_NS)
        ):
            etree.SubElement(types, _q("ct", "Override"), PartName=f"/{media_path}", ContentType=mime)
            self.dirty.add("[Content_Types].xml")
        return rid

    def replace_image(self, picture: _Picture, data: bytes, mime: str):
        if hashlib.sha256(data).hexdigest() == picture.data.checksum:
            raise OfficeEditError("替换图片与原图相同")
        self.track_relationships(picture)
        picture.embed.set(_q("r", "embed"), self._add_image_relationship(picture.part, data, mime))
        # Office 首选同一 blip 的 SVG 扩展时，删除旧的矢量分支以显示替换后的图片。
        for fallback in picture.embed.xpath('.//*[local-name()="svgBlip"]'):
            extension_node = fallback.getparent()
            extension_node.getparent().remove(extension_node)
        picture.embed.attrib.pop(_q("r", "link"), None)

    def insert_image(self, paragraph: _Paragraph, data: bytes, mime: str, width_mm: float, alt: str):
        from docx.oxml.shape import CT_Inline

        _, width_pixels, height_pixels = _raster_info(data)
        if not width_pixels or not height_pixels:
            raise OfficeEditError("插入图片的像素尺寸无法读取")
        width = round(width_mm * _EMU_PER_MM)
        height = round(width * height_pixels / width_pixels)
        if not 0 < height <= 800 * _EMU_PER_MM:
            raise OfficeEditError("当前宽度下图片的高度超过上限")
        rid = self._add_image_relationship(paragraph.part, data, mime)
        if self.next_shape_id is None:
            ids = [
                int(value) for part in self.parts.values()
                for value in part.xpath(".//wp:docPr/@id", namespaces=_NS)
                if value.isascii() and value.isdecimal() and 0 < len(value) <= 10
            ]
            self.next_shape_id = max(ids, default=0) + 1
        shape_id = self.next_shape_id
        if shape_id > 4294967295:
            raise OfficeEditError("Word 绘图编号超过上限")
        self.next_shape_id += 1
        inline = CT_Inline.new_pic_inline(shape_id, rid, "插入图片", width, height)
        if alt:
            inline.docPr.set("descr", alt)
        run = etree.SubElement(paragraph.node, _q("w", "r"))
        drawing = etree.SubElement(run, _q("w", "drawing"))
        drawing.append(inline)
        self.dirty.add(paragraph.part)

    def track_relationships(self, picture: _Picture):
        self.replaced_relationships[picture.part].update(
            value for node in picture.drawing.iter() for attr, value in node.attrib.items()
            if attr.startswith(f'{{{_NS["r"]}}}')
        )

    def prune_unused_images(self):
        if not self.replaced_relationships:
            return
        removed = set()
        for part, candidates in self.replaced_relationships.items():
            still_used = {value for node in self.xml(part).iter() for key, value in node.attrib.items()
                          if key.startswith(f'{{{_NS["r"]}}}')}
            rels_path = _path_for_rels(part)
            rels = self.xml(rels_path)
            for rel in list(rels):
                if rel.get("Id") in candidates - still_used and rel.get("Type", "").endswith("/image"):
                    media = _local_target(part, rel.get("Target", ""))
                    if media.startswith("word/media/") and media in self.names:
                        removed.add(media)
                        rels.remove(rel)
                        self.dirty.add(rels_path)
        if not removed:
            return
        # 页眉、页脚和图表可能共享媒体引用。仅清理整个包内都不再引用的原图。
        active = set()
        for name in self.names | self.dirty:
            parent, rels_dir, file = name.rpartition("/_rels/")
            if not rels_dir or not file.endswith(".rels"):
                continue
            part = posixpath.join(parent, file[:-5])
            for rel in self.xml(name):
                if rel.get("TargetMode", "Internal") == "Internal":
                    active.add(_local_target(part, rel.get("Target", "")))
        self.removed_media = removed - active
        types = self.xml("[Content_Types].xml")
        for item in types.findall("ct:Override", namespaces=_NS):
            if unquote(item.get("PartName", "")).lstrip("/") in self.removed_media:
                types.remove(item)
                self.dirty.add("[Content_Types].xml")

    def write(self, stream):
        with ZipFile(stream, "w", compression=ZIP_DEFLATED) as result:
            for info in self.archive.infolist():
                if info.filename in self.removed_media:
                    continue
                if info.filename in self.dirty:
                    data = etree.tostring(self.parts[info.filename], encoding="UTF-8", xml_declaration=True, standalone=True)
                else:
                    data = self.archive.read(info)
                result.writestr(info, data)
            for name, data in self.media.items():
                result.writestr(name, data)
            for name in sorted(self.dirty - self.names):
                result.writestr(name, etree.tostring(self.parts[name], encoding="UTF-8", xml_declaration=True))


@contextmanager
def _open(path: str | Path):
    try:
        target = validate_reference_file(path)
        if target.suffix.lower() != ".docx":
            raise OfficeEditError("保留排版的原文件编辑须使用 DOCX")
        with ZipFile(target) as archive:
            yield _Package(archive)
    except DocumentParseError as exc:
        raise OfficeEditError(str(exc)) from exc
    except BadZipFile as exc:
        raise OfficeEditError("Word 文件含有损坏的压缩部件") from exc


def inspect_office_document(path: str | Path) -> OfficeDocument:
    with _open(path) as package:
        return package.describe(Path(path).stem)


def office_image_bytes(path: str | Path, image_id: str, *, thumbnail: bool = False) -> tuple[bytes, str]:
    """只读取原包中已经由稳定锚点定位的内嵌图片。"""
    with _open(path) as package:
        picture = package.pictures.get(image_id)
        if picture is None or not picture.data.previewable:
            raise OfficeEditError("图片不存在或无法预览")
        return _reencode(package.archive.read(picture.media), thumbnail=thumbnail)


def office_vision_images(path: str | Path, image_ids: set[str] | None = None,
                         *, limit: int = 4) -> list[tuple[str, bytes, str]]:
    """给视觉模型返回有限数量的小图，调用方负责确认模型支持图片输入。"""
    with _open(path) as package:
        responses = []
        for anchor, picture in package.pictures.items():
            if len(responses) >= min(limit, 6):
                break
            if picture.data.previewable and (image_ids is None or anchor in image_ids):
                try:
                    data, mime = _reencode(package.archive.read(picture.media), thumbnail=True)
                except OfficeEditError:
                    continue
                responses.append((anchor, data, mime))
        return responses


def _resize(picture: _Picture, op: ResizeImage):
    extent = picture.frame.find("wp:extent", namespaces=_NS)
    current_width, current_height = int(extent.get("cx")), int(extent.get("cy"))
    width = round(op.width_mm * _EMU_PER_MM) if op.width_mm is not None else None
    height = round(op.height_mm * _EMU_PER_MM) if op.height_mm is not None else None
    if width is None:
        width = round(height * current_width / current_height)
    if height is None:
        height = round(width * current_height / current_width)
    if not 0 < width <= 800 * _EMU_PER_MM or not 0 < height <= 800 * _EMU_PER_MM:
        raise OfficeEditError("按比例缩放后的图片尺寸超过上限")
    if (width, height) == (current_width, current_height):
        raise OfficeEditError("调整后的图片尺寸与原图相同")
    for node in (extent, picture.geometry):
        if node is not None:
            node.set("cx", str(width))
            node.set("cy", str(height))


def _uploaded_image_data(path: Path) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise OfficeEditError("找不到可用的图片附件")
    with path.open("rb") as stream:
        return stream.read(_MAX_IMAGE_BYTES + 1)


def office_asset_thumbnail(path: str | Path) -> tuple[bytes, str]:
    """在完成文件归属和指纹核实之后，读取当前任务附件缩略图。"""
    return _reencode(_uploaded_image_data(Path(path)), thumbnail=True)


def _image_asset(asset_id: str, image_sources: Mapping[str, str | Path] | None) -> tuple[bytes, str]:
    uploaded = (image_sources or {}).get(asset_id)
    if uploaded is None:
        raise OfficeEditError("插入或替换的图片必须来自当前任务已经校验的附件")
    return _reencode(_uploaded_image_data(Path(uploaded)), thumbnail=False, replacement=True)


def apply_office_edits(
    source: str | Path, plan: OfficeEditPlan, target: str | Path, *,
    image_sources: Mapping[str, str | Path] | None = None, accepted_ids: set[str] | None = None,
) -> OfficeEditResult:
    """应用获批的操作并重读 Word。目标文件是调用方提供的全新暂存路径。"""
    result_path = Path(target)
    if result_path.suffix.lower() != ".docx":
        raise OfficeEditError("文档暂存文件须为 DOCX")
    operations = {f"change_{index}": op for index, op in enumerate(plan.operations, start=1)}
    if accepted_ids is not None:
        if not accepted_ids or not accepted_ids.issubset(operations):
            raise OfficeEditError("没有可应用的改动，或所选改动已失效")
        operations = {change_id: op for change_id, op in operations.items() if change_id in accepted_ids}
    spans = defaultdict(list)
    image_ops = defaultdict(list)
    inserted = defaultdict(list)
    assets: dict[str, tuple[bytes, str]] = {}

    def get_asset(asset_id: str) -> tuple[bytes, str]:
        if asset_id not in assets:
            assets[asset_id] = _image_asset(asset_id, image_sources)
        return assets[asset_id]

    written = False
    try:
        with _open(source) as package:
            for change_id, op in operations.items():
                if isinstance(op, ReplaceText):
                    item = package.paragraphs.get(op.target_id)
                    if item is None or not item.data.editable:
                        raise OfficeEditError("段落不存在或包含受保护的 Word 结构")
                    start, end = _occurrence(item.data.text, op.before, op.occurrence)
                    if op.before == op.after:
                        raise OfficeEditError("修改后的段落片段与原文相同")
                    spans[op.target_id].append(_minimal_text_edit(start, end, op.before, op.after))
                elif isinstance(op, InsertImage):
                    paragraph = package.paragraphs.get(op.target_id)
                    if paragraph is None or not paragraph.data.editable:
                        raise OfficeEditError("插入目标段落不存在或含有未支持的结构")
                    inserted[op.target_id].append((change_id, op))
                else:
                    picture = package.pictures.get(op.target_id)
                    if picture is None or not picture.data.editable:
                        raise OfficeEditError("目标图片不存在或不属于可编辑的内嵌图片")
                    image_ops[op.target_id].append((change_id, op))
            for target_id, edits in spans.items():
                item = package.paragraphs[target_id]
                boundary = len(item.data.text)
                for start, end, after in sorted(edits, reverse=True):
                    if end > boundary:
                        raise OfficeEditError("同一段落有重叠改动，请拆分后重试")
                    _replace((start, end), after, item.text_nodes)
                    boundary = start
                package.dirty.add(item.part)
            removed = set()
            expected_images = {key: item.data.checksum for key, item in package.pictures.items()}
            for target_id, edits in image_ops.items():
                picture = package.pictures[target_id]
                names = {op.operation for _, op in edits}
                if len(names) != len(edits) or ("remove_image" in names and len(edits) > 1):
                    raise OfficeEditError("同一图片有重复或相互冲突的操作")
                for _, op in edits:
                    if isinstance(op, ReplaceImage):
                        data, mime = get_asset(op.asset_id)
                        package.replace_image(picture, data, mime)
                        expected_images[target_id] = hashlib.sha256(data).hexdigest()
                    elif isinstance(op, ResizeImage):
                        _resize(picture, op)
                    elif isinstance(op, RemoveImage):
                        package.track_relationships(picture)
                        picture.drawing.getparent().remove(picture.drawing)
                        removed.add(target_id)
                    else:
                        if picture.properties is None:
                            raise OfficeEditError("图片没有可修改的说明属性")
                        if op.alt == picture.data.alt:
                            raise OfficeEditError("修改后的图片说明与原文相同")
                        picture.properties.set("descr", op.alt)
                        if not op.alt:
                            picture.properties.attrib.pop("title", None)
                package.dirty.add(picture.part)
            for target_id, insertions in inserted.items():
                paragraph = package.paragraphs[target_id]
                for change_id, op in insertions:
                    data, mime = get_asset(op.asset_id)
                    package.insert_image(paragraph, data, mime, op.width_mm, op.alt)
                    expected_images[change_id] = hashlib.sha256(data).hexdigest()
            old_paragraphs = package.paragraphs
            old_pictures = package.pictures
            expected_text = ["".join(text for text, _ in _text_nodes(item.node)) for item in old_paragraphs.values()]
            package.prune_unused_images()
            with result_path.open("xb") as stream:
                written = True
                package.write(stream)
        # 重读真实结果，验证图片锚点与段落位置在修改后仍存在。
        document = inspect_office_document(result_path)
        from docx import Document

        try:
            Document(result_path)
        except (ValueError, KeyError, etree.XMLSyntaxError) as exc:
            raise OfficeEditError("导出的 Word 文件没有通过重新打开校验") from exc
        # 图片删除后公开锚点序号变化，映射到经过校验的新版锚点。
        kept_by_paragraph = defaultdict(list)
        for anchor, image in old_pictures.items():
            if anchor not in removed:
                kept_by_paragraph[image.data.paragraph_id].append(anchor)
        result_anchors = [
            anchor for paragraph in old_paragraphs
            for anchor in (*kept_by_paragraph[paragraph], *(change_id for change_id, _ in inserted.get(paragraph, [])))
        ]
        if [expected_images[key] for key in result_anchors] != [image.checksum for image in document.images]:
            raise OfficeEditError("修改后的 Word 图片清单和计划不一致")
        if expected_text != [paragraph.text for paragraph in document.paragraphs]:
            raise OfficeEditError("修改后的 Word 段落结构与文字和计划不一致")
        after_pictures = dict(zip(result_anchors, document.images))
        changes = []
        for change_id, op in operations.items():
            if isinstance(op, ReplaceText):
                changes.append(OfficeChange(id=change_id, operation=op.operation, target_id=op.target_id,
                    section=old_paragraphs[op.target_id].data.section, before=op.before, after=op.after))
            elif isinstance(op, InsertImage):
                changes.append(OfficeChange(id=change_id, operation=op.operation, target_id=op.target_id,
                    section=old_paragraphs[op.target_id].data.section, before=None,
                    after=after_pictures[change_id], replacement_asset_id=op.asset_id))
            else:
                before = old_pictures[op.target_id].data
                after = after_pictures.get(op.target_id)
                changes.append(OfficeChange(id=change_id, operation=op.operation, target_id=op.target_id,
                    section=before.section, before=before, after=after,
                    replacement_asset_id=op.asset_id if isinstance(op, ReplaceImage) else None))
        return OfficeEditResult(document=document, changes=changes, validation=ValidationReport(
            checks=["source_package_checked", "native_anchor_patch", "word_file_reopened", "image_inventory_checked"],
        ))
    except Exception:
        if written:
            result_path.unlink(missing_ok=True)
        raise
