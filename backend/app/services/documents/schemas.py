"""文档规范、草稿、参考资料和局部编辑的结构化合同。"""

from __future__ import annotations

import re
from itertools import count
from typing import Annotated, Any, Literal

from pydantic import (
    AfterValidator, BaseModel, BeforeValidator, ConfigDict, Field, StrictFloat, StrictInt, model_validator,
)


def _xml_text(value: str) -> str:
    if re.search(r"[^\t\n\r\x20-\ud7ff\ue000-\ufffd\U00010000-\U0010ffff]", value):
        raise ValueError("文字中包含 Word 不支持的字符")
    return value


def _line(value: str) -> str:
    _xml_text(value)
    if "\n" in value or "\r" in value:
        raise ValueError("标题和名称必须为单行")
    return value


Anchor = Annotated[str, Field(min_length=1, max_length=32, pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_-]*$")]
Title = Annotated[str, Field(min_length=1, max_length=180), AfterValidator(_line)]
ShortText = Annotated[str, Field(max_length=500), AfterValidator(_xml_text)]
Text = Annotated[str, Field(max_length=12_000), AfterValidator(_xml_text)]
NonemptyText = Annotated[str, Field(min_length=1, max_length=12_000), AfterValidator(_xml_text)]
Strings = Annotated[list[ShortText], Field(max_length=40)]
InputValue = str | StrictInt | StrictFloat | list[str]
Severity = Literal["error", "warning", "info"]


class DocumentSchema(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, allow_inf_nan=False)


def _unique(values, message: str) -> None:
    if len(set(values)) != len(values):
        raise ValueError(message)


_ANCHOR_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]*$")


def normalize_anchor(value: Any, fallback: str) -> str:
    """把模型给出的中文/带标点标识归一为合法锚点（保留 ASCII 片段，否则用兜底）。"""
    raw = str(value or "").strip()
    if _ANCHOR_RE.fullmatch(raw):
        return raw
    parts = [part.strip("_-") for part in re.findall(r"[A-Za-z0-9_-]+", raw)]
    candidate = "_".join(part for part in parts if part)[:32]
    if not candidate:
        candidate = fallback
    candidate = re.sub(r"[^A-Za-z0-9_-]", "_", candidate)[:32]
    if not candidate or not candidate[0].isalnum():
        candidate = f"a{candidate}"[:32]
    return candidate


def unique_anchor(candidate: str, used: set[str]) -> str:
    value, suffix = candidate, 2
    while value in used:
        value = f"{candidate[:28]}_{suffix}"
        suffix += 1
    used.add(value)
    return value


class InputField(DocumentSchema):
    id: Anchor
    label: Title
    type: Literal["text", "date", "number", "single_choice", "multiple_choice"] = "text"
    required: bool = True
    description: ShortText = ""
    options: Strings = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_choices(self):
        _unique(self.options, "输入选项不能重复")
        if self.type in {"single_choice", "multiple_choice"}:
            if not self.options or any(not option for option in self.options):
                raise ValueError("选择字段需要非空选项")
        elif self.options:
            raise ValueError("当前输入类型不接受选项")
        return self


class ProfileSection(DocumentSchema):
    id: Anchor
    title: Title
    order: int = Field(ge=1, le=999)
    level: int = Field(default=1, ge=1, le=3)
    required: bool = True
    purpose: ShortText = ""
    required_fields: list[Anchor] = Field(default_factory=list, max_length=40)
    writing_guidance: Text = ""
    source_requirements: Strings = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_required_fields(self):
        _unique(self.required_fields, "章节所需字段不能重复")
        return self


class Margins(DocumentSchema):
    top: float = Field(default=2.54, ge=0.75, le=5)
    right: float = Field(default=2.54, ge=0.75, le=5)
    bottom: float = Field(default=2.54, ge=0.75, le=5)
    left: float = Field(default=2.54, ge=0.75, le=5)


