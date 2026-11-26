"""LLM Structured Decision：一次轻量、非流式、低温的结构化决策调用。

性能与降级（任务书 §5.2 / §5.3）：
- 每个新 Auto Run 最多一次正常调用；只有 JSON/Schema 解析失败才允许一次修复重试；
- 超时、服务不可用或两次输出都非法时返回 ``decision=None``，由调用方安全降级；
- 不记录隐藏推理链，只保留可审计的短理由与解析错误摘要。
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any

from pydantic import ValidationError

from app.core.config import settings
from app.observability.langfuse_tracing import platform_span

from .context import DecisionContext, SYSTEM_PROMPT, render_decision_prompt
from .schemas import RunDecision

logger = logging.getLogger(__name__)


class DecisionParseError(ValueError):
    """模型输出不是合法的 RunDecision JSON。"""


@dataclass
class DecisionOutcome:
    decision: RunDecision | None
    latency_ms: int = 0
    fallback: bool = True
    error: str = ""
    repair_attempted: bool = False
    model_id: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    raw_excerpt: str = field(default="", repr=False)


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


def parse_decision(raw: str) -> RunDecision:
    """解析并严格校验模型输出；非法时抛 DecisionParseError（供一次修复重试）。"""
    payload = _extract_json(raw)
    if payload is None:
        raise DecisionParseError("输出不是合法 JSON 对象")
    try:
        return RunDecision.model_validate(payload)
    except ValidationError as exc:
        first = exc.errors()[0] if exc.errors() else {}
        location = ".".join(str(item) for item in first.get("loc") or ())
        message = str(first.get("msg") or "schema 校验失败")[:200]
        raise DecisionParseError(f"{location}: {message}" if location else message) from None


def _text_of(response: Any) -> str:
    content = getattr(response, "content", "")
    if isinstance(content, list):
        content = "".join(
            part.get("text", "") if isinstance(part, dict) else str(part) for part in content
        )
    return str(content or "")


def _usage_of(response: Any) -> tuple[int, int]:
    """提取模型 Token 用量（不同 Provider 字段名不同，缺失时返回 0）。"""
    usage = getattr(response, "usage_metadata", None) or {}
    if not usage:
        metadata = getattr(response, "response_metadata", None) or {}
        usage = metadata.get("token_usage") or metadata.get("usage") or {}
    if not isinstance(usage, dict):
        usage = {}
    input_tokens = usage.get("input_tokens") or usage.get("prompt_tokens") or 0
    output_tokens = usage.get("output_tokens") or usage.get("completion_tokens") or 0
    try:
        return int(input_tokens), int(output_tokens)
    except (TypeError, ValueError):
        return 0, 0


async def invoke_decision_model(
    provider: dict[str, Any],
    model_config: dict[str, Any],
    prompt: str,
    *,
    max_tokens: int | None = None,
) -> tuple[str, int, int]:
    """调用当前对话模型做一次非流式决策（不要求工具能力）。"""
    from app.services.agent_runtime.model_adapter import build_chat_model

    chat = build_chat_model(
        provider,
        model_config,
        streaming=False,
        temperature=0,
        max_tokens=int(max_tokens or settings.AGENT_DECISION_MAX_TOKENS),
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
        f"原始输出（截断）：{raw[:1200]}\n"
        "请严格按系统提示的 JSON 字段重新输出一次，只输出 JSON 对象，不要代码块或解释。"
    )


async def decide(
    context: DecisionContext,
    *,
    provider: dict[str, Any],
    model_config: dict[str, Any],
    run_id: str = "",
    timeout: float | None = None,
    max_tokens: int | None = None,
) -> DecisionOutcome:
    """执行一次决策；任何失败都返回 fallback 结果，不抛出到 Run 主链路。"""
    started = time.monotonic()
    budget = float(timeout if timeout is not None else settings.AGENT_DECISION_TIMEOUT)
    model_id = str(model_config.get("name") or "")
    prompt = render_decision_prompt(context)
    raw = ""
    error = ""
    repair_attempted = False
    total_input_tokens = 0
    total_output_tokens = 0
    with platform_span(
        "decision.model_call",
        run_id=run_id,
        attributes={"model_id": model_id, "history_turns": len(context.history)},
    ) as span:
        for attempt in range(2):
            remaining = budget - (time.monotonic() - started)
            # 修复重试只在仍有可用时间时执行，避免把 Run 卡在决策阶段
            if remaining <= 1.0:
                error = error or "决策超时"
                break
            try:
                if attempt == 0:
                    raw, input_tokens, output_tokens = await asyncio.wait_for(
                        invoke_decision_model(
                            provider, model_config, prompt, max_tokens=max_tokens,
                        ),
                        timeout=remaining,
                    )
                else:
                    repair_attempted = True
                    raw, input_tokens, output_tokens = await asyncio.wait_for(
                        invoke_decision_model(
                            provider, model_config,
                            _repair_prompt(prompt, raw, error), max_tokens=max_tokens,
                        ),
                        timeout=max(1.0, remaining),
                    )
                total_input_tokens += input_tokens
                total_output_tokens += output_tokens
            except asyncio.TimeoutError:
                error = f"决策模型超时（>{budget:.1f}s）"
                break
            except Exception as exc:  # noqa: BLE001 模型不可用时必须继续可用
                error = f"决策模型调用失败: {exc}"
                break
            try:
                decision = parse_decision(raw)
            except DecisionParseError as exc:
                error = str(exc)
                if attempt == 0:
                    continue
                break
            latency_ms = int((time.monotonic() - started) * 1000)
            try:
                if span is not None:
                    span.update(output={
                        "intent": decision.intent, "route": decision.route,
                        "continuity": decision.continuity, "latency_ms": latency_ms,
                        "input_tokens": total_input_tokens,
                        "output_tokens": total_output_tokens,
                    })
            except Exception:  # noqa: BLE001 可观测失败不影响决策
                pass
            return DecisionOutcome(
                decision=decision,
                latency_ms=latency_ms,
                fallback=False,
                repair_attempted=repair_attempted,
                model_id=model_id,
                input_tokens=total_input_tokens,
                output_tokens=total_output_tokens,
            )

    latency_ms = int((time.monotonic() - started) * 1000)
    logger.warning("run %s 语义决策降级到规则路由：%s", run_id or "-", error or "未知原因")
    try:
        if span is not None:
            span.update(output={"fallback": True, "error": error[:200], "latency_ms": latency_ms})
    except Exception:  # noqa: BLE001
        pass
    return DecisionOutcome(
        decision=None,
        latency_ms=latency_ms,
        fallback=True,
        error=error or "决策失败",
        repair_attempted=repair_attempted,
        model_id=model_id,
        input_tokens=total_input_tokens,
        output_tokens=total_output_tokens,
        raw_excerpt=raw[:300],
    )
