"""技能管理模块测试：frontmatter 修补 / DB 技能注入（Agent 级绑定）/ 管理 API。

运行：
    pytest backend/tests/test_skill_management.py -q
"""
import pytest


class TestSkillBodyHelpers:
    def test_ensure_frontmatter_appends_when_missing(self):
        from app.services.agent_runtime.skills import ensure_skill_frontmatter

        body = ensure_skill_frontmatter("# 说明\n正文", name="my-skill", description="描述")
        assert body.startswith("---\nname: my-skill\n")
        assert "description: 描述" in body
        assert "# 说明\n正文" in body

    def test_ensure_frontmatter_fixes_name_mismatch(self):
        from app.services.agent_runtime.skills import ensure_skill_frontmatter

        raw = "---\nname: old-name\nversion: 1.0\ndescription: d\n---\n\n# 正文"
        body = ensure_skill_frontmatter(raw, name="new-name")
        assert "name: new-name" in body
        assert "old-name" not in body
        assert "# 正文" in body

    def test_ensure_frontmatter_keeps_valid(self):
        from app.services.agent_runtime.skills import ensure_skill_frontmatter

        raw = "---\nname: s1\ndescription: d\nversion: 2.0\n---\n\n# s1"
        assert ensure_skill_frontmatter(raw, name="s1") == raw


class _FakeSkill:
    def __init__(self, name, body="# t", enabled=True, agent_ids=None, files=None):
        self.name = name
        self.body = body
        self.enabled = enabled
        self.agent_ids = agent_ids
        self.files = files


