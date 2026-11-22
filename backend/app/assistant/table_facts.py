"""对请求中明确给出结构和处理规则的表格独立复算可验证事实。"""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass

_COUNT_INTENT = re.compile(r"有效(?:记录|行|条|数量)|按.{0,20}去重|\b(?:valid rows|valid records|deduplicat\w*)\b", re.I)
_HEADER = re.compile(r"^\s*(?:表头|列名|格式|字段)\s*(?:为|是)?[\s:：]+(.+?)\s*$", re.I)
_DEDUPLICATE = re.compile(r"(?:按照|按)\s*[`“\"]?([^，。,;；\n`“”\"]{1,28}?)[`”\"]?(?:列|字段)?\s*去重|\bdeduplicate by\s+([\w-]+)\b", re.I)
_EXCLUSION = re.compile(r"(?:忽略|排除|跳过|exclude|ignore)\s*[`“\"]?([A-Z][A-Z0-9_-]{1,24})\b", re.I)
_ID_NAMES = {"id", "编号", "流水号", "记录号", "事件编号", "事件id", "recordid", "eventid", "transactionid"}
_MAX_ROWS = 2000


@dataclass(frozen=True)
class TableFacts:
    raw_rows: int
    valid_rows: int
    duplicate_ids: tuple[str, ...]
    excluded_ids: tuple[str, ...]
    exclusion: str

    def declared_fields(self, request: str) -> dict:
        available = {"raw_rows": self.raw_rows, "total_rows": self.raw_rows, "valid_rows": self.valid_rows,
                     "duplicate_ids": list(self.duplicate_ids), "excluded_ids": list(self.excluded_ids)}
        if self.exclusion.casefold() == "void":
            available["void_ids"] = list(self.excluded_ids)
        return {key: value for key, value in available.items() if re.search(rf"\b{key}\b", request, re.I)}


def _field(text: str) -> str:
    name = re.sub(r"[\s_-]+", "", text).casefold()
    return "id" if name in _ID_NAMES else name


def derive_table_facts(request: str) -> TableFacts | None:
    """只使用用户提供的表头、单个表格和明确的去重、排除规则。"""
    if not _COUNT_INTENT.search(request):
        return None
    lines = request.splitlines()
    headers = []
    for index, line in enumerate(lines):
        if match := _HEADER.match(line):
            header = match.group(1).rstrip("：: ")
            if "," in header or "\t" in header:
                headers.append((index, header))
        elif line.strip().casefold() in {"```csv", "```tsv"} and index + 1 < len(lines):
            headers.append((index + 1, lines[index + 1]))
    if len(headers) != 1:
        return None
    index, heading = headers[0]
    delimiter = "\t" if "\t" in heading else ","
    block: list[str] = []
    for line in lines[index + 1:]:
        stripped = line.strip()
        if stripped == "```":
            break
        if stripped:
            block.append(stripped)
    if not 2 <= len(block) <= _MAX_ROWS:
        return None
    try:
        header = [_field(name) for name in next(csv.reader([heading], delimiter=delimiter, strict=True))]
        rows = [tuple(cell.strip() for cell in row) for row in csv.reader(block, delimiter=delimiter, strict=True)]
    except csv.Error:
        return None
    if not 2 <= len(header) <= 32 or len(set(header)) != len(header) or any(len(row) != len(header) for row in rows):
        return None

    instructions = "\n".join(lines[:index])
    key_rule = _DEDUPLICATE.search(instructions)
    key = _field(key_rule.group(1) or key_rule.group(2)) if key_rule else ""
    if re.search(r"去重|\bdeduplicat\w*\b", request, re.I) and not (key and key in header):
        return None
    key_index = header.index(key) if key in header else None
    exclusion_rule = _EXCLUSION.search(instructions)
    exclusion = exclusion_rule.group(1).casefold() if exclusion_rule else ""
    filter_columns = [i for i, column in enumerate(header) if column in {"状态", "status", "state", "标志", "flag"}]
    if exclusion and len(filter_columns) != 1:
        return None
    filter_index = filter_columns[0] if exclusion else None

    seen = {}
    duplicates, excluded = [], []
    valid = 0
    for ordinal, row in enumerate(rows, start=1):
        identifier = row[key_index] if key_index is not None else str(ordinal)
        if not identifier:
            return None
        if identifier in seen:
            if seen[identifier] != row:
                return None  # 同键不同值需要用户的冲突处理规则。
            if identifier not in duplicates:
                duplicates.append(identifier)
            continue
        seen[identifier] = row
        if filter_index is not None and row[filter_index].casefold() == exclusion:
            excluded.append(identifier)
        else:
            valid += 1
    return TableFacts(len(rows), valid, tuple(duplicates), tuple(excluded), exclusion)
