"""P3-02/P3-03：自然语言 Builder + Assistants/Conversations API v2。

运行：
    pytest backend/tests/test_api_v2_assistants.py -q
"""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session


@pytest.fixture(scope="module")
def client():
    from app.main import app

    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def user_setup():
    import os

    from app.models.user import User

    engine = create_engine(os.environ["DATABASE_SYNC_URL"])
    with Session(engine) as s:
        u = User(username="ast_u1", email="ast_u1@test.dev", hashed_password="x", is_active=True)
        s.add(u)
        s.commit()
        return {"user_id": u.id}


def _auth(user_id: int) -> dict:
    from app.core.security import create_access_token

    return {"Authorization": f"Bearer {create_access_token(user_id)}"}


class TestBuilder:
    def test_draft_from_natural_language(self, client, user_setup):
        """UX-02：一段自然语言生成可用草稿。"""
        resp = client.post(
            "/api/v2/assistants/draft",
            headers=_auth(user_setup["user_id"]),
            json={"description": "帮我创建一个行业研究助手，能够搜索最新资料、分析数据、核实信息来源，并生成结构化研究报告。"},
        )
        assert resp.status_code == 200
        draft = resp.json()
        assert draft["name"]
        assert "研究" in draft["system_prompt"] or draft["name"]
        assert "general" in draft["capability_ids"]
        assert "web_research" in draft["capability_ids"]
        assert "data_analysis" in draft["capability_ids"]
        assert draft["requires_confirmation"] is True

    def test_draft_rejects_empty_description(self, client, user_setup):
        resp = client.post(
            "/api/v2/assistants/draft",
            headers=_auth(user_setup["user_id"]),
            json={"description": "   "},
        )
        assert resp.status_code == 400

    def test_builder_capability_inference(self):
        """单元：关键词 → 能力包映射（不依赖 HTTP）。"""
        from app.services.assistant.builder import infer_capability_ids

        ids = infer_capability_ids("写 Python 脚本并重构代码")
        assert "coding" in ids
        ids2 = infer_capability_ids("发送邮件通知并调用 webhook")
        assert "business_automation" in ids2
        ids3 = infer_capability_ids("今天天气怎么样")
        assert ids3 == ["general"]


class TestAssistantsCRUD:
    def test_create_from_draft_and_immediately_usable(self, client, user_setup):
        """UX-02/03：确认草稿一键创建，无需底层参数。"""
        headers = _auth(user_setup["user_id"])
        draft = client.post(
            "/api/v2/assistants/draft", headers=headers,
            json={"description": "帮我创建一个文档处理助手，读取 PDF 和 Word 并提取要点"},
        ).json()
        resp = client.post(
            "/api/v2/assistants", headers=headers,
            json={
                "name": draft["name"],
                "description": draft["description"],
                "system_prompt": draft["system_prompt"],
                "capability_ids": draft["capability_ids"],
            },
        )
        assert resp.status_code == 200
        created = resp.json()
        assert created["id"] > 0
        assert created["version"]["version_no"] == 1  # 创建即发布 v1
        assert "doc_processing" in created["capability_ids"]

    def test_create_rejects_unknown_capability(self, client, user_setup):
        resp = client.post(
            "/api/v2/assistants", headers=_auth(user_setup["user_id"]),
            json={"name": "坏助手", "capability_ids": ["not_a_capability"]},
        )
        assert resp.status_code == 400

    def test_patch_and_publish(self, client, user_setup):
        headers = _auth(user_setup["user_id"])
        created = client.post(
            "/api/v2/assistants", headers=headers,
            json={"name": "可改助手", "capability_ids": ["general"]},
        ).json()
        patched = client.patch(
            f"/api/v2/assistants/{created['id']}", headers=headers,
            json={"description": "更新后的描述"},
        )
        assert patched.status_code == 200
        pub = client.post(f"/api/v2/assistants/{created['id']}/publish", headers=headers)
        assert pub.status_code == 200
        assert pub.json()["version_no"] == 2

    def test_delete_assistant(self, client, user_setup):
        headers = _auth(user_setup["user_id"])
        created = client.post(
            "/api/v2/assistants", headers=headers,
            json={"name": "待删助手", "capability_ids": ["general"]},
        ).json()
        resp = client.delete(f"/api/v2/assistants/{created['id']}", headers=headers)
        assert resp.status_code == 200
        assert client.get(f"/api/v2/assistants/{created['id']}", headers=headers).status_code == 404

    def test_owner_isolation(self, client, user_setup):
        """A22：他人助手 404。"""
        import os

        from app.models.user import User

        headers = _auth(user_setup["user_id"])
        created = client.post(
            "/api/v2/assistants", headers=headers,
            json={"name": "私有助手", "capability_ids": ["general"]},
        ).json()
        engine = create_engine(os.environ["DATABASE_SYNC_URL"])
        with Session(engine) as s:
            u2 = User(username="ast_u2", email="ast_u2@test.dev", hashed_password="x", is_active=True)
            s.add(u2)
            s.commit()
            u2_id = u2.id
        resp = client.get(f"/api/v2/assistants/{created['id']}", headers=_auth(u2_id))
        assert resp.status_code == 404


