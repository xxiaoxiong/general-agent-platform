"""P0-06 模型能力真实探测：Tool Calling / Streaming 分类逻辑。

bind_tools() 成功不足以证明服务端原生工具调用；探测以服务端真实返回体为准。
运行：
    pytest backend/tests/test_model_capability_probe.py -q
"""
import asyncio
from types import SimpleNamespace

import pytest

from app.services.model_service import ModelService


def _tool_call_response(name="get_probe_constant"):
    tc = SimpleNamespace(function=SimpleNamespace(name=name))
    msg = SimpleNamespace(tool_calls=[tc], content=None)
    return SimpleNamespace(choices=[SimpleNamespace(message=msg)])


def _text_response(content="你好"):
    msg = SimpleNamespace(tool_calls=None, content=content)
    return SimpleNamespace(choices=[SimpleNamespace(message=msg)])


class _FakeCompletions:
    def __init__(self, result=None, exc=None, stream_chunks=None):
        self._result = result
        self._exc = exc
        self._stream_chunks = stream_chunks or []
        self.calls = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        if self._exc:
            raise self._exc
        if kwargs.get("stream"):
            async def gen():
                for c in self._stream_chunks:
                    yield c
            return gen()
        return self._result


def _fake_client(completions):
    return SimpleNamespace(chat=SimpleNamespace(completions=completions))


def _patch(monkeypatch, completions):
    monkeypatch.setattr(ModelService, "_get_client", staticmethod(lambda provider: _fake_client(completions)))


def _provider_model():
    p = SimpleNamespace(name="fake", base_url="http://fake.local/v1", api_key="none")
    m = SimpleNamespace(name="fake-model")
    return p, m


class TestToolCallingProbe:
    def test_native_tool_call_passes(self, monkeypatch):
        _patch(monkeypatch, _FakeCompletions(result=_tool_call_response()))
        p, m = _provider_model()
        out = asyncio.run(ModelService.probe_tool_calling(p, m))
        assert out["tool_calling"] is True and "探测通过" in out["message"]

    def test_text_only_fails(self, monkeypatch):
        """模型只回文本 = 服务端无原生工具调用，必须判定失败（不得误报通过）"""
        _patch(monkeypatch, _FakeCompletions(result=_text_response("PING-1234")))
        p, m = _provider_model()
        out = asyncio.run(ModelService.probe_tool_calling(p, m))
        assert out["tool_calling"] is False and "tool_calls" in out["message"]

    def test_wrong_tool_call_fails(self, monkeypatch):
        _patch(monkeypatch, _FakeCompletions(result=_tool_call_response(name="other_tool")))
        p, m = _provider_model()
        out = asyncio.run(ModelService.probe_tool_calling(p, m))
        assert out["tool_calling"] is False and "非预期" in out["message"]

    def test_request_error_classified(self, monkeypatch):
        """超时/连接错误归类为探测失败，带错误类型信息"""
        _patch(monkeypatch, _FakeCompletions(exc=TimeoutError("request timed out")))
        p, m = _provider_model()
        out = asyncio.run(ModelService.probe_tool_calling(p, m))
        assert out["tool_calling"] is False and "TimeoutError" in out["message"]

    def test_probe_request_carries_no_side_effect_tool(self, monkeypatch):
        comp = _FakeCompletions(result=_tool_call_response())
        _patch(monkeypatch, comp)
        p, m = _provider_model()
        asyncio.run(ModelService.probe_tool_calling(p, m))
        req = comp.calls[0]
        assert req["tools"][0]["function"]["name"] == ModelService.PROBE_TOOL_NAME
        assert req["temperature"] == 0 and req["stream"] is False


class TestStreamingProbe:
    def test_chunks_received_pass(self, monkeypatch):
        delta = SimpleNamespace(content="O")
        chunk = SimpleNamespace(choices=[SimpleNamespace(delta=delta)])
        _patch(monkeypatch, _FakeCompletions(stream_chunks=[chunk, chunk]))
        p, m = _provider_model()
        out = asyncio.run(ModelService.probe_streaming(p, m))
        assert out["streaming"] is True and "2" in out["message"]

    def test_no_chunks_fails(self, monkeypatch):
        _patch(monkeypatch, _FakeCompletions(stream_chunks=[]))
        p, m = _provider_model()
        out = asyncio.run(ModelService.probe_streaming(p, m))
        assert out["streaming"] is False


class TestModelConfigReport:
    def test_report_aggregates_and_requires_tool_calling(self, monkeypatch):
        """passed 只看 tool_calling（内核硬性要求）；streaming 作为附加报告。"""
        async def fake_tool(provider, model_config):
            return {"tool_calling": True, "message": "ok", "latency_ms": 1.0}

        async def fake_stream(provider, model_config):
            return {"streaming": False, "message": "no", "latency_ms": 1.0}

        monkeypatch.setattr(ModelService, "probe_tool_calling", staticmethod(fake_tool))
        monkeypatch.setattr(ModelService, "probe_streaming", staticmethod(fake_stream))
        p, m = _provider_model()
        report = asyncio.run(ModelService.test_model_config(p, m))
        assert report["passed"] is True
        assert report["tool_calling"] is True and report["streaming"] is False

    def test_report_fails_without_tool_calling(self, monkeypatch):
        async def fake_tool(provider, model_config):
            return {"tool_calling": False, "message": "no native tool calls", "latency_ms": 1.0}

        async def fake_stream(provider, model_config):
            return {"streaming": True, "message": "ok", "latency_ms": 1.0}

        monkeypatch.setattr(ModelService, "probe_tool_calling", staticmethod(fake_tool))
        monkeypatch.setattr(ModelService, "probe_streaming", staticmethod(fake_stream))
        p, m = _provider_model()
        report = asyncio.run(ModelService.test_model_config(p, m))
        assert report["passed"] is False
