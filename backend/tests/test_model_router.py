"""P2-03：Model Router 选模与 ModelClientFactory 客户端复用。

运行：
    pytest backend/tests/test_model_router.py -q
"""
import asyncio

import pytest

from app.assistant.model_clients import ModelClientFactory
from app.assistant.model_router import (
    ModelCandidate,
    candidate_from_row,
    check_model_requirements,
    missing_capabilities,
    select_model,
)
from app.services.agent_runtime.model_adapter import ModelCapabilityError


def _cand(id=1, name="m", **kw):
    flags = {
        "supports_function_call": True,
        "supports_streaming": True,
        "supports_structured_output": False,
        "supports_vision": False,
        "supports_parallel_tool_calls": False,
    }
    flags.update(kw.pop("flags", {}))
    return ModelCandidate(id=id, name=name, provider_id=1, flags=flags, **kw)


class TestRequirements:
    def test_missing_capabilities_mapping(self):
        weak = _cand(flags={"supports_function_call": False})
        assert missing_capabilities(weak, ["tool_calling"]) == ["tool_calling"]
        assert missing_capabilities(_cand(), ["tool_calling", "streaming"]) == []

    def test_check_raises_with_clear_message(self):
        weak = _cand(name="weak-model", flags={"supports_function_call": False})
        with pytest.raises(ModelCapabilityError, match="weak-model"):
            check_model_requirements(weak, ["tool_calling"])


class TestSelectModel:
    def test_preferred_incompatible_never_silently_switched(self):
        """绑定模型不兼容时必须报错，禁止静默切换。"""
        weak = _cand(id=7, name="bound-weak", flags={"supports_function_call": False})
        strong = _cand(id=8, name="other-strong")
        with pytest.raises(ModelCapabilityError, match="bound-weak"):
            select_model([weak, strong], preferred_model_config_id=7)

    def test_preferred_compatible_returned(self):
        a = _cand(id=7)
        b = _cand(id=8, is_default_provider=True)
        assert select_model([a, b], preferred_model_config_id=7).id == 7

    def test_preferred_missing_raises(self):
        with pytest.raises(ModelCapabilityError, match="不存在"):
            select_model([_cand(id=1)], preferred_model_config_id=99)

    def test_default_provider_preferred_then_verified_then_id(self):
        plain = _cand(id=3)
        default_provider = _cand(id=4, is_default_provider=True)
        assert select_model([plain, default_provider]).id == 4

        verified = _cand(id=5, capability_status="verified")
        unverified = _cand(id=6, capability_status="unverified")
        assert select_model([unverified, verified]).id == 5

    def test_no_compatible_raises(self):
        weak = _cand(flags={"supports_function_call": False})
        with pytest.raises(ModelCapabilityError, match="没有满足能力要求"):
            select_model([weak])

    def test_require_verified_rejects_unverified(self):
        unverified = _cand(id=2, capability_status="unverified")
        with pytest.raises(ModelCapabilityError, match="没有满足能力要求"):
            select_model([unverified], require_verified=True)
        verified = _cand(id=9, capability_status="verified")
        assert select_model([unverified, verified], require_verified=True).id == 9

    def test_capability_status_gates_preferred_with_require_verified(self):
        unverified = _cand(id=2, capability_status="unverified")
        with pytest.raises(ModelCapabilityError, match="探测"):
            select_model([unverified], preferred_model_config_id=2, require_verified=True)


class TestCandidateFromRow:
    def test_maps_row_fields(self):
        mc = type("MC", (), {
            "id": 11, "name": "row-model", "provider_id": 2,
            "context_length": 8192, "max_tokens": 1024,
            "supports_function_call": True, "supports_streaming": True,
            "supports_structured_output": True, "supports_vision": False,
            "supports_parallel_tool_calls": False,
            "capability_status": "verified",
        })()
        provider = type("P", (), {"is_default": True})()
        c = candidate_from_row(mc, provider)
        assert c.id == 11 and c.is_default_provider is True
        assert c.has_capability("tool_calling") and c.has_capability("structured_output")
        assert not c.has_capability("vision")


class TestClientFactory:
    def test_clients_cached_and_config_keyed(self):
        ModelClientFactory.reset_for_tests()
        a1 = ModelClientFactory.get_openai_client(base_url="http://a.local/v1", api_key="k")
        a2 = ModelClientFactory.get_openai_client(base_url="http://a.local/v1", api_key="k")
        b = ModelClientFactory.get_openai_client(base_url="http://b.local/v1", api_key="k")
        assert a1 is a2, "同配置客户端必须复用"
        assert a1 is not b

        h1 = ModelClientFactory.get_httpx_client(verify=True)
        h2 = ModelClientFactory.get_httpx_client(verify=True)
        assert h1 is h2

        asyncio.run(ModelClientFactory.close_all())
        assert ModelClientFactory._openai_clients == {}
        assert ModelClientFactory._httpx_clients == {}
