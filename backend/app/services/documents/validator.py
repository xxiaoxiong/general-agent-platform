"""程序化检查草稿及真正重新打开的 Word 文件。"""

from __future__ import annotations

from datetime import date
import math
import re
from pathlib import Path

from .parser import DocumentParseError, parse_docx
from .schemas import (
    DocumentDraft, InputField, ProfileDefinition, Severity, ValidationIssue, ValidationReport, block_text,
)


def _issue(code: str, message: str, *, severity: Severity = "error", section_id: str | None = None):
    return ValidationIssue(code=code, message=message, severity=severity, section_id=section_id)


def valid_field(field: InputField, value) -> bool:
    if field.type == "number":
        return type(value) is int or (type(value) is float and math.isfinite(value))
    if field.type == "multiple_choice":
        return isinstance(value, list) and bool(value) and all(
            isinstance(item, str) and item in field.options for item in value
        )
    if not isinstance(value, str) or not value.strip():
        return False
    if field.type == "single_choice":
        return value in field.options
    if field.type == "date":
        try:
            return value == date.fromisoformat(value).isoformat()
        except ValueError:
            return False
    return True


def _visible(field: InputField, value, content: str) -> bool:
    if field.type == "date":
        ready = date.fromisoformat(value)
        if value in content:
            return True
        return re.search(rf"{ready.year}年\s*0?{ready.month}月\s*0?{ready.day}日", content) is not None
    if field.type == "number":
        return re.search(rf"(?<![\d.]){re.escape(str(value))}(?![\d.])", content.replace(",", "")) is not None
    if isinstance(value, list):
        return all(item in content for item in value)
    return re.sub(r"\s+", "", value) in re.sub(r"\s+", "", content)


def _validate_profile(draft: DocumentDraft, profile: ProfileDefinition) -> list[ValidationIssue]:
    issues = []
    actual = {section.id: section for section in draft.sections}
    expected = {section.id: section for section in profile.sections}
    known_ids = [
        section.id for section in sorted(draft.sections, key=lambda item: item.order) if section.id in expected
    ]
    expected_order = [
        section.id for section in sorted(profile.sections, key=lambda item: item.order) if section.id in actual
    ]
    if known_ids != expected_order:
        issues.append(_issue("section_order", "章节顺序与文档规范不一致"))
    for definition in profile.sections:
        section = actual.get(definition.id)
        if definition.required and (section is None or not any(block_text(block).strip(" |\n") for block in section.blocks)):
            issues.append(_issue("required_section", f"必需章节「{definition.title}」缺少内容", section_id=definition.id))
        if section is None:
            continue
        if section.title != definition.title or section.level != definition.level:
            issues.append(_issue("section_heading", f"章节「{definition.title}」标题或层级不符合规范", section_id=definition.id))
        if definition.source_requirements and not any(block.source_ids for block in section.blocks):
            issues.append(_issue("source_requirement", f"章节「{definition.title}」缺少来源引用",
                                 severity="warning", section_id=definition.id))
    for section_id in sorted(set(actual) - set(expected)):
        issues.append(_issue("unexpected_section", f"章节「{actual[section_id].title}」未在规范中定义", section_id=section_id))
    if not profile.formatting.tables_allowed and any(
        block.type == "table" for section in draft.sections for block in section.blocks
    ):
        issues.append(_issue("tables_disallowed", "文档规范限制使用表格"))
    text = "\n".join([draft.title, draft.subtitle, draft.abstract, *[
        block_text(block) for section in draft.sections for block in section.blocks
    ]])
    required_ids = {field.id for field in profile.required_inputs if field.required}
    required_ids.update(field_id for section in profile.sections if section.id in actual for field_id in section.required_fields)
    for field in profile.input_fields():
        field_id = field.id
        value = draft.field_values.get(field_id)
        if field_id in draft.missing_fields:
            issues.append(_issue("input_marked_missing", f"字段「{field.label}」已标记为待补充", severity="warning"))
        elif field_id in required_ids or value is not None:
            if not valid_field(field, value):
                issues.append(_issue("required_input", f"字段「{field.label}」缺失或类型有误"))
            elif field_id in required_ids and not _visible(field, value, text):
                issues.append(_issue("input_not_visible", f"字段「{field.label}」的值未在正文中找到",
                                     severity="warning"))
    return issues


def validate_draft(draft: DocumentDraft, profile: ProfileDefinition | None = None) -> ValidationReport:
    issues = []
    checks = ["content", "source_references", "tables"]
    if not any(block_text(block).strip(" |\n") for section in draft.sections for block in section.blocks):
        issues.append(_issue("empty_content", "文档正文为空"))
    sources = {source.id for source in draft.sources}
    for section in draft.sections:
        for block in section.blocks:
            if set(block.source_ids) - sources:
                issues.append(_issue("unknown_source", "正文引用了未登记的资料来源", section_id=section.id))
    for field_id in sorted(set(draft.missing_fields) & set(draft.field_values)):
        issues.append(_issue("conflicting_input", f"字段 {field_id} 同时存在值和待补充标记"))
    if profile is not None:
        checks.extend(["profile_sections", "profile_inputs"])
        issues.extend(_validate_profile(draft, profile))
    return ValidationReport(issues=issues, checks=checks)


def _canonical(value):
    if isinstance(value, str):
        return value.replace("\r\n", "\n").replace("\r", "\n")
    if isinstance(value, list):
        return [_canonical(item) for item in value]
    if isinstance(value, dict):
        return {key: _canonical(item) for key, item in value.items()}
    return value


def _block_content(section):
    return [_canonical(block.model_dump(exclude={"source_ids"})) for block in section.blocks]


def validate_docx(
    path: str | Path, draft: DocumentDraft, profile: ProfileDefinition | None = None,
) -> ValidationReport:
    """比较 Word 文件实际结构与草稿，发现缺失、乱序或渲染时内容损坏。"""
    draft_check = validate_draft(draft, profile)
    checks, issues = list(draft_check.checks), list(draft_check.issues)
    checks.append("docx_opened")
    try:
        actual = parse_docx(path)
    except DocumentParseError:
        issues.append(_issue("unreadable_docx", "生成的 Word 不存在、为空或无法重新打开"))
        return ValidationReport(issues=issues, checks=checks)
    checks.extend(["docx_headings", "docx_blocks", "docx_missing_fields"])
    if actual.title != draft.title or _canonical(actual.subtitle) != _canonical(draft.subtitle):
        issues.append(_issue("docx_title", "生成的 Word 标题或副标题与草稿不一致"))
    if _canonical(actual.abstract) != _canonical(draft.abstract):
        issues.append(_issue("docx_abstract", "生成的 Word 摘要与草稿不一致"))
    expected = sorted(draft.sections, key=lambda section: section.order)
    if [(s.id, s.title, s.level) for s in expected] != [(s.id, s.title, s.level) for s in actual.sections]:
        issues.append(_issue("docx_sections", "生成的 Word 章节缺失、乱序或标题层级错误"))
    by_id = {section.id: section for section in actual.sections}
    for section in expected:
        reopened = by_id.get(section.id)
        if reopened is not None and _block_content(reopened) != _block_content(section):
            issues.append(_issue("docx_content", f"生成的 Word 中「{section.title}」内容与草稿不一致",
                                 section_id=section.id))
    if set(actual.missing_fields) != set(draft.missing_fields):
        issues.append(_issue("docx_missing_marker", "生成的 Word 缺少明确的待补充字段标记"))
    if actual.warnings:
        issues.extend(_issue("docx_parse_warning", message) for message in actual.warnings)
    return ValidationReport(issues=issues, checks=checks)
