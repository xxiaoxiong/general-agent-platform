"""统一最终验收：所有必需步骤、交付物、证据与未决副作用（任务书 §9）。

不重写专业验证器：本模块只汇总各步骤已有的确定性验证结果，并检查
WorkPlan 级别的交付完整性。
"""
from __future__ import annotations

from pathlib import PurePosixPath
from typing import Any

from app.observability import metrics

from .schemas import (
    WorkArtifactRef, WorkCheck, WorkPlanRecord, WorkStepOutcome, WorkVerificationResult,
)

_FILE_SUFFIXES = {
    ".docx", ".xlsx", ".pptx", ".pdf", ".md", ".txt", ".csv", ".json",
    ".zip", ".html", ".png", ".jpg", ".jpeg",
}
_IGNORE_SUFFIXES = {"research_sources", "comparison_analysis", "analysis", "report"}


def _deliverable_is_file(value: str) -> bool:
    text = str(value or "").strip().casefold()
    if not text or text in _IGNORE_SUFFIXES:
        return False
    if text.startswith(("document:", "project:")):
        return True
    suffix = PurePosixPath(text.replace(" ", "_")).suffix
    return suffix in _FILE_SUFFIXES


def _artifact_matches(artifact: WorkArtifactRef, deliverable: str) -> bool:
    name = (artifact.name or artifact.path).casefold()
    target = deliverable.casefold().strip()
    return target in name or name.endswith(target) or artifact.path.casefold().endswith(target)


def final_verify(
    record: WorkPlanRecord,
    outcomes: dict[str, WorkStepOutcome],
) -> WorkVerificationResult:
    checks: list[WorkCheck] = []
    artifacts: list[WorkArtifactRef] = []
    evidence: list[str] = []
    unresolved = False

    steps = record.plan.steps
    failed_steps = [
        step.id for step in steps
        if (record.steps.get(step.id).status if record.steps.get(step.id) else "pending")
        in {"failed", "skipped", "needs_reconciliation", "waiting_approval", "running", "pending"}
    ]
    checks.append(WorkCheck(
        id="all_steps_completed", passed=not failed_steps,
        detail="" if not failed_steps else "未完成步骤：" + "、".join(failed_steps[:8]),
    ))

    for step in steps:
        outcome = outcomes.get(step.id)
        state = record.steps.get(step.id)
        if outcome is not None:
            for item in outcome.artifact_refs:
                if all(existing.path != item.path for existing in artifacts):
                    artifacts.append(item)
            for item in outcome.evidence_refs:
                if item not in evidence:
                    evidence.append(item)
            if outcome.verification.stop_reason == "external_operation":
                unresolved = True
        if state is not None and state.status == "completed":
            for item in state.artifact_refs:
                if all(existing.path != item.path for existing in artifacts):
                    artifacts.append(item)
            for item in state.evidence_refs:
                if item not in evidence:
                    evidence.append(item)
        if state is not None and state.verification and state.verification.stop_reason == "external_operation":
            unresolved = True
        if state is not None and state.status == "completed":
            for deliverable in step.deliverables:
                if not _deliverable_is_file(deliverable):
                    continue
                if not any(_artifact_matches(item, deliverable) for item in artifacts):
                    checks.append(WorkCheck(
                        id=f"deliverable:{step.id}", passed=False,
                        detail=f"缺少声明的交付物 {deliverable}",
                    ))
                    break

    checks.append(WorkCheck(
        id="external_operation", passed=not unresolved,
        detail="存在未核对的外部副作用，不能自动确认完成" if unresolved else "",
    ))
    requires_artifacts = any(
        step.executor_profile in {"document_docx", "software_project"}
        or any(_deliverable_is_file(item) for item in step.deliverables)
        for step in steps
    )
    checks.append(WorkCheck(
        id="artifacts_present", passed=(not requires_artifacts) or bool(artifacts),
        detail="" if (not requires_artifacts or artifacts)
        else "最终计划没有产生可下载/可展示的交付物",
    ))

    failed = [item for item in checks if not item.passed]
    status = "passed"
    if any(item.id in {"all_steps_completed", "external_operation", "artifacts_present"} and not item.passed
           for item in checks):
        status = "failed"
    elif failed:
        status = "partial"
    result = WorkVerificationResult(
        status=status, checks=checks, evidence_refs=evidence[:16],
        artifact_refs=artifacts[:24],
        repairable=False,
        stop_reason="external_operation" if unresolved else ("step_failed" if failed_steps else ""),
    )
    metrics.inc_work_plan_verification(status)
    return result


def plan_digest(record: WorkPlanRecord) -> dict[str, Any]:
    verification = record.final_verification
    return {
        "plan_id": record.plan.plan_id,
        "revision": record.plan.revision,
        "mode": record.mode,
        "status": record.status,
        "step_count": record.step_count,
        "completed_count": record.completed_count,
        "repairs": record.repairs,
        "steps": [
            {
                "id": step.id,
                "title": step.title,
                "executor_profile": step.executor_profile,
                "status": (record.steps.get(step.id).status if record.steps.get(step.id) else "pending"),
                "summary": (record.steps.get(step.id).summary if record.steps.get(step.id) else ""),
                "artifact_count": len((record.steps.get(step.id).artifact_refs if record.steps.get(step.id) else [])),
            }
            for step in record.plan.steps
        ],
        "verification_status": verification.status if verification else "unknown",
    }


def compose_final_text(record: WorkPlanRecord, outcomes: dict[str, WorkStepOutcome]) -> str:
    """最终交付文本：优先最后一个已完成文档/分析步骤的实际输出。"""
    ordered = [step for step in record.plan.steps if (record.steps.get(step.id).status
                                                       if record.steps.get(step.id) else "") == "completed"]
    if not ordered:
        return ""
    last = ordered[-1]
    outcome = outcomes.get(last.id)
    if outcome is not None and outcome.final_text.strip():
        return outcome.final_text.strip()
    lines = ["工作计划已完成："]
    for step in record.plan.steps:
        state = record.steps.get(step.id)
        status = state.status if state else "pending"
        lines.append(f"- {step.title}：{'完成' if status == 'completed' else status}")
        if state and state.summary:
            lines.append(f"  {state.summary}")
    return "\n".join(lines)