class ProfileFormatting(DocumentSchema):
    page_size: Literal["A4", "Letter"] = "A4"
    margins_cm: Margins = Field(default_factory=Margins)
    heading_hierarchy: bool = True
    tables_allowed: bool = True
    font_family: Annotated[str, Field(min_length=1, max_length=80), AfterValidator(_line)] = "微软雅黑"
    body_font_size: int = Field(default=11, ge=9, le=16)
    line_spacing: float = Field(default=1.35, ge=1, le=2.2)
    header: ShortText = ""
    footer: ShortText = ""
    page_numbers: bool = True
    section_page_break: bool = False


class QualityRule(DocumentSchema):
    id: Anchor
    severity: Severity = "warning"
    description: Annotated[str, Field(min_length=1, max_length=500), AfterValidator(_xml_text)]


def _validate_structure(sections) -> None:
    _unique([s.id for s in sections], "章节标识不能重复")
    _unique([s.order for s in sections], "章节顺序不能重复")
    previous = 0
    for section in sorted(sections, key=lambda item: item.order):
        if section.level > previous + 1:
            raise ValueError("章节标题层级不能跳级")
        previous = section.level


class ProfileDefinition(DocumentSchema):
    schema_version: Literal[1] = 1
    document_type: Anchor
    name: Title
    description: ShortText = ""
    language: Annotated[str, Field(min_length=2, max_length=35, pattern=r"^[a-zA-Z][a-zA-Z0-9-]*$")] = "zh-CN"
    audience: ShortText = ""
    tone: ShortText = ""
    output_formats: list[Literal["docx"]] = Field(default_factory=lambda: ["docx"], min_length=1, max_length=1)
    sections: list[ProfileSection] = Field(min_length=1, max_length=60)
    required_inputs: list[InputField] = Field(default_factory=list, max_length=80)
    global_rules: Strings = Field(default_factory=list)
    formatting: ProfileFormatting = Field(default_factory=ProfileFormatting)
    quality_rules: list[QualityRule] = Field(default_factory=list, max_length=40)
    uncertainties: Strings = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def normalize_profile_shape(cls, value):
        """归一键值形态：中文标识转合法锚点、章节顺序缺失或重复时按出现顺序重排。"""
        if not isinstance(value, dict):
            return value
        data = dict(value)
        data["document_type"] = normalize_anchor(data.get("document_type"), "document")
        rename: dict[str, str] = {}
        inputs, seen_inputs = [], set()
        for index, field in enumerate(data.get("required_inputs") or []):
            if not isinstance(field, dict):
                inputs.append(field)
                continue
            field = dict(field)
            original = str(field.get("id") or "")
            field["id"] = unique_anchor(normalize_anchor(original, f"field{index + 1}"), seen_inputs)
            rename[original] = field["id"]
            inputs.append(field)
        data["required_inputs"] = inputs

        sections = [dict(item) if isinstance(item, dict) else item for item in data.get("sections") or []]
        seen_sections = set()
        for index, item in enumerate(sections):
            if not isinstance(item, dict):
                continue
            item["id"] = unique_anchor(normalize_anchor(item.get("id"), f"s{index + 1}"), seen_sections)
            renamed = []
            for raw in item.get("required_fields") or []:
                field_id = rename.get(str(raw)) or normalize_anchor(raw, "field")
                if field_id not in renamed:
                    renamed.append(field_id)
            item["required_fields"] = renamed

        rules = [dict(item) if isinstance(item, dict) else item for item in data.get("quality_rules") or []]
        seen_rules = set()
        for index, item in enumerate(rules):
            if isinstance(item, dict):
                item["id"] = unique_anchor(normalize_anchor(item.get("id"), f"rule{index + 1}"), seen_rules)
        data["quality_rules"] = rules

        orders = [item.get("order") if isinstance(item, dict) else None for item in sections]
        if not (all(isinstance(order, int) and order >= 1 for order in orders)
                and len(set(orders)) == len(orders)):
            for index, item in enumerate(sections):
                if isinstance(item, dict):
                    item["order"] = index + 1
        data["sections"] = sections
        return data

    @model_validator(mode="after")
    def validate_profile(self):
        _validate_structure(self.sections)
        _unique([item.id for item in self.required_inputs], "输入字段标识不能重复")
        _unique([rule.id for rule in self.quality_rules], "质量规则标识不能重复")
        if len(self.input_fields()) > 80:
            raise ValueError("文档规范配置了过多输入字段")
        return self

    def input_fields(self) -> list[InputField]:
        declared = {item.id for item in self.required_inputs}
        fields = {item.id: item for item in self.required_inputs}
        for section in self.sections:
            for field_id in section.required_fields:
                if field_id not in fields:
                    # 章节引用了未声明的字段：保留为可选推导字段，不向用户强制索要
                    # （模型常把章节 id 误填为 required_fields，强制必填会索要虚构资料）
                    fields[field_id] = InputField(id=field_id, label=field_id, required=False)
                elif section.required and not fields[field_id].required and field_id in declared:
                    # 只有规范中显式声明的字段才会被必填章节升级，误引用的推导字段始终保持可选
                    fields[field_id] = fields[field_id].model_copy(update={"required": True})
        return list(fields.values())