class TestDbSkillInjection:
    def test_binding_semantics(self):
        from app.services.agent_runtime.skills import db_files_payload

        rows = [
            _FakeSkill("global-skill", agent_ids=None),      # 全局
            _FakeSkill("no-agent", agent_ids=[]),            # 不注入
            _FakeSkill("bound", agent_ids=[1]),              # 仅 Agent 1
            _FakeSkill("off", enabled=False),                # 禁用
        ]
        files = db_files_payload(rows, agent_id=1)
        assert set(files) == {"/skills/global-skill/SKILL.md", "/skills/bound/SKILL.md"}
        files2 = db_files_payload(rows, agent_id=2)
        assert set(files2) == {"/skills/global-skill/SKILL.md"}

    def test_enabled_names_filter(self):
        from app.services.agent_runtime.skills import db_enabled_names

        rows = [
            _FakeSkill("global-skill", agent_ids=None),
            _FakeSkill("no-agent", agent_ids=[]),
            _FakeSkill("bound", agent_ids=[1]),
            _FakeSkill("off", enabled=False),
        ]
        assert db_enabled_names(rows, agent_id=1) == {"global-skill", "bound"}
        assert db_enabled_names(rows, agent_id=2) == {"global-skill"}

    def test_db_skill_aux_files_injected(self):
        """管理面技能附属文件（标准 Skill 形态）随 SKILL.md 一起注入。"""
        from app.services.agent_runtime.skills import db_files_payload

        row = _FakeSkill("kit", files={"scripts/run.py": "print(1)\n", "references/doc.md": "# doc\n"})
        files = db_files_payload([row], agent_id=1)
        assert files["/skills/kit/SKILL.md"]["content"] == "# t"
        assert files["/skills/kit/scripts/run.py"]["content"] == "print(1)\n"
        assert files["/skills/kit/references/doc.md"]["content"] == "# doc\n"

    def test_db_skill_invalid_relpath_skipped(self):
        """非法附属文件路径防御性跳过（写入侧已 400，注入侧兜底）。"""
        from app.services.agent_runtime.skills import db_files_payload

        row = _FakeSkill("kit", files={
            "../escape.py": "bad", "/abs.py": "bad", "a\\b.py": "bad",
            "SKILL.md": "bad", "ok.py": "good",
        })
        files = db_files_payload([row], agent_id=1)
        assert set(files) == {"/skills/kit/SKILL.md", "/skills/kit/ok.py"}
        assert files["/skills/kit/SKILL.md"]["content"] == "# t"  # body 不被附属文件覆盖

    def test_same_name_db_overrides_registry(self):
        from app.services.agent_runtime.skills import (
            SkillRegistry,
            db_files_payload,
        )

        merged = SkillRegistry().files_payload()
        merged.update(db_files_payload([_FakeSkill("chart-report", body="DB 版本")], agent_id=1))
        assert merged["/skills/chart-report/SKILL.md"]["content"] == "DB 版本"

    @pytest.mark.asyncio
    async def test_service_skill_files_merges_db(self):
        from app.core.database import AsyncSessionLocal, init_db
        from app.models.skill import Skill

        from app.services.agent_runtime.service import AgentRuntimeService

        await init_db()
        async with AsyncSessionLocal() as db:
            row = Skill(name="svc-skill", body="# svc", enabled=True, agent_ids=None)
            db.add(row)
            await db.commit()
        try:
            async with AsyncSessionLocal() as db:
                payload = await AgentRuntimeService._skill_files(object(), db, 1)
            files = payload["files"]
            assert "/skills/svc-skill/SKILL.md" in files
            assert "/skills/chart-report/SKILL.md" in files  # 内置仍在
        finally:
            async with AsyncSessionLocal() as db:
                from sqlalchemy import delete

                await db.execute(delete(Skill).where(Skill.name == "svc-skill"))
                await db.commit()

    @pytest.mark.asyncio
    async def test_service_skill_files_db_overrides_whole_dir(self, tmp_path, monkeypatch):
        """同名 DB 技能整目录覆盖 registry：目录技能的附属文件不残留。"""
        from app.core.database import AsyncSessionLocal, init_db
        from app.models.skill import Skill

        from app.services.agent_runtime.service import AgentRuntimeService

        skill_dir = tmp_path / "dup-kit"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text(
            "---\nname: dup-kit\ndescription: 同名覆盖\n---\n\n# 目录版\n", encoding="utf-8")
        (skill_dir / "legacy.py").write_text("print('dir')\n", encoding="utf-8")
        monkeypatch.setattr("app.core.config.settings.AGENT_SKILLS_DIR", str(tmp_path), raising=False)

        await init_db()
        async with AsyncSessionLocal() as db:
            row = Skill(name="dup-kit", body="# DB 版", enabled=True, agent_ids=None,
                        files={"scripts/new.py": "print('db')\n"})
            db.add(row)
            await db.commit()
        try:
            async with AsyncSessionLocal() as db:
                payload = await AgentRuntimeService._skill_files(object(), db, 1)
            files = payload["files"]
            assert files["/skills/dup-kit/SKILL.md"]["content"] == "# DB 版"
            assert files["/skills/dup-kit/scripts/new.py"]["content"] == "print('db')\n"
            assert "/skills/dup-kit/legacy.py" not in files  # 目录版附属文件不残留
        finally:
            async with AsyncSessionLocal() as db:
                from sqlalchemy import delete

                await db.execute(delete(Skill).where(Skill.name == "dup-kit"))
                await db.commit()


