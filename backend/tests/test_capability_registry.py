"""P2-02：Capability Registry 能力包解析与授权（fail-closed）。

运行：
    pytest backend/tests/test_capability_registry.py -q
"""
import pytest

from app.services.assistant.capability import (
    CAPABILITY_PACKS,
    CapabilityError,
    build_skill_sources,
    build_subagent_profiles,
    build_toolset,
    list_capabilities,
    resolve_capabilities,
    validate_capability_policy,
)


class TestCatalog:
    def test_seven_packs_registered(self):
        assert set(CAPABILITY_PACKS) == {
            "general", "web_research", "doc_processing",
            "data_analysis", "knowledge", "coding", "business_automation",
        }

    def test_list_capabilities_fields(self):
        items = list_capabilities()
        assert len(items) == 7
        for item in items:
            for key in ("id", "version", "name", "description", "risk_level",
                        "default_enabled", "requires_hitl", "required_resources",
                        "policy_note", "model_requirements"):
                assert key in item, f"{item.get('id')} 缺少字段 {key}"
        general = next(i for i in items if i["id"] == "general")
        assert general["default_enabled"] is True


class TestResolve:
    def test_default_falls_back_to_general(self):
        resolved = resolve_capabilities(None, None)
        assert resolved.capability_ids == ["general"]
        assert "calculator" in resolved.tools and "datetime_now" in resolved.tools

    def test_unknown_capability_rejected(self):
        with pytest.raises(CapabilityError, match="未登记"):
            resolve_capabilities(["general", "hack_all"], None)

    def test_resource_capability_requires_binding(self):
        """data_analysis 必须绑定数据源；缺失即拒绝（不可静默降级）。"""
        with pytest.raises(CapabilityError, match="database_connection"):
            resolve_capabilities(["data_analysis"], None)
        resolved = resolve_capabilities(
            ["data_analysis"], {"database_connection": 3}
        )
        assert "db_query" in resolved.tools
        assert resolved.subagent_profiles == ["data-analyst"]

    def test_knowledge_requires_base(self):
        with pytest.raises(CapabilityError, match="knowledge_base"):
            resolve_capabilities(["knowledge"], {})
        resolved = resolve_capabilities(["knowledge"], {"knowledge_base": 5})
        assert "knowledge_search" in resolved.tools

    def test_merge_dedupes_and_aggregates_flags(self):
        resolved = resolve_capabilities(
            ["general", "web_research", "business_automation"],
            None,
        )
        assert resolved.tools.count("calculator") == 1, "合并工具必须去重"
        assert resolved.requires_hitl is True
        assert resolved.requires_network is True
        assert resolved.risk_level == "high"
        assert "researcher" in resolved.subagent_profiles

    def test_build_interfaces(self):
        resolved = resolve_capabilities(["web_research"], None)
        assert build_toolset(resolved) == resolved.tools
        assert build_skill_sources(resolved) == resolved.skill_sources
        assert build_subagent_profiles(resolved) == resolved.subagent_profiles


class TestPolicyValidation:
    def test_hitl_required_but_disabled_rejected(self):
        resolved = resolve_capabilities(["business_automation"], None)
        with pytest.raises(CapabilityError, match="HITL"):
            validate_capability_policy(resolved, enable_hitl=False)
        validate_capability_policy(resolved, enable_hitl=True)

    def test_network_required_but_disabled_rejected(self):
        resolved = resolve_capabilities(["web_research"], None)
        with pytest.raises(CapabilityError, match="网络"):
            validate_capability_policy(resolved, allow_network=False)

    def test_coding_rejected_when_sandbox_disabled(self):
        resolved = resolve_capabilities(["coding"], None)
        with pytest.raises(CapabilityError, match="沙箱"):
            validate_capability_policy(resolved, sandbox_mode="disabled")
        validate_capability_policy(resolved, sandbox_mode="restricted")


class TestRuntimeIntegration:
    def test_resolve_tool_specs_merges_capabilities(self):
        """service._resolve_tool_specs：能力包工具 + 显式工具去重合并。"""
        from app.services.agent_runtime.service import AgentRuntimeService

        specs = AgentRuntimeService._resolve_tool_specs(None, {
            "capability_ids": ["general", "web_research"],
            "tools": [{"name": "calculator", "enabled": True}, {"name": "weather", "enabled": False}],
        })
        names = [s["name"] for s in specs]
        assert "web_search" in names and "calculator" in names
        assert names.count("calculator") == 1
        assert "weather" not in names, "显式禁用的工具不得进入工具面"

    def test_legacy_agents_without_capabilities_keep_explicit_tools(self):
        from app.services.agent_runtime.service import AgentRuntimeService

        specs = AgentRuntimeService._resolve_tool_specs(None, {"tools": ["calculator"]})
        assert [s["name"] for s in specs] == ["calculator"]
