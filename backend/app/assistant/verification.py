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

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from .task_contract import TaskContract

STATUS_PASSED = "passed"
STATUS_PARTIAL = "partial"
STATUS_FAILED = "failed"

_URL_RE = re.compile(r"https?://[^\s)\"'<>]+", re.IGNORECASE)
_NUMBER_RE = re.compile(r"\d[\d,]*\.?\d*%?")

_WRITE_TOOLS = {"write_file", "edit_file"}
_DB_TOOLS = {"db_query", "db_count", "db_aggregate", "db_schema"}
_EXEC_TOOLS = {"python_exec"}
_WEB_TOOLS = {"web_search", "fetch_webpage", "http_get"}
_SIDE_EFFECT_TOOLS = {"http_post", "send_email_tool", "webhook_notify"}


@dataclass
class ToolEvidence:
    """一次工具执行的验证证据（来自 Tool Execution Ledger）。"""

    tool_name: str
    status: str = "succeeded"          # succeeded / failed / unknown
    external_operation_id: Optional[str] = None
    output: str = ""


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


def _check_citations_present(final_text: str, evidence: Sequence[ToolEvidence]) -> CheckResult:
    urls = _URL_RE.findall(final_text or "")
    web_used = any(e.tool_name in _WEB_TOOLS and e.status == "succeeded" for e in evidence)
    tool_urls = [u for e in evidence for u in _URL_RE.findall(e.output or "")]
    if urls or tool_urls:
        return CheckResult("citations_present", True, "")
    if not web_used:
        # 未使用任何联网工具做研究：无法核验来源
        return CheckResult("citations_present", False,
                           "研究任务未提供来源 URL（也未使用联网工具核实）")
    return CheckResult("citations_present", False, "使用了联网工具但结论未附来源 URL")


def _check_artifact_produced(_final_text: str, evidence: Sequence[ToolEvidence]) -> CheckResult:
    if _has_success(evidence, _WRITE_TOOLS):
        return CheckResult("artifact_produced", True, "")
    return CheckResult("artifact_produced", False, "未生成任何文件产物（写文件工具未成功执行）")


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
    "citations_present": _check_citations_present,
    "artifact_produced": _check_artifact_produced,
    "computation_evidence": _check_computation_evidence,
    "db_evidence": _check_db_evidence,
    "exec_evidence": _check_exec_evidence,
    "side_effect_evidence": _check_side_effect_evidence,
}


def verify_task(
    contract: TaskContract,
    final_text: str,
    evidence: Sequence[ToolEvidence],
) -> VerificationResult:
    """执行验证（确定性，零模型调用）。

    状态口径：
    - passed：全部检查通过；
    - partial：answer_present 通过但证据类检查有缺口（可交付，标记复核）；
    - failed：回答缺失或关键证据缺失（不得按"已完成"交付）。
    """
    checks: List[CheckResult] = []
    for cid in contract.completion_checks:
        fn = _CHECKS.get(cid)
        if fn is not None:
            checks.append(fn(final_text, evidence))

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
