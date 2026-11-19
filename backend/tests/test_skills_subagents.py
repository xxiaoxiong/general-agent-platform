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

    def test_files_payload_confined_to_skills_prefix(self):
        """Skill 只注入 /skills/** 文件（指令 + 附属文件），不得携带工具授权。"""
        payload = SkillRegistry().files_payload()
        assert payload, "应有内置技能注入"
        assert all(k.startswith("/skills/") for k in payload)
        assert all(".." not in k.split("/") for k in payload)

    def test_external_skill_multi_file_injection(self, tmp_path, monkeypatch):
        """标准 Agent Skills 形态：目录技能整目录注入（SKILL.md + 脚本/引用）。"""
        skill_dir = tmp_path / "report-kit"
        (skill_dir / "scripts").mkdir(parents=True)
        (skill_dir / "references").mkdir()
        (skill_dir / "SKILL.md").write_text(
            "---\nname: report-kit\ndescription: 报告工具包\n---\n\n# report-kit\n",
            encoding="utf-8",
        )
        (skill_dir / "scripts" / "build.py").write_bytes(b"print('hi')\n")
        (skill_dir / "references" / "guide.md").write_bytes("# 指南\n".encode("utf-8"))
        (skill_dir / ".hidden").write_text("secret", encoding="utf-8")
        (skill_dir / "__pycache__").mkdir()
        (skill_dir / "__pycache__" / "x.pyc").write_bytes(b"\x00")
        monkeypatch.setattr("app.core.config.settings.AGENT_SKILLS_DIR", str(tmp_path), raising=False)

        payload = SkillRegistry().files_payload()
        assert "/skills/report-kit/SKILL.md" in payload
        assert payload["/skills/report-kit/scripts/build.py"]["content"] == "print('hi')\n"
        assert payload["/skills/report-kit/references/guide.md"]["content"] == "# 指南\n"
        assert not any(".hidden" in k or "__pycache__" in k for k in payload)

    def test_external_skill_binary_file_base64(self, tmp_path, monkeypatch):
        """非 UTF-8 附属文件以 base64 注入（create_file_data encoding="base64"）。"""
        import base64

        skill_dir = tmp_path / "bin-kit"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text(
            "---\nname: bin-kit\ndescription: 二进制附件\n---\n\n# bin-kit\n", encoding="utf-8")
        raw = b"\x89PNG\r\n\x00\x01"
        (skill_dir / "logo.bin").write_bytes(raw)
        monkeypatch.setattr("app.core.config.settings.AGENT_SKILLS_DIR", str(tmp_path), raising=False)

        payload = SkillRegistry().files_payload()
        data = payload["/skills/bin-kit/logo.bin"]
        assert data["encoding"] == "base64"
        assert data["content"] == base64.b64encode(raw).decode("ascii")

    def test_external_skill_skips_oversized_files(self, tmp_path, monkeypatch):
        """附属文件超单文件上限跳过注入（SKILL.md 不受限）。"""
        from app.services.agent_runtime.skills import MAX_SKILL_FILE_BYTES

        skill_dir = tmp_path / "big-kit"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text(
            "---\nname: big-kit\ndescription: 超限文件\n---\n\n# big-kit\n", encoding="utf-8")
        (skill_dir / "huge.txt").write_text("x" * (MAX_SKILL_FILE_BYTES + 1), encoding="utf-8")
        monkeypatch.setattr("app.core.config.settings.AGENT_SKILLS_DIR", str(tmp_path), raising=False)

        payload = SkillRegistry().files_payload()
        assert "/skills/big-kit/SKILL.md" in payload
        assert "/skills/big-kit/huge.txt" not in payload

    def test_validate_skill_relpath(self):
        from app.services.agent_runtime.skills import validate_skill_relpath

        assert validate_skill_relpath("scripts/build.py") == "scripts/build.py"
        for bad in ["", "/etc/passwd", "../escape.py", "a/../b.py", "a\\b.py",
                    "a//b.py", "./x.py", "SKILL.md", "a/./b.py", "x" * 300, "a\x00b"]:
            with pytest.raises(ValueError):
                validate_skill_relpath(bad)

    def test_validate_skill_name(self):
        from app.services.agent_runtime.skills import validate_skill_name

        assert validate_skill_name("my-skill_1") == "my-skill_1"
        for bad in ["", "../evil", "a/b", "a b", "技能", "x" * 65, "-lead", "a.b"]:
            with pytest.raises(ValueError):
                validate_skill_name(bad)

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
