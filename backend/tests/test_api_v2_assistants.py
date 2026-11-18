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
        from app.assistant.builder import infer_capability_ids

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
