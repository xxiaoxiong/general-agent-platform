"""Work Planner：一次轻量、非流式、低温的结构化规划调用。

任务书 §5.3 / §5.4：
- Planner 只规划，不调用工具；
- 输入是目标、已校验 Decision、必要上下文引用、可用执行器与已授权能力；
- 输出严格 WorkPlan JSON，有超时、输出 Token 上限与一次格式修复机会；
- 超时/非法/依赖有环/执行器不存在时返回 fallback，由调用方安全回退原路线；
- 不把完整历史会话或大文件正文塞进规划提示。
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

from pydantic import ValidationError

from app.core.config import settings
from app.observability.langfuse_tracing import platform_span

from .registry import ExecutorRegistry
from .schemas import MAX_STEPS, PlannerMetadata, WorkPlan

logger = logging.getLogger(__name__)

HistoryTurn = dict[str, Any]

_SYSTEM = """你是 General Agent Platform 的工作规划器。你只把用户目标拆解为有限、可验证的工作步骤，不执行任务、不调用工具、不授予权限。

## 规划原则
1. 只为真正需要跨能力域、跨执行引擎的复合目标规划；单一专业任务不要拆成多步。
2. 步骤数量 2 到 {max_steps} 个，按依赖关系串行组织；不要为了"看起来完整"而拆步骤。
3. 每个步骤必须有独立、可执行、可验收的目标；步骤之间通过 input_refs 传递必要成果。
4. executor_profile 只能从「可用执行器」中选择；禁止输出工具名、Python 类名、API URL、工作区路径或用户权限。
5. input_refs 只能引用候选上下文（message_i / artifact_<id> / attachment_i / preview_document）或前置步骤产物（step:<step_id>）。
6. required_capabilities 只能从「已授权能力」中选择；没有授权的能力不得写入计划。
7. 用户明确指定的交付格式、语言、篇幅、来源要求是硬约束，必须写入 constraints/acceptance_criteria，不得被模型建议覆盖。
8. 文档交付步骤使用 document_docx；资料检索使用 research；统计分析使用 data_analysis；数据库查询使用 database；代码任务使用 coding；外部副作用使用 business_action；其余使用 general_agent。
9. 只有显式开启团队协作时才可使用 team；只有软件项目才可使用 software_project。
10. 不要把完整历史、附件正文或源码写进 objective；只写该步骤需要完成什么。
11. 用户明确给出「先研究/调研 → 再分析/比较 → 最后生成文档/报告」三阶段时，分别建立研究、分析、文档三个步骤，不要把分析合并进研究步骤。

## 输出
只输出一个 JSON 对象，不要 Markdown 代码块、不要解释。结构：
{{"schema_version":"v1","objective":"总体目标","constraints":["..."],
 "context_refs":["message_0"],"deliverables":["..."],"acceptance_criteria":["..."],
 "steps":[{{"id":"research","title":"资料检索与核验","objective":"...","executor_profile":"research",
   "depends_on":[],"input_refs":[],"required_capabilities":[],"deliverables":["research_sources"],
   "acceptance_criteria":["关键结论有可核验来源"]}}]}}
