"""Verification（P4-02）：按任务类型的确定性验证器。

任务书 §9：
- 按任务类型选择验证器（不给简单问答加验证负担，不额外调用模型）；
- 验证失败必须产生结构化事件，不允许模型说"已完成"就通过；
- 结论：通过 → 最终交付；部分通过 → 补充执行；不通过 → 修复/重试/请求用户。

验证依据是执行证据（工具账本 + 最终文本），全部确定性可测试：
- answer_present       最终回答非空（模型自评不算证据）
- citations_present    研究任务：回答/工具输出含来源 URL
- artifact_produced    文档任务：写文件工具成功执行
- computation_evidence 数据任务：计算/统计工具被执行或回答含数值结论
- db_evidence          数据库任务：db 工具成功执行
- exec_evidence        代码任务：python_exec / 写文件成功执行
- side_effect_evidence 业务任务：副作用工具有外部 Operation ID（账本证据）
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any, Dict, List, Optional, Sequence
from urllib.parse import urlsplit

from .task_contract import TaskContract

STATUS_PASSED = "passed"
STATUS_PARTIAL = "partial"
STATUS_FAILED = "failed"

_URL_RE = re.compile(r"https?://[^\s)）\"'<>，。；]+", re.IGNORECASE)
_NUMBER_RE = re.compile(r"\d[\d,]*\.?\d*%?")

_WRITE_TOOLS = {"write_file", "edit_file"}
_DB_TOOLS = {"db_query", "db_count", "db_aggregate"}
_EXEC_TOOLS = {"python_exec"}
_WEB_TOOLS = {"web_search", "fetch_webpage", "http_get"}
_SIDE_EFFECT_TOOLS = {"http_post", "send_email_tool", "webhook_notify"}
_ROW_COUNTS = {
    "raw_rows": re.compile(r"(?:原始记录|原始数据|输入记录|\b(?:raw|original) (?:rows|records)\b)(?:条数|行数|数|共计|共|为)?[ \t*`：:|=]*(\d+)(?=\s*(?:[条行|。.；]|$))", re.M | re.I),
    "valid_rows": re.compile(r"(?:有效记录|有效数据|有效条数|有效行数|\bvalid (?:rows|records)\b)(?:条数|行数|数|共计|共|为)?[ \t*`：:|=]*(\d+)(?=\s*(?:[条行|。.；]|$))", re.M | re.I),
}


def _invalid_json_constant(value: str) -> None:
    raise ValueError(value)


@dataclass
class ToolEvidence:
    """一次工具执行的验证证据（来自 Tool Execution Ledger）。"""

    tool_name: str
    status: str = "succeeded"          # succeeded / failed / unknown
    external_operation_id: Optional[str] = None
    output: str = ""
    path: str = ""                       # 已确认存在并可提交的产物相对路径
    artifact_content: Any = None         # 用于内容验收，不能当作模型自报事实


@dataclass
class CheckResult:
    id: str
    passed: bool
    detail: str

    def to_dict(self) -> Dict[str, Any]:
        return {"id": self.id, "passed": self.passed, "detail": self.detail}


@dataclass
class VerificationResult:
    status: str = STATUS_PASSED
    task_type: str = "other"
    checks: List[CheckResult] = field(default_factory=list)

    @property
    def failed_checks(self) -> List[CheckResult]:
        return [c for c in self.checks if not c.passed]

    def to_event_data(self) -> Dict[str, Any]:
        return {
            "status": self.status,
            "task_type": self.task_type,
            "checks": [c.to_dict() for c in self.checks],
        }

    def feedback_text(self) -> str:
        """补充执行的反馈提示（交给模型修复）。"""
        lines = ["以下完成条件尚未满足，请补充完成："]
        for c in self.failed_checks:
            lines.append(f"- [{c.id}] {c.detail}")
        return "\n".join(lines)


def _has_success(evidence: Sequence[ToolEvidence], names: set) -> bool:
    return any(e.tool_name in names and e.status == "succeeded" for e in evidence)


def _check_answer_present(final_text: str, _evidence: Sequence[ToolEvidence] = ()) -> CheckResult:
    text = (final_text or "").strip()
    if not text:
        return CheckResult("answer_present", False, "最终回答为空，任务未产出结论")
    return CheckResult("answer_present", True, "")


def _urls(text: str) -> set[tuple]:
    urls = set()
    for match in _URL_RE.findall(text):
        try:
            url = urlsplit(match.rstrip(".,;]}`"))
            if url.hostname:
                urls.add((url.hostname.casefold(), url.path.rstrip("/"), url.query))
        except ValueError:
            continue
    return urls


def _check_citations_present(final_text: str, evidence: Sequence[ToolEvidence], contract: TaskContract) -> CheckResult:
    observed = _urls("\n".join(contract.supplied_urls))
    for entry in evidence:
        if entry.status == "succeeded" and entry.tool_name in _WEB_TOOLS:
            observed.update(_urls(entry.output))
    verified = len(_urls(final_text) & observed)
    required = max(1, contract.source_count)
    if verified >= required:
        return CheckResult("citations_present", True, "")
    return CheckResult("citations_present", False, f"需要引用 {required} 个已读取或由用户提供的来源，当前验收 {verified} 个")


def _check_artifact_produced(evidence: Sequence[ToolEvidence], contract: TaskContract) -> CheckResult:
    valid = {item.path for item in evidence if item.tool_name in _WRITE_TOOLS and item.status == "succeeded" and item.path}
    names = {PurePosixPath(path).name for path in valid}
    suffixes = {PurePosixPath(path).suffix.removeprefix(".").casefold() for path in valid}
    missing = [path for path in contract.artifact_paths if path not in (valid if "/" in path else names)]
    missing.extend(f".{suffix} 文件" for suffix in contract.artifact_formats if suffix not in suffixes)
    if missing:
        return CheckResult("artifact_produced", False, "缺少可下载的交付文件 " + "、".join(missing[:12]))
    if not valid:
        return CheckResult("artifact_produced", False, "当前任务尚未在会话工作区保存可读取的交付文件")
    return CheckResult("artifact_produced", True, f"已核对 {len(valid)} 份交付文件")


def _check_table_facts(final_text: str, evidence: Sequence[ToolEvidence], contract: TaskContract) -> CheckResult:
    facts = contract.table_facts
    problems = []
    raw_count, valid_count = facts.raw_rows, facts.valid_rows
    text_parts = [("最终正文", final_text), *((row.path, row.artifact_content) for row in evidence
                  if row.path and isinstance(row.artifact_content, str))]
    matched_valid_count = False
    for label, text in text_parts:
        for field, expected in (("raw_rows", raw_count), ("valid_rows", valid_count)):
            found = {int(value) for value in _ROW_COUNTS[field].findall(text)}
            if field == "valid_rows" and expected in found:
                matched_valid_count = True
            if found - {expected}:
                metric = "原始" if field == "raw_rows" else "有效"
                problems.append(f"{label} 的{metric}记录数应为 {expected} 条")
    if contract.expected_json:
        verified_json = []
        targets = {path for path in contract.artifact_paths if path.casefold().endswith(".json")}
        for row in evidence:
            if not row.path or not row.path.casefold().endswith(".json"):
                continue
            if targets and row.path not in targets and PurePosixPath(row.path).name not in targets:
                continue
            verified_json.append((row.path, row.artifact_content))
        if "json_answer" in contract.completion_checks:
            try:
                verified_json.append(("最终 JSON", json.loads(final_text, parse_constant=_invalid_json_constant)))
            except (ValueError, RecursionError):
                pass
        if not verified_json:
            problems.append("缺少可核对条数和排除记录的 JSON 交付")
        for label, data in verified_json:
            for field, expected in contract.expected_json.items():
                actual = data.get(field) if isinstance(data, dict) else None
                equal = type(actual) is type(expected) and actual == expected
                if isinstance(expected, list) and isinstance(actual, list) and all(isinstance(item, str) for item in actual):
                    equal = sorted(actual) == sorted(expected)
                if not equal:
                    problems.append(f"{label} 的 {field} 应为 {json.dumps(expected, ensure_ascii=False)}")
    elif not matched_valid_count:
        problems.append(f"请在交付中写明从原始表格独立复算的有效记录数 {valid_count} 条")
    return CheckResult("table_facts", not problems, "。".join(problems[:6]) if problems else f"已按原始表格复算 {raw_count} 条记录和 {valid_count} 条有效记录")


def _check_computation_evidence(final_text: str, evidence: Sequence[ToolEvidence]) -> CheckResult:
    calc_used = any(e.tool_name in {"calculator", "db_aggregate"} and e.status == "succeeded"
                    for e in evidence)
    if calc_used:
        return CheckResult("computation_evidence", True, "")
    if _NUMBER_RE.search(final_text or ""):
        # 回答含数值结论但无计算证据：部分可信（不阻塞交付，标记供复核）
        return CheckResult("computation_evidence", True, "数值结论未经计算工具复核")
    return CheckResult("computation_evidence", False, "数据分析任务缺少计算/统计证据")


def _check_db_evidence(_final_text: str, evidence: Sequence[ToolEvidence]) -> CheckResult:
    if _has_success(evidence, _DB_TOOLS):
        return CheckResult("db_evidence", True, "")
    return CheckResult("db_evidence", False, "数据库任务未成功执行任何数据库查询")


def _check_exec_evidence(_final_text: str, evidence: Sequence[ToolEvidence]) -> CheckResult:
    if _has_success(evidence, _EXEC_TOOLS | _WRITE_TOOLS):
        return CheckResult("exec_evidence", True, "")
    return CheckResult("exec_evidence", False, "代码任务未执行代码或写入文件")


def _check_side_effect_evidence(_final_text: str, evidence: Sequence[ToolEvidence]) -> CheckResult:
    side_effects = [e for e in evidence if e.tool_name in _SIDE_EFFECT_TOOLS]
    succeeded = [e for e in side_effects if e.status == "succeeded"]
    if succeeded:
        with_op = [e for e in succeeded if e.external_operation_id]
        if with_op:
            return CheckResult("side_effect_evidence", True, "")
        return CheckResult("side_effect_evidence", True, "副作用已执行但无外部 Operation ID（账本留痕供核对）")
    if any(e.status == "unknown" for e in side_effects):
        return CheckResult("side_effect_evidence", False, "外部操作结果未知，需人工核对后再确认完成")
    return CheckResult("side_effect_evidence", False, "业务任务未执行任何外部副作用操作")


_CHECKS = {
    "answer_present": _check_answer_present,
    "computation_evidence": _check_computation_evidence,
    "db_evidence": _check_db_evidence,
    "exec_evidence": _check_exec_evidence,
    "side_effect_evidence": _check_side_effect_evidence,
}


def verify_task(
    contract: TaskContract,
    final_text: str,
    evidence: Sequence[ToolEvidence],
    *,
    execution_checks: Sequence[CheckResult] = (),
) -> VerificationResult:
    """执行验证（确定性，零模型调用）。

    状态口径：
    - passed：全部检查通过；
    - partial：answer_present 通过但证据类检查有缺口（可交付，标记复核）；
    - failed：回答缺失或关键证据缺失（不得按"已完成"交付）。
    """
    checks: List[CheckResult] = []
    for cid in contract.completion_checks:
        if cid == "citations_present":
            checks.append(_check_citations_present(final_text, evidence, contract))
        elif cid == "artifact_produced":
            checks.append(_check_artifact_produced(evidence, contract))
        elif cid == "table_facts" and contract.table_facts:
            checks.append(_check_table_facts(final_text, evidence, contract))
        elif cid == "minimum_chars":
            count = sum(not char.isspace() for char in final_text)
            checks.append(CheckResult(cid, count >= contract.minimum_chars, f"正文共 {count} 个非空白字符，要求至少 {contract.minimum_chars} 个"))
        elif cid == "json_answer":
            try:
                json.loads(final_text, parse_constant=_invalid_json_constant)
                valid = True
            except (ValueError, RecursionError):
                valid = False
            checks.append(CheckResult(cid, valid, "" if valid else "回复需要是可直接解析的 JSON"))
        elif fn := _CHECKS.get(cid):
            checks.append(fn(final_text, evidence))

    checks.extend(execution_checks)

    answer = next((c for c in checks if c.id == "answer_present"), None)
    failed = [c for c in checks if not c.passed]

    if answer is not None and not answer.passed:
        status = STATUS_FAILED
    elif not failed:
        status = STATUS_PASSED
    elif len(failed) * 2 >= len(checks):
        status = STATUS_FAILED
    else:
        status = STATUS_PARTIAL

    return VerificationResult(status=status, task_type=contract.task_type, checks=checks)
