"""OpenTelemetry 接入（P1-02）。

- FastAPI / HTTPX / Redis / SQLAlchemy instrumentation；
- W3C TraceContext 传播（API ↔ Worker ↔ 模型/工具请求关联）；
- OTLP（gRPC）批量导出：Trace 导出失败不影响业务执行；
- ``OTEL_ENABLED=false``（默认）时整条链路 no-op。

指标（Prometheus）在 metrics.py 单独维护。
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from app.core.config import settings

logger = logging.getLogger("ai_plat.observability.otel")

_initialized = False
_tracer = None


def is_enabled() -> bool:
    return bool(settings.OTEL_ENABLED and settings.OTEL_EXPORTER_OTLP_ENDPOINT)


def get_tracer(name: str = "general-agent-platform"):
    """获取 Tracer；未启用时返回 No-op Tracer（调用方无需判空）。"""
    global _tracer
    if _tracer is None:
        from opentelemetry import trace

        _tracer = trace.get_tracer(name)
    return _tracer


def init_otel(app: Any = None) -> None:
    """初始化 OTel TracerProvider + instrumentation（幂等，可重复调用）。

    - ``app`` 提供时做 FastAPI instrumentation；
    - 任何 instrumentation 失败只降级该组件，不影响启动与执行。
    """
    global _initialized
    if _initialized or not is_enabled():
        return
    _initialized = True

    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
            OTLPSpanExporter,
        )
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        resource = Resource.create({
            "service.name": settings.OTEL_SERVICE_NAME,
            "deployment.environment": settings.APP_ENV,
            "service.version": settings.APP_VERSION,
        })
        provider = TracerProvider(resource=resource)
        exporter = OTLPSpanExporter(endpoint=settings.OTEL_EXPORTER_OTLP_ENDPOINT, insecure=True)
        # BatchSpanProcessor：后台批量 + 有界队列（溢出丢 Span 不阻塞执行）
        provider.add_span_processor(BatchSpanProcessor(exporter))
        trace.set_tracer_provider(provider)
    except Exception as e:  # noqa: BLE001
        logger.warning("OTel TracerProvider 初始化失败，降级为 no-op: %s", e)
        return

    if app is not None:
        try:
            from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

            FastAPIInstrumentor.instrument_app(app)
        except Exception as e:  # noqa: BLE001
            logger.warning("FastAPI instrumentation 失败（忽略）: %s", e)

    for name, _instrument in (
        ("HTTPX", _instrument_httpx),
        ("Redis", _instrument_redis),
        ("SQLAlchemy", _instrument_sqlalchemy),
    ):
        try:
            _instrument()
        except Exception as e:  # noqa: BLE001
            logger.warning("%s instrumentation 失败（忽略）: %s", name, e)

    logger.info("OTel 已启用：endpoint=%s", settings.OTEL_EXPORTER_OTLP_ENDPOINT)


def _instrument_httpx() -> None:
    from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor

    HTTPXClientInstrumentor().instrument()


def _instrument_redis() -> None:
    from opentelemetry.instrumentation.redis import RedisInstrumentor

    RedisInstrumentor().instrument()


def _instrument_sqlalchemy() -> None:
    """SQLAlchemy instrumentation（挂到 async engine 的 sync_engine 内核）。"""
    from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor

    from app.core.database import engine

    target = getattr(engine, "sync_engine", engine)
    SQLAlchemyInstrumentor().instrument_engine(target, enable_commenter=True)


def reset_for_tests() -> None:
    global _initialized, _tracer
    _initialized = False
    _tracer = None