class TestConversations:
    def test_create_conversation_binds_default_assistant(self, client, user_setup):
        """UX-01/10：会话缺省绑定默认助手。"""
        headers = _auth(user_setup["user_id"])
        resp = client.post("/api/v2/conversations", headers=headers, json={})
        assert resp.status_code == 200
        conv = resp.json()
        assert conv["assistant_id"] > 0
        assert conv["title"] == "新对话"

    def test_conversation_messages_and_isolation(self, client, user_setup):
        headers = _auth(user_setup["user_id"])
        conv = client.post("/api/v2/conversations", headers=headers,
                           json={"title": "测试会话"}).json()
        msgs = client.get(f"/api/v2/conversations/{conv['id']}/messages", headers=headers)
        assert msgs.status_code == 200
        assert msgs.json()["items"] == []

        import os

        from app.models.user import User

        engine = create_engine(os.environ["DATABASE_SYNC_URL"])
        with Session(engine) as s:
            u2 = User(username="ast_u3", email="ast_u3@test.dev", hashed_password="x", is_active=True)
            s.add(u2)
            s.commit()
            u2_id = u2.id
        resp = client.get(f"/api/v2/conversations/{conv['id']}/messages", headers=_auth(u2_id))
        assert resp.status_code == 404

    def test_list_conversations(self, client, user_setup):
        headers = _auth(user_setup["user_id"])
        resp = client.get("/api/v2/conversations", headers=headers)
        assert resp.status_code == 200
        assert resp.json()["total"] >= 1

    def test_pin_rename_unpin_and_paginate_owned_conversations(self, client, user_setup):
        headers = _auth(user_setup["user_id"])
        paths = []

        def create(title):
            result = client.post("/api/v2/conversations", headers=headers, json={"title": title})
            assert result.status_code == 200
            body = result.json()
            paths.append(f"/api/v2/conversations/{body['id']}")
            assert body["is_pinned"] is False and body["pinned_at"] is None
            return body

        def change(index, metadata):
            result = client.patch(paths[index], headers=headers, json=metadata)
            assert result.status_code == 200
            return result.json()

        def page(number):
            response = client.get("/api/v2/conversations", headers=headers, params={"page": number, "page_size": 1})
            assert response.status_code == 200
            return response.json()["items"][0]

        try:
            first = create("旧会话")
            second = create("较早会话")
            recent = create("最近会话")
            pinned = change(0, {"title": "  制造周报  ", "is_pinned": True})
            assert pinned["title"] == "制造周报" and pinned["is_pinned"] is True
            assert pinned["pinned_at"] and pinned["updated_at"] == first["updated_at"]
            assert pinned["message_count"] == 0
            assert page(1)["id"] == first["id"]
            change(1, {"is_pinned": True})
            assert page(1)["id"] == second["id"] and page(2)["id"] == first["id"]
            # 重复置顶请求不会调整之前确定的顺序。
            assert change(0, {"is_pinned": True})["pinned_at"] == pinned["pinned_at"]
            assert page(1)["id"] == second["id"]
            unpinned = change(0, {"is_pinned": False})
            assert unpinned["is_pinned"] is False and unpinned["pinned_at"] is None
            assert unpinned["title"] == "制造周报" and unpinned["updated_at"] == first["updated_at"]
            assert page(2)["id"] == recent["id"]
            assert client.get(paths[0], headers=headers).json() == unpinned

            for invalid in ({}, {"title": "  "}, {"title": "长" * 201}, {"title": None},
                            {"is_pinned": "true"}, {"is_pinned": None}, {"position": 1}):
                assert client.patch(paths[0], headers=headers, json=invalid).status_code == 422
        finally:
            for path in paths:
                client.delete(path, headers=headers)

    def test_other_user_cannot_modify_conversation_metadata(self, client, user_setup):
        import os

        from app.models.user import User

        headers = _auth(user_setup["user_id"])
        created = client.post("/api/v2/conversations", headers=headers, json={"title": "个人记录"}).json()
        path = f"/api/v2/conversations/{created['id']}"
        engine = create_engine(os.environ["DATABASE_SYNC_URL"])
        try:
            with Session(engine) as session:
                other = User(username="ast_meta_other", email="ast_meta_other@test.dev", hashed_password="x")
                session.add(other)
                session.commit()
                other_headers = _auth(other.id)
                try:
                    assert client.patch(path, headers=other_headers, json={"title": "越权修改", "is_pinned": True}).status_code == 404
                    assert client.get(path, headers=headers).json() == created
                finally:
                    session.delete(other)
                    session.commit()
        finally:
            engine.dispose()
            client.delete(path, headers=headers)


@pytest.mark.asyncio
async def test_runtime_preserves_manually_reset_title_and_pinned_state(tmp_path):
    import app.models  # 确保关联表在 metadata 中注册
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.core.database import Base
    from app.models.agent import Agent, AgentSession
    from app.models.user import User
    from app.services.agent_runtime.repositories.session_repository import SessionRepository

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'metadata.db'}")
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        async with sessions() as db:
            db.add_all([
                Agent(id=1, name="测试助手"),
                User(id=1, username="meta_owner", email="meta_owner@test.dev", hashed_password="x"),
                AgentSession(id=1, agent_id=1, user_id=1, title="之前的标题", messages=[]),
                AgentSession(id=2, agent_id=1, user_id=1, messages=[]),
            ])
            await db.commit()
            repo = SessionRepository(db)
            assert await repo.update_metadata(1, 1, title="新对话", is_pinned=True)
            await db.commit()

        async with sessions() as db:
            repo = SessionRepository(db)
            message = {"role": "user", "content": "用户的新内容", "message_id": "new-message"}
            await repo.append_messages(1, [message], title="自动生成的名称")
            await repo.append_messages(2, [message], title="初次生成的名称")
            await db.commit()
            manual = await db.get(AgentSession, 1, populate_existing=True)
            default = await db.get(AgentSession, 2, populate_existing=True)
            assert manual.title == "新对话" and manual.title_edited and manual.pinned_at
            assert manual.messages == [message] and manual.version == 1
            assert default.title == "初次生成的名称" and not default.title_edited
    finally:
        await engine.dispose()
