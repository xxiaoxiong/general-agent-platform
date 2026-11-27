"""WorkPlan Validator：模型计划必须收敛到服务端可执行边界（任务书 §4.4）。

只信任服务端事实：
- executor_profile 必须在 Registry 中且对当前 Agent 可用；
- 依赖图必须是已声明步骤、无自依赖、无环、只引用前序步骤；
- input_refs 必须来自 DecisionContext 候选引用或前序步骤产物；
- required_capabilities 必须来自当前助手已授权能力；
- 文本/数组/总大小有硬上限；不信任模型自报权限、URL、路径或工具名。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable

from app.core.config import settings

from .registry import ExecutorRegistry
from .schemas import MAX_PLAN_BYTES, WorkPlan

_ALLOWED_STEP_REF_PREFIX = "step:"
_STEP_ID_PATTERN = r"^[A-Za-z][A-Za-z0-9_-]*$"


@dataclass
class ValidatedWorkPlan:
    plan: WorkPlan
    valid: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    context_refs: list[str] = field(default_factory=list)


def _cycle_free(plan: WorkPlan) -> tuple[bool, str]:
    index = {step.id: position for position, step in enumerate(plan.steps)}
    for position, step in enumerate(plan.steps):
        for dep in step.depends_on:
            if dep == step.id:
                return False, f"步骤 {step.id} 不能依赖自身"
            if dep not in index:
                return False, f"步骤 {step.id} 依赖未定义的步骤 {dep}"
            if index[dep] >= position:
                return False, f"步骤 {step.id} 只能依赖前序步骤（{dep} 在其后面）"
    return True, ""


def _seen_urls(text: str) -> set[str]:
    import re

    return {
        match.rstrip(".,;)]}").casefold()
        for match in re.findall(r"https?://[^\s<>\"'）)。，；]+", text or "", re.I)
    }


def validate_plan(
    plan: WorkPlan,
    *,
    registry: ExecutorRegistry,
    context_refs: Iterable[str] = (),
    user_message: str = "",
    max_steps: int | None = None,
) -> ValidatedWorkPlan:
    errors: list[str] = []
    warnings: list[str] = []
    limit = int(max_steps or getattr(settings, "AGENT_WORK_MAX_STEPS", 6) or 6)
    limit = max(1, min(limit, 6))

    if len(plan.steps) > limit:
        errors.append(f"步骤数量 {len(plan.steps)} 超过上限 {limit}")
    if plan.byte_size() > MAX_PLAN_BYTES:
        errors.append(f"计划体积超过上限 {MAX_PLAN_BYTES} 字节")
    if not plan.steps:
        errors.append("计划至少需要一个步骤")

    ok, reason = _cycle_free(plan)
    if not ok:
        errors.append(reason)

    candidates = {str(item) for item in context_refs if item}
    declared: dict[str, int] = {step.id: index for index, step in enumerate(plan.steps)}
    valid_step_refs: list[str] = []
    for position, step in enumerate(plan.steps):
        if not re.match(_STEP_ID_PATTERN, step.id):
            errors.append(f"步骤 ID 不合法：{step.id}")
        resolved = registry.resolve(step.executor_profile)
        if not resolved.allowed:
            errors.append(f"步骤 {step.id} 执行器不可用：{step.executor_profile}（{resolved.reason}）")
        allowed_caps, denied_caps = registry.capability_allowed(step.required_capabilities)
        if denied_caps:
            # 未授权能力不得进入执行计划：服务端直接丢弃并告警（不因此让整个计划失败）
            warnings.append(
                f"步骤 {step.id} 请求的未授权能力已丢弃：{', '.join(denied_caps[:4])}"
            )
            plan.steps[position].required_capabilities = allowed_caps
        for ref in step.input_refs:
            if ref.startswith(_ALLOWED_STEP_REF_PREFIX):
                dep_id = ref[len(_ALLOWED_STEP_REF_PREFIX):]
                if dep_id not in declared:
                    errors.append(f"步骤 {step.id} 引用了不存在的步骤产物 {ref}")
                elif declared[dep_id] >= position:
                    errors.append(f"步骤 {step.id} 只能引用前序步骤产物 {ref}")
                else:
                    valid_step_refs.append(ref)
            elif ref not in candidates:
                errors.append(f"步骤 {step.id} 引用了不存在的上下文 {ref}")
        if not step.acceptance_criteria:
            errors.append(f"步骤 {step.id} 缺少可核对的验收条件")
        if len(step.depends_on) > limit:
            errors.append(f"步骤 {step.id} 依赖数量超过上限")
        # 模型不得把工作区路径/宿主机路径当作执行指令
        for text in (step.objective, *step.deliverables):
            if "/workspace/" in text or "\\workspace\\" in text:
                errors.append(f"步骤 {step.id} 不能指定工作区路径")
                break
        # 模型新增的 URL 不能当作已核验资源（用户消息里出现过的除外）
        user_urls = _seen_urls(user_message)
        for text in (step.objective, *step.deliverables):
            extra = _seen_urls(text) - user_urls
            if extra:
                errors.append(f"步骤 {step.id} 不能引入未授权 URL：{', '.join(sorted(extra)[:2])}")
                break

    for ref in plan.context_refs:
        if ref not in candidates:
            errors.append(f"计划引用了不存在的上下文 {ref}")

    if len(plan.steps) == 1:
        warnings.append("单步骤计划不会启用跨引擎编排（直接执行更高效）")

    return ValidatedWorkPlan(
        plan=plan,
        valid=not errors,
        errors=list(dict.fromkeys(errors))[:12],
        warnings=warnings,
        context_refs=[ref for ref in plan.context_refs if ref in candidates] + valid_step_refs,
    )


def candidate_refs_from_context(
    *,
    history: Iterable[dict[str, Any]] = (),
    artifacts: Iterable[dict[str, Any]] = (),
    attachments: Iterable[dict[str, Any]] = (),
    preview_document: dict[str, Any] | None = None,
) -> list[str]:
    """与决策上下文/规划提示一致的候选引用 ID。"""
    refs: list[str] = []
    turns = [item for item in history if isinstance(item, dict)]
    # Context Builder 取最近 8 轮并重新编号；本轮请求固定使用 message_current，
    # 无历史时 message_0 也指向本轮请求（模型常见的自然写法）。
    if not turns:
        refs.append("message_0")
    for index, _ in enumerate(turns[-8:]):
        refs.append(f"message_{index}")
    refs.append("message_current")
    for item in artifacts:
        if isinstance(item, dict) and item.get("document_id") is not None:
            refs.append(f"artifact_{item['document_id']}")
    for index, item in enumerate(attachments):
        if isinstance(item, dict):
            refs.append(f"attachment_{index}")
    if preview_document and preview_document.get("document_id") is not None:
        refs.append("preview_document")
    return list(dict.fromkeys(refs))
