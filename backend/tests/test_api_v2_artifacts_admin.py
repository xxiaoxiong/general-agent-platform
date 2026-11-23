"""P3-D：Artifacts 安全下载 + admin/observability API v2。

运行：
    pytest backend/tests/test_api_v2_artifacts_admin.py -q
"""
import os

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
    from app.models.user import User

    engine = create_engine(os.environ["DATABASE_SYNC_URL"])
    with Session(engine) as s:
        u1 = User(username="art_u1", email="art_u1@test.dev", hashed_password="x", is_active=True)
        u2 = User(username="art_u2", email="art_u2@test.dev", hashed_password="x", is_active=True)
        admin = User(username="art_admin", email="art_admin@test.dev", hashed_password="x",
                     is_active=True, is_superuser=True)
        s.add_all([u1, u2, admin])
        s.commit()
        return {"user1_id": u1.id, "user2_id": u2.id, "admin_id": admin.id}


def _auth(user_id: int) -> dict:
    from app.core.security import create_access_token

    return {"Authorization": f"Bearer {create_access_token(user_id)}"}


def _mk_session_with_file(user_id: int, agent_id: int, rel_path: str, content: str = "artifact-body") -> tuple:
    """在会话工作区写产物文件，返回 (session_id, artifact_id)。"""
    from app.models.agent import AgentSession
    from app.services.agent_runtime.backends import workspace_root

    engine = create_engine(os.environ["DATABASE_SYNC_URL"])
    with Session(engine) as s:
        sess = AgentSession(agent_id=agent_id, user_id=user_id, messages=[])
        s.add(sess)
        s.commit()
        session_id = sess.id

    root = workspace_root(user_id, agent_id, session_id)
    os.makedirs(root, exist_ok=True)
    target = os.path.join(root, rel_path)
    os.makedirs(os.path.dirname(target), exist_ok=True)
    with open(target, "w", encoding="utf-8") as f:
        f.write(content)

    from app.api.workspace.artifacts import make_artifact_id

    return session_id, make_artifact_id(session_id, rel_path)


def _mk_agent(user_id: int) -> int:
    from app.models.agent import Agent

    engine = create_engine(os.environ["DATABASE_SYNC_URL"])
    with Session(engine) as s:
        agent = Agent(name="产物测试", system_prompt="t", tools=[], is_active=True, owner_id=user_id)
        s.add(agent)
        s.commit()
        return agent.id


class TestArtifacts:
    def test_download_artifact(self, client, user_setup):
        """UX-06：产物下载（归属 + 路径包含校验通过）。"""
        agent_id = _mk_agent(user_setup["user1_id"])
        _sid, artifact_id = _mk_session_with_file(
            user_setup["user1_id"], agent_id, "report/结果.md", "P3_ARTIFACT_OK"
        )
        r = client.get(f"/api/v2/artifacts/{artifact_id}", headers=_auth(user_setup["user1_id"]))
        assert r.status_code == 200
        assert "P3_ARTIFACT_OK" in r.text

    def test_artifact_owner_isolation(self, client, user_setup):
        agent_id = _mk_agent(user_setup["user1_id"])
        _sid, artifact_id = _mk_session_with_file(user_setup["user1_id"], agent_id, "a.txt")
        r = client.get(f"/api/v2/artifacts/{artifact_id}", headers=_auth(user_setup["user2_id"]))
        assert r.status_code == 404

    def test_artifact_traversal_rejected(self, client, user_setup):
        """路径穿越令牌直接拒绝（400），不触达文件系统。"""
        from app.api.workspace.artifacts import make_artifact_id

        agent_id = _mk_agent(user_setup["user1_id"])
        session_id, _ = _mk_session_with_file(user_setup["user1_id"], agent_id, "ok.txt")
        evil_id = make_artifact_id(session_id, "../../etc/passwd")
        r = client.get(f"/api/v2/artifacts/{evil_id}", headers=_auth(user_setup["user1_id"]))
        assert r.status_code == 400

    def test_artifact_invalid_token(self, client, user_setup):
        r = client.get("/api/v2/artifacts/not-base64-!!!", headers=_auth(user_setup["user1_id"]))
        assert r.status_code == 400

    def test_artifact_missing_file_404(self, client, user_setup):
        from app.api.workspace.artifacts import make_artifact_id

        agent_id = _mk_agent(user_setup["user1_id"])
        session_id, _ = _mk_session_with_file(user_setup["user1_id"], agent_id, "x.txt")
        ghost = make_artifact_id(session_id, "no-such-file.txt")
        r = client.get(f"/api/v2/artifacts/{ghost}", headers=_auth(user_setup["user1_id"]))
        assert r.status_code == 404


class TestAdminObservability:
    def test_observability_superuser_only(self, client, user_setup):
        """P0-03：管理端点仅超管可访问。"""
        r = client.get("/api/v2/admin/observability", headers=_auth(user_setup["user1_id"]))
        assert r.status_code == 403
        r = client.get("/api/v2/admin/observability")
        assert r.status_code in (401, 403)

    def test_observability_overview_shape(self, client, user_setup):
        r = client.get("/api/v2/admin/observability", headers=_auth(user_setup["admin_id"]))
        assert r.status_code == 200
        data = r.json()
        assert data["window_hours"] == 24
        assert "by_status" in data["runs"]
        assert "by_status" in data["tool_executions"]
        assert "total" in data["events"]
        assert data["links"]["langfuse"]