class TestSkillManagementApi:
    @pytest.fixture(scope="class")
    def client(self):
        from fastapi.testclient import TestClient

        from app.main import app

        with TestClient(app) as c:
            yield c

    @pytest.fixture(scope="class")
    def users(self):
        import os

        from sqlalchemy import create_engine
        from sqlalchemy.orm import Session

        from app.models.user import User

        engine = create_engine(os.environ["DATABASE_SYNC_URL"])
        with Session(engine) as s:
            admin = User(username="sk_admin", email="sk_admin@test.dev",
                         hashed_password="x", is_active=True, is_superuser=True)
            normal = User(username="sk_user", email="sk_user@test.dev",
                          hashed_password="x", is_active=True)
            s.add_all([admin, normal])
            s.commit()
            return {"admin": admin.id, "user": normal.id}

    @staticmethod
    def _auth(user_id: int) -> dict:
        from app.core.security import create_access_token

        return {"Authorization": f"Bearer {create_access_token(user_id)}"}

    def test_crud_roundtrip(self, client, users):
        admin_h = self._auth(users["admin"])
        user_h = self._auth(users["user"])

        # 普通用户只读不写
        assert client.get("/api/v2/skills", headers=user_h).status_code == 200
        assert client.post("/api/v2/skills", headers=user_h,
                           json={"name": "x", "body": "y"}).status_code == 403

        # 创建（无 frontmatter 的正文自动补齐）
        resp = client.post("/api/v2/skills", headers=admin_h, json={
            "name": "api-skill",
            "description": "API 创建的技能",
            "body": "# api-skill\n\n测试正文",
            "agent_ids": [1],
        })
        assert resp.status_code == 200, resp.text
        skill = resp.json()
        sid = skill["id"]
        assert skill["body"].startswith("---\nname: api-skill")
        assert skill["managed"] is True

        # 重名拒绝
        dup = client.post("/api/v2/skills", headers=admin_h,
                          json={"name": "api-skill", "body": "z"})
        assert dup.status_code == 400
        # 与内置技能冲突拒绝
        dup2 = client.post("/api/v2/skills", headers=admin_h,
                           json={"name": "chart-report", "body": "z"})
        assert dup2.status_code == 400

        # 列表可见（含内置技能只读项）
        items = client.get("/api/v2/skills", headers=user_h).json()["items"]
        names = {i["name"] for i in items}
        assert {"api-skill", "chart-report"} <= names
        builtin = next(i for i in items if i["name"] == "chart-report")
        assert builtin["managed"] is False

        # 更新（body 变更保持 frontmatter 合法）
        resp = client.put(f"/api/v2/skills/{sid}", headers=admin_h, json={
            "body": "更新后的正文", "agent_ids": None,
        })
        assert resp.status_code == 200
        assert "name: api-skill" in resp.json()["body"]
        assert "更新后的正文" in resp.json()["body"]

        # 启停切换
        resp = client.post(f"/api/v2/skills/{sid}/toggle", headers=admin_h)
        assert resp.json()["enabled"] is False

        # 删除
        assert client.delete(f"/api/v2/skills/{sid}", headers=admin_h).status_code == 200
        assert client.delete(f"/api/v2/skills/{sid}", headers=admin_h).status_code == 404

    def test_files_roundtrip_and_validation(self, client, users):
        """附属文件（标准 Skill 形态）API roundtrip + 写入侧校验。"""
        admin_h = self._auth(users["admin"])

        # 创建带附属文件的技能
        resp = client.post("/api/v2/skills", headers=admin_h, json={
            "name": "api-kit",
            "body": "# api-kit\n\n引用 scripts/run.py",
            "files": {"scripts/run.py": "print(1)\n", "references/doc.md": "# doc\n"},
        })
        assert resp.status_code == 200, resp.text
        skill = resp.json()
        sid = skill["id"]
        assert skill["files"] == {"scripts/run.py": "print(1)\n", "references/doc.md": "# doc\n"}

        # 更新整体替换附属文件
        resp = client.put(f"/api/v2/skills/{sid}", headers=admin_h,
                          json={"files": {"scripts/v2.py": "print(2)\n"}})
        assert resp.status_code == 200
        assert resp.json()["files"] == {"scripts/v2.py": "print(2)\n"}

        # 清空附属文件
        resp = client.put(f"/api/v2/skills/{sid}", headers=admin_h, json={"files": {}})
        assert resp.status_code == 200
        assert resp.json()["files"] == {}

        # 非法附属文件路径 400
        bad = client.post("/api/v2/skills", headers=admin_h, json={
            "name": "api-bad-path", "body": "x",
            "files": {"../escape.py": "bad"},
        })
        assert bad.status_code == 400

        # 非法技能名 400（防 /skills/ 注入路径逃逸）
        bad2 = client.post("/api/v2/skills", headers=admin_h,
                           json={"name": "../evil", "body": "x"})
        assert bad2.status_code == 400

        # 单文件超限 400
        from app.services.agent_runtime.skills import MAX_SKILL_FILE_BYTES

        big = client.post("/api/v2/skills", headers=admin_h, json={
            "name": "api-big", "body": "x",
            "files": {"big.txt": "x" * (MAX_SKILL_FILE_BYTES + 1)},
        })
        assert big.status_code == 400

        # 清理
        assert client.delete(f"/api/v2/skills/{sid}", headers=admin_h).status_code == 200
