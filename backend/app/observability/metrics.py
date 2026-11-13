"""Prometheus 运行指标（P1-05）。

指标清单对齐任务书 §5.6；所有记录调用均防御式（指标故障不影响执行）。
`/metrics` 端点由 main.py 挂载供 Prometheus 抓取。
"""
from __future__ import annotations

import logging
import time
from typing import Optional

logger = logging.getLogger("ai_plat.observability.metrics")

try:
    from prometheus_client import (
        CONTENT_TYPE_LATEST,
        CollectorRegistry,
        Counter,
        Gauge,
        Histogram,
        generate_latest,
    )
    _AVAILABLE = True
except Exception:  # noqa: BLE001
    _AVAILABLE = False

# 独立注册表：避免与 prometheus_client 默认全局注册表互相干扰（测试可单独抓取）
REGISTRY = CollectorRegistry() if _AVAILABLE else None

if _AVAILABLE:
    # ── Run 生命周期 ─────────────────────────────────────────────────────
    AGENT_RUNS_TOTAL = Counter(
        "agent_runs_total", "Agent 运行总数（按终态）",
        ["status", "agent_type"], registry=REGISTRY,
    )
    AGENT_RUN_DURATION = Histogram(
        "agent_run_duration_seconds", "Agent 运行总耗时",
        ["status"], registry=REGISTRY,
        buckets=(0.5, 1, 2, 5, 10, 30, 60, 120, 300, 600, 1800),
    )
    AGENT_QUEUE_WAIT = Histogram(
        "agent_queue_wait_seconds", "Run 从创建到被 Worker 领取的排队等待",
        registry=REGISTRY,
        buckets=(0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 30, 60),
    )
    AGENT_ACTIVE_RUNS = Gauge(
        "agent_active_runs", "当前执行中的 Run 数", registry=REGISTRY,
    )
    AGENT_RUN_RETRIES = Counter(
        "agent_run_retries_total", "Run 重试次数", ["reason"], registry=REGISTRY,
    )
    AGENT_RECOVERY = Counter(
        "agent_recovery_total", "恢复决策次数", ["decision"], registry=REGISTRY,
    )
    AGENT_LEASE_LOST = Counter(
        "agent_lease_lost_total", "执行权丢失（fencing）次数", registry=REGISTRY,
    )

    # ── 工具 ────────────────────────────────────────────────────────────
    AGENT_TOOL_CALLS = Counter(
        "agent_tool_calls_total", "工具调用次数", ["tool", "status"], registry=REGISTRY,
    )
    AGENT_TOOL_DURATION = Histogram(
        "agent_tool_duration_seconds", "工具调用耗时", ["tool"], registry=REGISTRY,
        buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 30, 60),
    )

    # ── 模型 ────────────────────────────────────────────────────────────
    AGENT_MODEL_CALLS = Counter(
        "agent_model_calls_total", "模型调用次数", ["provider", "model", "status"], registry=REGISTRY,
    )
    AGENT_MODEL_LATENCY = Histogram(
        "agent_model_latency_seconds", "模型调用延迟", ["provider", "model"], registry=REGISTRY,
        buckets=(0.1, 0.25, 0.5, 1, 2, 5, 10, 30, 60, 120),
    )
    AGENT_TOKENS = Counter(
        "agent_tokens_total", "Token 用量", ["provider", "model", "direction"], registry=REGISTRY,
    )

    # ── 事件流 / 存储 ───────────────────────────────────────────────────
    AGENT_SSE_RECONNECT = Counter(
        "agent_sse_reconnect_total", "SSE 断线重连（after_seq / Last-Event-ID）次数",
        registry=REGISTRY,
    )
    AGENT_EVENT_LAG = Histogram(
        "agent_event_lag_seconds", "事件产生到持久化的滞后", registry=REGISTRY,
        buckets=(0.005, 0.01, 0.05, 0.1, 0.25, 0.5, 1, 2, 5),
    )
    AGENT_CHECKPOINT_WRITE = Histogram(
        "agent_checkpoint_write_seconds", "Checkpoint 写入耗时", registry=REGISTRY,
        buckets=(0.005, 0.01, 0.05, 0.1, 0.25, 0.5, 1, 2, 5),
    )


def _safe(fn):
    """指标记录防御式包装：任何指标异常只记日志、不影响业务。"""
    def wrapper(*args, **kwargs):
        if not _AVAILABLE:
            return None
        try:
            return fn(*args, **kwargs)
        except Exception as e:  # noqa: BLE001
            logger.debug("metrics record failed: %s", e)
            return None
    return wrapper


@_safe
def observe_run_finished(status: str, duration_seconds: float, agent_type: str = "assistant") -> None:
    AGENT_RUNS_TOTAL.labels(status=status, agent_type=agent_type).inc()
    AGENT_RUN_DURATION.labels(status=status).observe(max(0.0, duration_seconds))


@_safe
def observe_queue_wait(seconds: float) -> None:
    AGENT_QUEUE_WAIT.observe(max(0.0, seconds))


@_safe
def set_active_runs(count: int) -> None:
    AGENT_ACTIVE_RUNS.set(count)


@_safe
def inc_retry(reason: str) -> None:
    AGENT_RUN_RETRIES.labels(reason=reason).inc()


@_safe
def inc_recovery(decision: str) -> None:
    AGENT_RECOVERY.labels(decision=decision).inc()


@_safe
def inc_lease_lost() -> None:
    AGENT_LEASE_LOST.inc()


@_safe
def observe_tool(tool: str, status: str, duration_seconds: Optional[float] = None) -> None:
    AGENT_TOOL_CALLS.labels(tool=tool, status=status).inc()
    if duration_seconds is not None:
        AGENT_TOOL_DURATION.labels(tool=tool).observe(max(0.0, duration_seconds))


@_safe
def observe_model(provider: str, model: str, status: str,
                  latency_seconds: Optional[float] = None,
                  prompt_tokens: int = 0, completion_tokens: int = 0) -> None:
    AGENT_MODEL_CALLS.labels(provider=provider, model=model, status=status).inc()
    if latency_seconds is not None:
        AGENT_MODEL_LATENCY.labels(provider=provider, model=model).observe(max(0.0, latency_seconds))
    if prompt_tokens:
        AGENT_TOKENS.labels(provider=provider, model=model, direction="input").inc(prompt_tokens)
    if completion_tokens:
        AGENT_TOKENS.labels(provider=provider, model=model, direction="output").inc(completion_tokens)


@_safe
def inc_sse_reconnect() -> None:
    AGENT_SSE_RECONNECT.inc()


@_safe
def observe_event_lag(seconds: float) -> None:
    AGENT_EVENT_LAG.observe(max(0.0, seconds))


@_safe
def observe_checkpoint_write(seconds: float) -> None:
    AGENT_CHECKPOINT_WRITE.observe(max(0.0, seconds))


def render() -> tuple[bytes, str]:
    """/metrics 响应体（未安装 prometheus_client 时返回空文本）。"""
    if not _AVAILABLE:
        return b"", "text/plain; charset=utf-8"
    return generate_latest(REGISTRY), CONTENT_TYPE_LATEST
