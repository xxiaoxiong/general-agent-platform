"""P1-02：OTel 初始化与 instrumentation 接线（离线，无网络导出）。

运行：
    pytest backend/tests/test_otel_tracing.py -q
"""
import pytest

from app.observability import otel_tracing as ot


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    ot.reset_for_tests()
    monkeypatch.setattr(ot.settings, "OTEL_ENABLED", False, raising=False)
    monkeypatch.setattr(ot.settings, "OTEL_EXPORTER_OTLP_ENDPOINT", "", raising=False)
    yield
    ot.reset_for_tests()


class TestGating:
    def test_disabled_by_default(self):
        assert ot.is_enabled() is False
        ot.init_otel()
        assert ot._initialized is False, "未启用时不得初始化 TracerProvider"

    def test_enabled_flag_without_endpoint_is_off(self, monkeypatch):
        monkeypatch.setattr(ot.settings, "OTEL_ENABLED", True, raising=False)
        assert ot.is_enabled() is False
        ot.init_otel()
        assert ot._initialized is False

    def test_noop_tracer_works(self):
        """未启用时 get_tracer 返回 no-op Tracer，调用方无需判空。"""
        tracer = ot.get_tracer()
        with tracer.start_as_current_span("probe") as span:
            assert span is not None


class TestInit:
    def test_init_wires_provider_and_instrumentations(self, monkeypatch):
        """启用时：TracerProvider 挂载 + 三类 instrumentation 各调用一次。"""
        monkeypatch.setattr(ot.settings, "OTEL_ENABLED", True, raising=False)
        monkeypatch.setattr(ot.settings, "OTEL_EXPORTER_OTLP_ENDPOINT", "http://127.0.0.1:4317", raising=False)

        called = {"httpx": 0, "redis": 0, "sqlalchemy": 0}
        monkeypatch.setattr(ot, "_instrument_httpx", lambda: called.__setitem__("httpx", called["httpx"] + 1))
        monkeypatch.setattr(ot, "_instrument_redis", lambda: called.__setitem__("redis", called["redis"] + 1))
        monkeypatch.setattr(ot, "_instrument_sqlalchemy", lambda: called.__setitem__("sqlalchemy", called["sqlalchemy"] + 1))

        class FakeExporter:
            def __init__(self, **kwargs):
                self.kwargs = kwargs

        class FakeProcessor:
            def __init__(self, exporter):
                self.exporter = exporter

            def on_start(self, span, parent_context=None):
                pass

            def on_end(self, span):
                pass

            def _on_ending(self, span):
                pass

            def shutdown(self):
                return True

            def force_flush(self, timeout_millis=30000):
                return True

        import opentelemetry.exporter.otlp.proto.grpc.trace_exporter as te
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        monkeypatch.setattr(te, "OTLPSpanExporter", FakeExporter)
        monkeypatch.setattr("opentelemetry.sdk.trace.export.BatchSpanProcessor", FakeProcessor)

        ot.init_otel()
        assert ot._initialized is True
        assert called == {"httpx": 1, "redis": 1, "sqlalchemy": 1}

        ot.init_otel()  # 幂等
        assert called == {"httpx": 1, "redis": 1, "sqlalchemy": 1}

    def test_provider_failure_degrades_without_raising(self, monkeypatch):
        monkeypatch.setattr(ot.settings, "OTEL_ENABLED", True, raising=False)
        monkeypatch.setattr(ot.settings, "OTEL_EXPORTER_OTLP_ENDPOINT", "http://127.0.0.1:4317", raising=False)

        def _boom(**kwargs):
            raise RuntimeError("exporter init failed")

        import opentelemetry.exporter.otlp.proto.grpc.trace_exporter as te

        monkeypatch.setattr(te, "OTLPSpanExporter", _boom)
        ot.init_otel()  # 不得抛异常
        assert ot._initialized is True  # 已尝试初始化；TracerProvider 降级为 no-op
