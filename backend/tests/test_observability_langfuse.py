"""P1-01 / P1-04：Langfuse 接入与脱敏单测（离线，无网络）。

运行：
    pytest backend/tests/test_observability_langfuse.py -q
"""
import contextlib

import pytest

from app.observability import langfuse_tracing as lt


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    lt.reset_for_tests()
    monkeypatch.setattr(lt.settings, "LANGFUSE_PUBLIC_KEY", "", raising=False)
    monkeypatch.setattr(lt.settings, "LANGFUSE_SECRET_KEY", "", raising=False)
    yield
    lt.reset_for_tests()


class TestDisabledByDefault:
    def test_disabled_without_keys(self):
        assert lt.is_enabled() is False
        assert lt.get_client() is None
        assert lt.make_callback_handler("run-1") is None

    def test_platform_span_noop(self):
        """未配置时 platform_span 必须是无操作且不影响执行。"""
        with lt.platform_span("worker_claim", run_id="run-1") as span:
            assert span is None

    def test_platform_span_swallows_when_disabled_even_on_error(self):
        with pytest.raises(ValueError):
            with lt.platform_span("agent_run", run_id="run-1"):
                raise ValueError("boom")


class TestMasking:
    def test_sensitive_keys_redacted_recursively(self):
        data = {
            "api_key": "sk-123",
            "nested": {"Authorization": "Bearer x", "ok": "visible"},
            "items": [{"password": "p"}, "plain"],
        }
        out = lt.mask_sensitive(data)
        assert out["api_key"] == "***REDACTED***"
        assert out["nested"]["Authorization"] == "***REDACTED***"
        assert out["nested"]["ok"] == "visible"
        assert out["items"][0]["password"] == "***REDACTED***"
        assert out["items"][1] == "plain"

    def test_long_content_truncated(self):
        """P1-04：大输出不写入 Trace —— 超长字符串截断。"""
        big = "x" * 5000
        out = lt.mask_sensitive({"content": big})
        assert len(out["content"]) < 5000
        assert "truncated" in out["content"]
        assert lt.mask_sensitive("short") == "short"


class TestTraceId:
    def test_trace_id_deterministic_per_run(self):
        from langfuse import Langfuse

        t1 = lt.run_trace_id("run-abc")
        t2 = lt.run_trace_id("run-abc")
        t3 = lt.run_trace_id("run-xyz")
        assert t1 == t2, "同一 run_id 必须派生同一 trace_id（跨 Worker/恢复一致）"
        assert t1 != t3
        # Langfuse 官方 trace id 为 32 位十六进制
        assert len(t1) == 32 and int(t1, 16) >= 0


class TestTraceWebUrl:
    """Trace 页面路由是 /project/{pid}/traces/{tid}，缺 projectId 段在 UI 上 404。"""

    def test_project_id_from_settings(self, monkeypatch):
        monkeypatch.setattr(lt.settings, "LANGFUSE_PROJECT_ID", "p1")
        monkeypatch.setattr(lt.settings, "LANGFUSE_PUBLIC_URL", "http://ui.test")
        assert lt.build_trace_web_url("t1") == "http://ui.test/project/p1/traces/t1"
        # trace_id 为空 → Trace 列表页
        assert lt.build_trace_web_url() == "http://ui.test/project/p1/traces"

    def test_project_id_discovered_and_cached(self, monkeypatch):
        monkeypatch.setattr(lt.settings, "LANGFUSE_PROJECT_ID", "")
        monkeypatch.setattr(lt.settings, "LANGFUSE_PUBLIC_KEY", "pk")
        monkeypatch.setattr(lt.settings, "LANGFUSE_SECRET_KEY", "sk")
        monkeypatch.setattr(lt.settings, "LANGFUSE_BASE_URL", "http://lf.test")
        monkeypatch.setattr(lt.settings, "LANGFUSE_PUBLIC_URL", "")
        calls = []

        class _Resp:
            status_code, text = 200, ""

            def json(self):
                return {"data": [{"id": "pid-1"}]}

            def raise_for_status(self):
                pass

        class _Client:
            def __init__(self, *a, **k):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def get(self, url, **kwargs):
                calls.append(url)
                return _Resp()

        monkeypatch.setattr(lt.httpx, "Client", _Client)
        assert lt.langfuse_project_id() == "pid-1"
        assert lt.build_trace_web_url("t1") == "http://lf.test/project/pid-1/traces/t1"
        assert lt.langfuse_project_id() == "pid-1"
        assert len(calls) == 1, "项目 ID 发现结果必须缓存（每次调用打 API 不可接受）"

    def test_discovery_failure_degrades(self, monkeypatch):
        monkeypatch.setattr(lt.settings, "LANGFUSE_PROJECT_ID", "")
        monkeypatch.setattr(lt.settings, "LANGFUSE_PUBLIC_KEY", "pk")
        monkeypatch.setattr(lt.settings, "LANGFUSE_SECRET_KEY", "sk")
        monkeypatch.setattr(lt.settings, "LANGFUSE_BASE_URL", "http://lf.test")
        monkeypatch.setattr(lt.settings, "LANGFUSE_PUBLIC_URL", "http://ui.test")

        class _Client:
            def __init__(self, *a, **k):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def get(self, url, **kwargs):
                raise RuntimeError("down")

        monkeypatch.setattr(lt.httpx, "Client", _Client)
        assert lt.langfuse_project_id() == ""
        # 降级：链接仍返回（旧式路径），不抛异常
        assert lt.build_trace_web_url("t1") == "http://ui.test/project/traces/t1"


