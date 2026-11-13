"""P2-05 / P2-06：Skills Registry 与 SubAgent Registry。

运行：
    pytest backend/tests/test_skills_subagents.py -q
"""
from pathlib import Path

import pytest

from app.services.agent_runtime.skills import SkillRegistry
from app.services.agent_runtime.subagents import (
    SubAgentProfile,
    SubAgentRegistry,
    build_subagents,
)
from app.services.agent_runtime.tools.policy import ToolPolicy


class TestSkillRegistry:
    def test_builtin_skills_carry_metadata(self):
        reg = SkillRegistry()
        skills = {s.name: s for s in reg.list_skills()}
        assert set(skills) >= {"chart-report", "meeting-minutes"}
        for s in skills.values():
            assert s.version and s.scope == "builtin" and s.source == "builtin"

    def test_disabled_via_settings(self):
        reg = SkillRegistry(disabled={"chart-report"})
        names = {s.name for s in reg.enabled_skills()}
        assert "chart-report" not in names and "meeting-minutes" in names

    def test_external_dir_scanning_with_scope_and_version(self, tmp_path, monkeypatch):
        skill_dir = tmp_path / "org-report"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text(
            "---\nname: org-report\ndescription: 组织级报告技能\nversion: 2.1\n---\n\n# org-report\n",
            encoding="utf-8",
        )
        monkeypatch.setattr("app.core.config.settings.AGENT_SKILLS_DIR", str(tmp_path), raising=False)
        reg = SkillRegistry()
        skills = {s.name: s for s in reg.list_skills()}
        assert "org-report" in skills
        meta = skills["org-report"]
        assert meta.scope == "org" and meta.version == "2.1"
        assert meta.source == str(skill_dir)

    def test_scope_filtering(self, tmp_path, monkeypatch):
        monkeypatch.setattr("app.core.config.settings.AGENT_SKILLS_DIR", "", raising=False)
        reg = SkillRegistry()
        assert all(s.scope == "builtin" for s in reg.list_skills(scope="builtin"))
        assert reg.list_skills(scope="user") == []

    def test_files_payload_only_instruction_files(self):
        """Skill 只注入指令文件（/skills/**），不得携带工具授权。"""
        payload = SkillRegistry().files_payload()
        assert payload, "应有内置技能注入"
        assert all(k.startswith("/skills/") and k.endswith("/SKILL.md") for k in payload)

    def test_audit_fields(self):
        fields = SkillRegistry().to_audit_fields()
        assert {"skills"} <= set(fields)
        for item in fields["skills"]:
            assert {"name", "version", "scope", "source", "enabled"} <= set(item)


class TestSubAgentRegistry:
    def test_builtin_profiles_registered(self):
        reg = SubAgentRegistry()
        names = {p.name for p in reg.list_profiles()}
        assert names == {"researcher", "data-analyst"}
        p = reg.get("researcher")
        assert p.model_policy == "inherit" and p.max_depth == 1

    def test_register_custom_profile(self):
        reg = SubAgentRegistry(profiles={})
        reg.register(SubAgentProfile(
            name="auditor", description="审计子代理", system_prompt="s",
            tools=frozenset({"calculator"}),
        ))
        assert reg.get("auditor") is not None
        assert [p.name for p in reg.list_profiles()] == ["auditor"]

    def test_build_subagents_narrows_to_policy(self):
        policy = ToolPolicy(allowed_tools={"calculator", "db_query"})
        out = build_subagents(policy)
        by_name = {s["name"]: s for s in out}
        # researcher 的工具与父授权交集只剩 calculator
        assert by_name["researcher"]["tools"] == ["calculator"]
        # data-analyst 收窄到 db_query/calculator
        assert set(by_name["data-analyst"]["tools"]) == {"calculator", "db_query"}

    def test_build_subagents_skips_empty_intersection(self):
        policy = ToolPolicy(allowed_tools={"calculator"})
        out = build_subagents(policy, registry=SubAgentRegistry(profiles={
            "lonely": SubAgentProfile(
                name="lonely", description="d", system_prompt="s",
                tools=frozenset({"web_search"}),
            ),
        }))
        assert out == [], "无授权交集的子代理不得出现（不暴露不可用委派）"

    def test_unregistered_subagent_not_delegable(self):
        """委派面 = 注册表白名单：未登记名字不出现在任何子代理配置。"""
        policy = ToolPolicy(allowed_tools={"calculator", "web_search"})
        names = {s["name"] for s in build_subagents(policy)}
        assert names <= {"researcher", "data-analyst"}
        assert "god-mode" not in names
