"""Retrieval Planner：模型驱动的有界历史检索判断（非关键词硬编码）。

- 输入：当前请求 + 最近对话摘要 + 服务端项目上下文；不输入完整历史正文；
- 输出：need_history / scope_hint / queries / reason（严格 JSON）；
- 超时、调用失败或两次输出非法 → 安全降级为“不检索”，绝不拖垮 Run；
- scope_hint 只是检索意图，服务端会重新计算可访问范围（不能作为授权）。
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.core.config import settings
from app.observability.langfuse_tracing import platform_span

logger = logging.getLogger("ai_plat.conversation_memory")

SYSTEM_PROMPT = (
    "你是历史会话检索规划器。你的唯一任务是判断：为了正确完成用户当前请求，"
    "是否需要检索用户过去的会话记录，并生成少量检索查询。\n\n"
    "判断原则：\n"
    "1. 用户明确表示续接、对比、沿用过去讨论（例如继续/之前/上次/那个方案），通常需要历史；\n"
    "2. 用户没有说“之前”但请求依赖既有偏好、约定、技术决策或项目背景时，也需要历史；\n"
    "3. 普通知识问答、闲聊、独立计算、一次性写作，不需要历史；\n"
    "4. 不要因为出现某个词就机械判断，必须结合语义；也不要每轮都检索；\n"
    "5. queries 应为 1~3 条自然语言检索语句，覆盖用户真正需要的背景（决策、约束、方案）；\n"
    "6. 不要编造历史内容；你只负责提出检索意图。\n\n"
    "只输出严格 JSON，不要代码块、解释或额外字段：\n"
    '{"need_history": true|false, "scope_hint": "current_project"|"user_history"|"all", '
    '"queries": ["..."], "reason": "不超过 120 字的客观理由"}'
)


class RetrievalPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    need_history: bool = False
    scope_hint: Literal["current_project", "user_history", "all"] = "user_history"
    queries: List[str] = Field(default_factory=list, max_length=3)
    reason: str = Field(default="", max_length=300)


class RetrievalPlanParseError(ValueError):
    pass


@dataclass
class PlannerOutcome:
    plan: Optional[RetrievalPlan] = None
    latency_ms: int = 0
    fallback: bool = True
    error: str = ""
    repair_attempted: bool = False
    model_id: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    raw_excerpt: str = field(default="", repr=False)


def _extract_json(raw: str) -> Optional[Dict[str, Any]]:
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
        payload = json.loads(text)
    except (TypeError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def parse_plan(raw: str) -> RetrievalPlan:
    payload = _extract_json(raw)
    if payload is None:
        raise RetrievalPlanParseError("输出不是合法 JSON 对象")
    try:
        plan = RetrievalPlan.model_validate(payload)
    except ValidationError as exc:
        first = exc.errors()[0] if exc.errors() else {}
        location = ".".join(str(item) for item in first.get("loc") or ())
        message = str(first.get("msg") or "schema 校验失败")[:200]
        raise RetrievalPlanParseError(f"{location}: {message}" if location else message) from None
    queries: List[str] = []
    for query in plan.queries:
        text = " ".join(str(query or "").split())[:160]
        if text and text not in queries:
            queries.append(text)
    plan.queries = queries[:3]
    if plan.need_history and not plan.queries:
        raise RetrievalPlanParseError("need_history=true 但 queries 为空")
    if not plan.need_history:
        plan.queries = []
    plan.reason = str(plan.reason or "").strip()[:300]
    return plan


def _message_text(message: Dict[str, Any]) -> str:
    content = message.get("content")
    if isinstance(content, list):
        content = "".join(
            str(item.get("text") if isinstance(item, dict) else item) for item in content
        )
    return " ".join(str(content or "").split())


def current_request(messages: Optional[List[Dict[str, Any]]]) -> str:
    for message in reversed(messages or []):
        if isinstance(message, dict) and message.get("role") == "user":
            text = _message_text(message)
            if text:
                return text[:2000]
    return ""


def recent_excerpt(messages: Optional[List[Dict[str, Any]]], limit: int = 6) -> str:
    rows: List[str] = []
    for message in (messages or [])[-limit:]:
        if not isinstance(message, dict):
            continue
        role = "用户" if message.get("role") == "user" else "助手"
        text = _message_text(message)
        if not text:
            continue
        rows.append(f"{role}：{text[:400]}")
    return "\n".join(rows)


def render_planner_prompt(
    *, request: str, recent: str, project_context: str, history_hint: str,
) -> str:
    return (
        f"## 当前请求\n{request or '(空)'}\n\n"
        f"## 最近对话（仅供判断，不是完整历史）\n{recent or '(无)'}\n\n"
        f"## 服务端上下文\n{project_context or '(非项目会话)'}\n"
        f"{history_hint}\n\n"
        "请判断是否需要检索历史会话，并按系统提示的 JSON 格式输出。"
    )


def _text_of(response: Any) -> str:
    content = getattr(response, "content", "")
    if isinstance(content, list):
        content = "".join(
            part.get("text", "") if isinstance(part, dict) else str(part) for part in content
        )
    return str(content or "")


def _usage_of(response: Any) -> tuple:
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


async def _invoke(
    provider: Dict[str, Any], model_config: Dict[str, Any], prompt: str,
) -> tuple:
    from app.services.agent_runtime.model_adapter import build_chat_model

    chat = build_chat_model(
        provider,
        model_config,
        streaming=False,
        temperature=0,
        max_tokens=int(getattr(settings, "CONVERSATION_MEMORY_PLANNER_MAX_TOKENS", 400) or 400),
        require_tools=False,
    )
    response = await chat.ainvoke([
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": prompt},
    ])
    input_tokens, output_tokens = _usage_of(response)
    return _text_of(response), input_tokens, output_tokens


def _repair_prompt(prompt: str, raw: str, error: str) -> str:
    return (
        f"{prompt}\n\n## 上一次输出无法解析\n错误：{error[:300]}\n"
        f"原始输出（截断）：{raw[:800]}\n"
        "请严格按系统提示的 JSON 字段重新输出一次，只输出 JSON 对象。"
    )


async def plan_retrieval(
    *,
    provider: Dict[str, Any],
    model_config: Dict[str, Any],
    request: str,
    recent: str = "",
    project_context: str = "",
    history_hint: str = "",
    run_id: str = "",
    timeout: Optional[float] = None,
) -> PlannerOutcome:
    """执行一次有界规划；任何失败返回 fallback（need_history=false）。"""
    started = time.monotonic()
    budget = float(
        timeout if timeout is not None
        else getattr(settings, "CONVERSATION_MEMORY_PLANNER_TIMEOUT", 12.0) or 12.0
    )
    model_id = str(model_config.get("name") or "")
    prompt = render_planner_prompt(
        request=request, recent=recent, project_context=project_context,
        history_hint=history_hint,
    )
    raw = ""
    error = ""
    repair_attempted = False
    total_input = 0
    total_output = 0
    with platform_span(
        "conversation_memory.planner", run_id=run_id, attributes={"model_id": model_id},
    ) as span:
        for attempt in range(2):
            remaining = budget - (time.monotonic() - started)
            if remaining <= 1.0:
                error = error or "检索规划超时"
                break
            try:
                if attempt == 0:
                    raw, input_tokens, output_tokens = await asyncio.wait_for(
                        _invoke(provider, model_config, prompt), timeout=remaining,
                    )
                else:
                    repair_attempted = True
                    raw, input_tokens, output_tokens = await asyncio.wait_for(
                        _invoke(provider, model_config, _repair_prompt(prompt, raw, error)),
                        timeout=max(1.0, remaining),
                    )
                total_input += input_tokens
                total_output += output_tokens
            except asyncio.TimeoutError:
                error = f"检索规划模型超时（>{budget:.1f}s）"
                break
            except Exception as exc:  # noqa: BLE001 规划不可用必须继续可用
                error = f"检索规划模型调用失败: {exc}"
                break
            try:
                plan = parse_plan(raw)
            except RetrievalPlanParseError as exc:
                error = str(exc)
                if attempt == 0:
                    continue
                break
            latency_ms = int((time.monotonic() - started) * 1000)
            try:
                if span is not None:
                    span.update(output={
                        "need_history": plan.need_history,
                        "scope_hint": plan.scope_hint,
                        "query_count": len(plan.queries),
                        "latency_ms": latency_ms,
                    })
            except Exception:  # noqa: BLE001
                pass
            return PlannerOutcome(
                plan=plan, latency_ms=latency_ms, fallback=False,
                repair_attempted=repair_attempted, model_id=model_id,
                input_tokens=total_input, output_tokens=total_output,
            )
    latency_ms = int((time.monotonic() - started) * 1000)
    logger.warning("历史检索规划降级（不检索）：%s", error or "未知原因")
    try:
        if span is not None:
            span.update(output={"fallback": True, "error": error[:200], "latency_ms": latency_ms})
    except Exception:  # noqa: BLE001
        pass
    return PlannerOutcome(
        plan=None, latency_ms=latency_ms, fallback=True, error=error or "规划失败",
        repair_attempted=repair_attempted, model_id=model_id,
        input_tokens=total_input, output_tokens=total_output, raw_excerpt=raw[:300],
    )