class SourceReference(DocumentSchema):
    id: Anchor
    label: Title
    kind: Literal["attachment", "user_input", "database", "knowledge", "mcp", "web", "document", "other"] = "other"
    detail: ShortText = ""
    locator: ShortText = ""


class _Block(DocumentSchema):
    id: Anchor | None = None
    source_ids: list[Anchor] = Field(default_factory=list, max_length=40)


class ParagraphBlock(_Block):
    type: Literal["paragraph"] = "paragraph"
    text: NonemptyText
    style: Literal["normal", "quote", "caption"] = "normal"


class ListBlock(_Block):
    type: Literal["list"] = "list"
    items: list[NonemptyText] = Field(min_length=1, max_length=150)
    ordered: bool = False


CellText = Annotated[str, Field(max_length=2_000), AfterValidator(_xml_text)]
TableRow = Annotated[list[CellText], Field(min_length=1, max_length=30)]


class TableBlock(_Block):
    type: Literal["table"] = "table"
    rows: list[TableRow] = Field(min_length=1, max_length=300)
    header: bool = True

    @model_validator(mode="after")
    def validate_rows(self):
        width = len(self.rows[0])
        if any(len(row) != width for row in self.rows):
            raise ValueError("表格各行的列数必须一致")
        return self


# 模型常见输出形态归一：类名式 type、单元格/列表项写成对象、表格行写成 {cells: [...]}。
# 内容结构由模型决定，格式差异不应让整轮生成失败后重试。
_BLOCK_TYPES = {
    "paragraph": "paragraph", "paragraphblock": "paragraph", "paragraph_block": "paragraph",
    "text": "paragraph", "textblock": "paragraph",
    "list": "list", "listblock": "list", "list_block": "list", "bullet": "list", "bullets": "list",
    "table": "table", "tableblock": "table", "table_block": "table",
}
_BLOCK_FIELDS = {
    "paragraph": {"id", "source_ids", "type", "text", "style"},
    "list": {"id", "source_ids", "type", "items", "ordered"},
    "table": {"id", "source_ids", "type", "rows", "header"},
}
_PARAGRAPH_STYLES = {"normal", "quote", "caption"}
_MAX_TABLE_COLUMNS = 30


def _cell_text(value: Any) -> Any:
    if isinstance(value, dict):
        for key in ("text", "content", "value", "title"):
            if isinstance(value.get(key), str):
                return value[key]
        return " ".join(str(item) for item in value.values() if isinstance(item, str))
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    return value