class _FakeSpan:
    def __init__(self):
        self.updates = []

    def update(self, **kwargs):
        self.updates.append(kwargs)


class _FakeClient:
    def __init__(self):
        self.spans = []
        self.calls = []

    @contextlib.contextmanager
    def start_as_current_observation(self, **kwargs):
        self.calls.append(kwargs)
        span = _FakeSpan()
        self.spans.append(span)
        yield span


class TestPlatformSpan:
    def test_span_records_attributes_and_output_with_masking(self, monkeypatch):
        fake = _FakeClient()
        monkeypatch.setattr(lt, "get_client", lambda: fake)
        with lt.platform_span("session_writeback", run_id="run-1",
                              attributes={"session_id": 7, "token": "secret"},
                              output={"ok": True}) as span:
            assert span is not None
        assert fake.calls[0]["name"] == "session_writeback"
        assert fake.calls[0]["trace_context"]["trace_id"] == lt.run_trace_id("run-1")
        meta = fake.spans[0].updates[0]["metadata"]
        assert meta["session_id"] == 7 and meta["token"] == "***REDACTED***"

    def test_span_marks_error_and_reraises(self, monkeypatch):
        fake = _FakeClient()
        monkeypatch.setattr(lt, "get_client", lambda: fake)
        with pytest.raises(RuntimeError, match="inner failure"):
            with lt.platform_span("agent_run", run_id="run-2"):
                raise RuntimeError("inner failure")
        last = fake.spans[0].updates[-1]
        assert last.get("level") == "ERROR" and "inner failure" in last["output"]["error"]

    def test_client_failure_degrades_to_noop(self, monkeypatch):
        def _boom():
            raise RuntimeError("backend down")

        monkeypatch.setattr(lt, "get_client", _boom)
        with lt.platform_span("agent_run", run_id="run-3") as span:
            assert span is None


class TestCallbackHandler:
    def test_handler_creation_when_enabled(self, monkeypatch):
        created = {}

        class FakeHandler:
            def __init__(self, *, trace_context=None):
                created["trace_context"] = trace_context

        monkeypatch.setattr(lt.settings, "LANGFUSE_PUBLIC_KEY", "pk-test", raising=False)
        monkeypatch.setattr(lt.settings, "LANGFUSE_SECRET_KEY", "sk-test", raising=False)
        import langfuse.langchain as lc

        monkeypatch.setattr(lc, "CallbackHandler", FakeHandler)
        handler = lt.make_callback_handler("run-9")
        assert isinstance(handler, FakeHandler)
        assert created["trace_context"]["trace_id"] == lt.run_trace_id("run-9")

    def test_handler_none_when_disabled(self):
        assert lt.make_callback_handler("run-9") is None
