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
    DOCUMENT_STAGE_DURATION = Histogram(
        "document_stage_duration_seconds", "文档任务节点耗时",
        ["task_type", "stage", "outcome"], registry=REGISTRY,
        buckets=(0.05, 0.1, 0.5, 1, 2, 5, 15, 30, 90, 300),
    )
    DOCUMENT_VALIDATION = Counter(
        "document_validation_total", "文档质量检查结果",
        ["task_type", "outcome"], registry=REGISTRY,
    )

    # ── Workspace Sandbox Runtime（任务书 §14）──────────────────────────
    SANDBOX_RUNNER_ONLINE = Gauge(
        "sandbox_runner_online", "在线 Sandbox Runner 数", registry=REGISTRY,
    )
    SANDBOX_RUNNER_HEARTBEAT_AGE = Gauge(
        "sandbox_runner_heartbeat_age_seconds", "Runner 最近心跳距今秒数",
        ["node"], registry=REGISTRY,
    )
    SANDBOX_ACTIVE_COUNT = Gauge(
        "sandbox_active_count", "活动 Sandbox 数", registry=REGISTRY,
    )
    SANDBOX_QUEUE_DEPTH = Gauge(
        "sandbox_queue_depth", "Sandbox Job 队列深度", registry=REGISTRY,
    )
    SANDBOX_QUEUE_WAIT = Histogram(
        "sandbox_queue_wait_seconds", "Job 从入队到被领取的等待",
        registry=REGISTRY, buckets=(0.1, 0.5, 1, 2, 5, 10, 30, 60, 120),
    )
    SANDBOX_PROVISION_DURATION = Histogram(
        "sandbox_provision_duration_seconds", "Sandbox 创建到 Ready 耗时",
        registry=REGISTRY, buckets=(1, 2, 5, 10, 20, 30, 60, 120),
    )
    SANDBOX_START_DURATION = Histogram(
        "sandbox_start_duration_seconds", "Sandbox 启动耗时",
        registry=REGISTRY, buckets=(0.5, 1, 2, 5, 10, 20, 30, 60),
    )
    SANDBOX_CPU_RESERVED = Gauge(
        "sandbox_cpu_reserved", "Sandbox CPU 预留（核）", registry=REGISTRY,
    )
    SANDBOX_CPU_USAGE = Gauge(
        "sandbox_cpu_usage", "Sandbox CPU 实际使用（核）", registry=REGISTRY,
    )
    SANDBOX_MEMORY_RESERVED = Gauge(
        "sandbox_memory_reserved_bytes", "Sandbox 内存预留字节", registry=REGISTRY,
    )
    SANDBOX_MEMORY_USAGE = Gauge(
        "sandbox_memory_usage_bytes", "Sandbox 内存实际使用字节", registry=REGISTRY,
    )
    SANDBOX_VOLUME_USAGE = Gauge(
        "sandbox_volume_usage_bytes", "Workspace Volume 使用字节", registry=REGISTRY,
    )
    SANDBOX_JOB_RETRY = Counter(
        "sandbox_job_retry_total", "Sandbox Job 重试次数", registry=REGISTRY,
    )
    SANDBOX_RECONCILE_MISMATCH = Counter(
        "sandbox_reconcile_mismatch_total", "Desired/Observed 不一致次数",
        registry=REGISTRY,
    )
    SANDBOX_OOM = Counter(
        "sandbox_oom_total", "Sandbox OOM 次数", registry=REGISTRY,
    )
    SERVICE_RESTART = Counter(
        "workspace_service_restart_total", "Service 重启次数",
        ["result"], registry=REGISTRY,
    )
    SERVICE_HEALTH = Gauge(
        "workspace_service_health", "Service 健康状态（1=healthy）",
        ["workspace", "service"], registry=REGISTRY,
    )
    PREVIEW_REQUESTS = Counter(
        "preview_request_total", "Preview 请求数", ["result"], registry=REGISTRY,
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


@_safe
def observe_document_stage(task_type: str, stage: str, outcome: str, seconds: float) -> None:
    DOCUMENT_STAGE_DURATION.labels(task_type=task_type, stage=stage, outcome=outcome).observe(seconds)


@_safe
def record_document_validation(task_type: str, passed: bool) -> None:
    DOCUMENT_VALIDATION.labels(task_type=task_type, outcome="passed" if passed else "failed").inc()


# ── Workspace Sandbox Runtime ─────────────────────────────────────────────

@_safe
def set_sandbox_capacity(runner_count: int, active_count: int, queue_depth: int,
                         cpu_reserved_millis: int = 0, memory_reserved_bytes: int = 0) -> None:
    SANDBOX_RUNNER_ONLINE.set(runner_count)
    SANDBOX_ACTIVE_COUNT.set(active_count)
    SANDBOX_QUEUE_DEPTH.set(queue_depth)
    SANDBOX_CPU_RESERVED.set(cpu_reserved_millis / 1000.0)
    SANDBOX_MEMORY_RESERVED.set(memory_reserved_bytes)


@_safe
def set_runner_heartbeat_age(node: str, seconds: float) -> None:
    SANDBOX_RUNNER_HEARTBEAT_AGE.labels(node=node).set(max(0.0, seconds))


@_safe
def observe_sandbox_queue_wait(seconds: float) -> None:
    SANDBOX_QUEUE_WAIT.observe(max(0.0, seconds))


@_safe
def observe_sandbox_provision(seconds: float) -> None:
    SANDBOX_PROVISION_DURATION.observe(max(0.0, seconds))


@_safe
def observe_sandbox_start(seconds: float) -> None:
    SANDBOX_START_DURATION.observe(max(0.0, seconds))


@_safe
def set_sandbox_usage(cpu_cores: float = 0.0, memory_bytes: int = 0,
                      volume_bytes: int = 0) -> None:
    SANDBOX_CPU_USAGE.set(max(0.0, cpu_cores))
    SANDBOX_MEMORY_USAGE.set(max(0, memory_bytes))
    SANDBOX_VOLUME_USAGE.set(max(0, volume_bytes))


@_safe
def inc_sandbox_job_retry() -> None:
    SANDBOX_JOB_RETRY.inc()


@_safe
def inc_sandbox_reconcile_mismatch() -> None:
    SANDBOX_RECONCILE_MISMATCH.inc()


@_safe
def inc_sandbox_oom() -> None:
    SANDBOX_OOM.inc()


@_safe
def inc_service_restart(result: str = "ok") -> None:
    SERVICE_RESTART.labels(result=result).inc()


@_safe
def set_service_health(workspace_id: int, service: str, healthy: bool) -> None:
    SERVICE_HEALTH.labels(workspace=str(workspace_id), service=service).set(
        1 if healthy else 0)


@_safe
def inc_preview_request(denied: bool = False) -> None:
    PREVIEW_REQUESTS.labels(result="denied" if denied else "ok").inc()


def render() -> tuple[bytes, str]:
    """/metrics 响应体（未安装 prometheus_client 时返回空文本）。"""
    if not _AVAILABLE:
        return b"", "text/plain; charset=utf-8"
    return generate_latest(REGISTRY), CONTENT_TYPE_LATEST