def _coerce_block_shape(value: Any) -> Any:
    """按等价语义归一单个内容块；无法承载内容时返回 None 由调用方丢弃。"""
    if not isinstance(value, dict):
        return value
    data = dict(value)
    kind = _BLOCK_TYPES.get(str(data.get("type") or "").strip().casefold())
    if kind is None:
        # 未知块类型按承载内容推断：模型有时写 section/chapter 或自造类型
        if isinstance(data.get("text"), str) and data["text"].strip():
            kind = "paragraph"
        elif isinstance(data.get("items"), list):
            kind = "list"
        elif isinstance(data.get("rows"), list):
            kind = "table"
        else:
            return None
    # 只保留该块类型认识的字段：模型常给列表/表格块附上段落专属字段
    data = {key: value for key, value in data.items() if key in _BLOCK_FIELDS[kind]}
    data["type"] = kind
    # 块锚点由系统分配：模型给出的超长/非法 id 直接丢弃，避免无意义校验失败
    ident = data.get("id")
    if ident is not None and (not isinstance(ident, str)
                              or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,31}", ident)):
        data.pop("id", None)
    if kind == "paragraph":
        text = data.get("text")
        if isinstance(text, str) and not text.strip():
            return None
        if data.get("style") not in _PARAGRAPH_STYLES:
            data["style"] = "normal"
    if kind == "list" and isinstance(data.get("items"), list):
        data["items"] = [cell for cell in (_cell_text(item) for item in data["items"])
                         if isinstance(cell, str) and cell.strip()]
        if not data["items"]:
            return None
    if kind == "table" and isinstance(data.get("rows"), list):
        rows = []
        for row in data["rows"]:
            if isinstance(row, dict):
                row = row.get("cells") or row.get("values") or []
            if not isinstance(row, list):
                continue
            cells = [_cell_text(cell) for cell in row][:_MAX_TABLE_COLUMNS]
            if any(isinstance(cell, str) and cell.strip() for cell in cells):
                rows.append(cells)
        if not rows:
            return None
        # 行宽不一致时按最大列数补空单元格，保留全部信息
        width = max(len(row) for row in rows)
        data["rows"] = [row + [""] * (width - len(row)) for row in rows]
    return data


def _coerce_blocks(value: Any) -> Any:
    if not isinstance(value, list):
        return value
    result = []
    for entry in value:
        # 模型有时把章节对象塞进 blocks：展开其内部块列表
        if isinstance(entry, dict) and str(entry.get("type") or "").strip().casefold() in {
            "section", "subsection", "chapter",
        } and isinstance(entry.get("blocks"), list):
            result.extend(_coerce_blocks(entry["blocks"]))
            continue
        coerced = _coerce_block_shape(entry)
        if coerced is not None:
            result.append(coerced)
    return result


ContentBlock = Annotated[ParagraphBlock | ListBlock | TableBlock, Field(discriminator="type")]
# 块列表与单块在进入判别联合之前先归一格式（判别联合本身不会调用成员的 before 校验）
BlockList = Annotated[list[ContentBlock], BeforeValidator(_coerce_blocks)]
BlockItem = Annotated[ContentBlock, BeforeValidator(_coerce_block_shape)]


def block_text(block: ContentBlock) -> str:
    if isinstance(block, ParagraphBlock):
        return block.text
    if isinstance(block, ListBlock):
        return "\n".join(block.items)
    return "\n".join(" | ".join(row) for row in block.rows)


class DraftSection(DocumentSchema):
    id: Anchor
    title: Title
    order: int = Field(ge=1, le=999)
    level: int = Field(default=1, ge=1, le=3)
    blocks: BlockList = Field(default_factory=list, max_length=300)

    @model_validator(mode="after")
    def assign_block_anchors(self):
        anchors = [block.id for block in self.blocks if block.id is not None]
        _unique(anchors, "章节中的内容锚点不能重复")
        used = set(anchors)
        sequence = count(1)
        for block in self.blocks:
            if block.id is None:
                candidate = f"b{next(sequence)}"
                while candidate in used:
                    candidate = f"b{next(sequence)}"
                block.id = candidate
                used.add(candidate)
        return self


class DocumentDraft(DocumentSchema):
    title: Title
    subtitle: ShortText = ""
    abstract: Text = ""
    metadata: dict[Anchor, ShortText] = Field(default_factory=dict, max_length=40)
    sections: list[DraftSection] = Field(min_length=1, max_length=60)
    sources: list[SourceReference] = Field(default_factory=list, max_length=120)
    field_values: dict[Anchor, InputValue] = Field(default_factory=dict, max_length=120)
    missing_fields: list[Anchor] = Field(default_factory=list, max_length=120)

    @model_validator(mode="after")
    def validate_draft_structure(self):
        _validate_structure(self.sections)
        _unique([source.id for source in self.sources], "来源标识不能重复")
        _unique(self.missing_fields, "缺失字段不能重复")
        if sum(len(block_text(block)) for section in self.sections for block in section.blocks) > 250_000:
            raise ValueError("文档草稿内容超出处理上限")
        return self


