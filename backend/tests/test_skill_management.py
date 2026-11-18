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
    def __init__(self, name, body="# t", enabled=True, agent_ids=None):
        self.name = name
        self.body = body
        self.enabled = enabled
        self.agent_ids = agent_ids


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