步骤 ID 只能使用小写字母开头、字母/数字/下划线/短横线，且全局唯一。
"""


@dataclass
class PlannerOutcome:
    plan: WorkPlan | None
    latency_ms: int = 0
    fallback: bool = True
    error: str = ""
    repair_attempted: bool = False
    model_id: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    raw_excerpt: str = field(default="", repr=False)


class PlannerParseError(ValueError):
    pass


def _extract_json(raw: str) -> dict[str, Any] | None:
    text = (raw or "").strip()
    if not text:
        return None
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    if fenced:
        text = fenced.group(1)
    else:
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            return None
        text = text[start:end + 1]
    try:
        value = json.loads(text)
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def parse_plan(raw: str) -> WorkPlan:
    payload = _extract_json(raw)
    if payload is None:
        raise PlannerParseError("输出不是合法 JSON 对象")
    try:
        return WorkPlan.model_validate(payload)
    except ValidationError as exc:
        first = exc.errors()[0] if exc.errors() else {}
        location = ".".join(str(item) for item in first.get("loc") or ())
        message = str(first.get("msg") or "schema 校验失败")[:200]
        raise PlannerParseError(f"{location}: {message}" if location else message) from None


def _text_of(response: Any) -> str:
    content = getattr(response, "content", "")
    if isinstance(content, list):
        content = "".join(
            part.get("text", "") if isinstance(part, dict) else str(part) for part in content
        )
    return str(content or "")


def _usage_of(response: Any) -> tuple[int, int]:
    usage = getattr(response, "usage_metadata", None) or {}
    if not usage:
        metadata = getattr(response, "response_metadata", None) or {}
        usage = metadata.get("token_usage") or metadata.get("usage") or {}
    if not isinstance(usage, dict):
        usage = {}
    try:
        return (
            int(usage.get("input_tokens") or usage.get("prompt_tokens") or 0),
            int(usage.get("output_tokens") or usage.get("completion_tokens") or 0),
        )
    except (TypeError, ValueError):
        return 0, 0


async def invoke_planner_model(
    provider: dict[str, Any],
    model_config: dict[str, Any],
    prompt: str,
    *,
    max_tokens: int | None = None,
) -> tuple[str, int, int]:
    from app.services.agent_runtime.model_adapter import build_chat_model

    chat = build_chat_model(
        provider,
        model_config,
        streaming=False,
        temperature=0,
        max_tokens=int(max_tokens or settings.AGENT_WORK_PLAN_MAX_TOKENS),
        require_tools=False,
    )
    response = await chat.ainvoke([
        {"role": "system", "content": _SYSTEM.format(max_steps=MAX_STEPS)},
        {"role": "user", "content": prompt},
    ])
    input_tokens, output_tokens = _usage_of(response)
    return _text_of(response), input_tokens, output_tokens


def _clip(text: Any, limit: int) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()[:limit]


def render_planner_prompt(
    *,
    message: str,
    decision: dict[str, Any] | None,
    history: Iterable[HistoryTurn] = (),
    artifacts: Iterable[dict[str, Any]] = (),
    attachments: Iterable[dict[str, Any]] = (),
    preview_document: dict[str, Any] | None = None,
    registry: ExecutorRegistry,
    extra_constraints: Iterable[str] = (),
) -> str:
    """渲染规划输入（有界裁剪；附件正文/源码不进入提示）。"""
    decision = decision or {}
    lines: list[str] = [f"## 本轮用户目标\n{_clip(message, 4000)}"]
    goal = _clip(decision.get("goal"), 500)
    if goal:
        lines.append(f"\n## 已校验目标\n- {goal}")
    intent = _clip(decision.get("intent"), 60)
    if intent:
        lines.append(f"- 意图：{intent}")
    fmt = _clip(decision.get("deliverable_format"), 20)
    if fmt and fmt != "none":
        lines.append(f"- 交付格式：{fmt}")
    constraints = [str(item) for item in (decision.get("constraints") or []) if str(item).strip()]
    constraints.extend(str(item) for item in extra_constraints if str(item).strip())
    if constraints:
        lines.append("\n## 用户约束（硬约束）")
        lines.extend(f"- {_clip(item, 300)}" for item in constraints[:8])
    deliverables = [str(item) for item in (decision.get("deliverables") or []) if str(item).strip()]
    if deliverables:
        lines.append("\n## 交付物")
        lines.extend(f"- {_clip(item, 300)}" for item in deliverables[:8])
    acceptance = [str(item) for item in (decision.get("acceptance_criteria") or []) if str(item).strip()]
    if acceptance:
        lines.append("\n## 验收要求")
        lines.extend(f"- {_clip(item, 300)}" for item in acceptance[:8])

    turns = [item for item in history if isinstance(item, dict)][-6:]
    if turns:
        lines.append("\n## 最近对话（候选引用 message_i，仅作上下文）")
        budget = 3600
        for index, item in enumerate(turns):
            if budget <= 0:
                break
            content = _clip(str(item.get("content") or "").split("\n\n[附件]", 1)[0], min(600, budget))
            if not content:
                continue
            budget -= len(content)
            role = "用户" if item.get("role") == "user" else "助手"
            lines.append(f"- [message_{index}] {role}：{content}")
    else:
        lines.append("\n## 最近对话\n- （无历史；[message_current] 或 [message_0] 均指本轮用户请求）")

    produced = [item for item in artifacts if isinstance(item, dict)][:5]
    if produced:
        lines.append("\n## 会话文档产物（候选引用 artifact_<id>）")
        for item in produced:
            if item.get("document_id") is None:
                continue
            lines.append(
                f"- [artifact_{item['document_id']}]《{_clip(item.get('title'), 80)}》"
                f" 版本={item.get('version_no')}"
            )
    files = [item for item in attachments if isinstance(item, dict)][:8]
    if files:
        lines.append("\n## 本轮附件（候选引用 attachment_i，只提供元信息）")
        for index, item in enumerate(files):
            lines.append(f"- [attachment_{index}] {_clip(item.get('name') or item.get('path'), 160)}")
    if preview_document and preview_document.get("document_id") is not None:
        lines.append(
            f"\n## 当前预览文档（候选引用 preview_document）\n"
            f"- 《{_clip(preview_document.get('title'), 80)}》 版本={preview_document.get('version_no')}"
        )

    lines.append("\n" + registry.render_prompt())
    lines.append(
        "\n## 上下文引用规则\n"
        "- input_refs 只能使用 message_current / message_i / artifact_<id> / attachment_i / preview_document / step:<已声明步骤 ID>。\n"
        "- 不要编造 ID、路径、URL 或工具名。\n"
        "- 后续步骤如果需要前序成果，写 input_refs=[\"step:<前序步骤ID>\"]。"
    )
    return "\n".join(lines)


def _repair_prompt(prompt: str, raw: str, error: str) -> str:
    return (
        f"{prompt}\n\n## 上一次输出无法解析\n错误：{error[:300]}\n"
        f"原始输出（截断）：{raw[:1500]}\n"
        "请严格按系统提示的 JSON 结构重新输出一次，只输出 JSON 对象，不要代码块或解释。"
    )


async def plan_work(
    *,
    message: str,
    decision: dict[str, Any] | None,
    history: Iterable[HistoryTurn],
    artifacts: Iterable[dict[str, Any]],
    attachments: Iterable[dict[str, Any]],
    preview_document: dict[str, Any] | None,
    registry: ExecutorRegistry,
    provider: dict[str, Any],
    model_config: dict[str, Any],
    run_id: str = "",
    extra_constraints: Iterable[str] = (),
    timeout: float | None = None,
    max_tokens: int | None = None,
    meter: Callable[[int, int], None] | None = None,
) -> PlannerOutcome:
    """执行一次规划；任何失败返回 fallback，不抛到 Run 主链路。"""
    started = time.monotonic()
    budget = float(timeout if timeout is not None else settings.AGENT_WORK_PLAN_TIMEOUT)
    model_id = str(model_config.get("name") or "")
    prompt = render_planner_prompt(
        message=message, decision=decision, history=history, artifacts=artifacts,
        attachments=attachments, preview_document=preview_document,
        registry=registry, extra_constraints=extra_constraints,
    )
    raw = ""
    error = ""
    repair_attempted = False
    total_input_tokens = 0
    total_output_tokens = 0
    with platform_span(
        "work_plan.model_call", run_id=run_id,
        attributes={"model_id": model_id, "available_executors": len(registry.allowed_profiles())},
    ) as span:
        for attempt in range(2):
            remaining = budget - (time.monotonic() - started)
            if remaining <= 1.0:
                error = error or "规划超时"
                break
            try:
                if attempt == 0:
                    raw, input_tokens, output_tokens = await asyncio.wait_for(
                        invoke_planner_model(provider, model_config, prompt, max_tokens=max_tokens),
                        timeout=remaining,
                    )
                else:
                    repair_attempted = True
                    raw, input_tokens, output_tokens = await asyncio.wait_for(
                        invoke_planner_model(
                            provider, model_config,
                            _repair_prompt(prompt, raw, error), max_tokens=max_tokens,
                        ),
                        timeout=max(1.0, remaining),
                    )
                total_input_tokens += input_tokens
                total_output_tokens += output_tokens
                if meter is not None:
                    try:
                        meter(input_tokens, output_tokens)
                    except Exception:  # noqa: BLE001 计量失败不影响规划
                        pass
            except asyncio.TimeoutError:
                error = f"规划模型超时（>{budget:.1f}s）"
                break
            except Exception as exc:  # noqa: BLE001 模型不可用时必须继续可用
                error = f"规划模型调用失败: {exc}"
                break
            try:
                plan = parse_plan(raw)
            except PlannerParseError as exc:
                error = str(exc)
                if attempt == 0:
                    continue
                break
            latency_ms = int((time.monotonic() - started) * 1000)
            plan = plan.model_copy(update={
                "planner_metadata": PlannerMetadata(
                    model_id=model_id, latency_ms=latency_ms, fallback=False,
                    repair_attempted=repair_attempted,
                    input_tokens=total_input_tokens, output_tokens=total_output_tokens,
                ),
            })
            if span is not None:
                try:
                    span.update(output={
                        "plan_id": plan.plan_id, "step_count": len(plan.steps),
                        "latency_ms": latency_ms, "repair_attempted": repair_attempted,
                    })
                except Exception:  # noqa: BLE001
                    pass
            return PlannerOutcome(
                plan=plan, latency_ms=latency_ms, fallback=False,
                repair_attempted=repair_attempted, model_id=model_id,
                input_tokens=total_input_tokens, output_tokens=total_output_tokens,
            )

    latency_ms = int((time.monotonic() - started) * 1000)
    logger.warning("run %s WorkPlan 规划降级：%s", run_id or "-", error or "未知原因")
    if span is not None:
        try:
            span.update(output={"fallback": True, "error": error[:200], "latency_ms": latency_ms})
        except Exception:  # noqa: BLE001
            pass
    return PlannerOutcome(
        plan=None, latency_ms=latency_ms, fallback=True, error=error or "规划失败",
        repair_attempted=repair_attempted, model_id=model_id,
        input_tokens=total_input_tokens, output_tokens=total_output_tokens,
        raw_excerpt=raw[:300],
    )