class ParsedDocument(DocumentSchema):
    title: Title
    subtitle: ShortText = ""
    abstract: Text = ""
    sections: list[DraftSection] = Field(default_factory=list)
    metadata: dict[str, ShortText] = Field(default_factory=dict)
    styles: dict[str, ShortText] = Field(default_factory=dict)
    warnings: list[ShortText] = Field(default_factory=list)
    missing_fields: list[Anchor] = Field(default_factory=list)

    def as_text(self, limit: int = 200_000) -> str:
        lines = [self.title]
        if self.subtitle:
            lines.append(self.subtitle)
        if self.abstract:
            lines.append(self.abstract)
        for section in self.sections:
            lines.append(f"{'#' * section.level} {section.title}")
            for block in section.blocks:
                if isinstance(block, ParagraphBlock):
                    lines.append(block.text)
                elif isinstance(block, ListBlock):
                    lines.extend(
                        f"{index}. {text}" if block.ordered else f"- {text}"
                        for index, text in enumerate(block.items, 1)
                    )
                else:
                    lines.extend(" | ".join(row) for row in block.rows)
        return "\n".join(lines)[:limit]


class ValidationIssue(DocumentSchema):
    code: Anchor
    severity: Severity = "error"
    message: ShortText
    section_id: Anchor | None = None


class ValidationReport(DocumentSchema):
    passed: bool = True
    issues: list[ValidationIssue] = Field(default_factory=list)
    checks: list[Anchor] = Field(default_factory=list)

    @model_validator(mode="after")
    def summarize(self):
        self.passed = not any(issue.severity == "error" for issue in self.issues)
        return self


class PatchOperation(DocumentSchema):
    operation: Literal["replace_section", "replace_block", "insert_block", "remove_block"]
    section_id: Anchor
    block_id: Anchor | None = None
    blocks: BlockList | None = Field(default=None, max_length=300)
    block: BlockItem | None = None
    reason: ShortText = ""
    source_ids: list[Anchor] = Field(default_factory=list, max_length=40)
    expected_before: Text | None = None

    @model_validator(mode="before")
    @classmethod
    def coerce_model_shape(cls, value):
        """纠正常见模型形状偏差：不改变内容语义，只丢弃互斥字段或单块列表。"""
        if not isinstance(value, dict):
            return value
        operation, blocks, block = value.get("operation"), value.get("blocks"), value.get("block")
        if operation == "replace_section" and isinstance(blocks, list) and blocks:
            # 整节重写以 blocks 为准：误带的 block/block_id 不参与语义
            return {**value, "block": None, "block_id": None}
        if (operation in {"replace_block", "insert_block"} and block is None
                and isinstance(blocks, list) and len(blocks) == 1 and isinstance(blocks[0], dict)):
            # 块级操作误把单块放进 blocks：等价改写为 block
            return {**value, "block": blocks[0], "blocks": None}
        return value

    @model_validator(mode="after")
    def validate_target(self):
        if self.operation == "replace_section":
            if self.blocks is None or self.block is not None or self.block_id is not None:
                raise ValueError("替换章节需要章节 ID 和内容块列表")
        elif self.blocks is not None:
            raise ValueError("块级操作只能提交单个内容块")
        elif self.operation == "remove_block":
            if not self.block_id or self.block is not None:
                raise ValueError("删除内容块需要明确现有块 ID")
        elif self.block is None or (self.operation == "replace_block" and not self.block_id):
            raise ValueError("替换或插入操作缺少目标或新块")
        return self


class DocumentPatch(DocumentSchema):
    operations: list[PatchOperation] = Field(min_length=1, max_length=50)
    summary: ShortText = ""
