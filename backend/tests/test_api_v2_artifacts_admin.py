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


def _mk_run_with_frozen(user_id: int, agent_id: int, run_id: str, frozen: list, summary: dict | None = None) -> int:
    """创建 Run 并写入冻结交付副本 [(digest, rel_path, content)]，返回 session_id。"""
    from app.models.agent import AgentRun, AgentSession
    from app.services.agent_runtime.backends import workspace_root

    engine = create_engine(os.environ["DATABASE_SYNC_URL"])
    with Session(engine) as s:
        sess = AgentSession(agent_id=agent_id, user_id=user_id, messages=[])
        s.add(sess)
        s.commit()
        session_id = sess.id
        s.add(AgentRun(id=run_id, agent_id=agent_id, session_id=session_id, user_id=user_id,
                       thread_id=f"t-{run_id}", status="completed", summary=summary))
        s.commit()

    root = workspace_root(user_id, agent_id, session_id)
    for digest, rel, content in frozen:
        target = os.path.join(root, ".run-artifacts", run_id, digest, rel)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "w", encoding="utf-8") as f:
            f.write(content)
    return session_id


class TestRunArtifactsZip:
    def test_run_zip_packs_frozen_artifacts(self, client, user_setup):
        """Run 交付包：冻结副本打包，条目名为工作区相对路径。"""
        import hashlib
        from io import BytesIO
        from uuid import uuid4
        from zipfile import ZipFile

        agent_id = _mk_agent(user_setup["user1_id"])
        run_id = uuid4().hex
        _mk_run_with_frozen(user_setup["user1_id"], agent_id, run_id, [
            (hashlib.sha256(b"a").hexdigest(), "report/结果.md", "内容A"),
            (hashlib.sha256(b"b").hexdigest(), "data.csv", "内容B"),
        ])
        r = client.get(f"/api/v2/artifacts/runs/{run_id}/zip", headers=_auth(user_setup["user1_id"]))
        assert r.status_code == 200
        assert r.headers["content-type"] == "application/zip"
        assert f"filename*=UTF-8''run-{run_id}.zip" in r.headers["content-disposition"]
        with ZipFile(BytesIO(r.content)) as archive:
            assert sorted(archive.namelist()) == ["data.csv", "report/结果.md"]
            assert archive.read("report/结果.md") == "内容A".encode("utf-8")

    def test_run_zip_dedupes_digest_variants(self, client, user_setup):
        """同名多 digest 副本取 mtime 最新一份。"""
        import hashlib
        import time
        from io import BytesIO
        from uuid import uuid4
        from zipfile import ZipFile

        from app.services.agent_runtime.backends import workspace_root

        agent_id = _mk_agent(user_setup["user1_id"])
        run_id = uuid4().hex
        digest_old = hashlib.sha256(b"old").hexdigest()
        digest_new = hashlib.sha256(b"new").hexdigest()
        session_id = _mk_run_with_frozen(user_setup["user1_id"], agent_id, run_id, [
            (digest_old, "report.md", "旧版"),
            (digest_new, "report.md", "新版"),
        ])
        root = workspace_root(user_setup["user1_id"], agent_id, session_id)
        now = time.time()
        os.utime(os.path.join(root, ".run-artifacts", run_id, digest_old, "report.md"), (now - 60, now - 60))
        os.utime(os.path.join(root, ".run-artifacts", run_id, digest_new, "report.md"), (now, now))
        r = client.get(f"/api/v2/artifacts/runs/{run_id}/zip", headers=_auth(user_setup["user1_id"]))
        with ZipFile(BytesIO(r.content)) as archive:
            assert archive.namelist() == ["report.md"]
            assert archive.read("report.md") == "新版".encode("utf-8")

    def test_run_zip_owner_isolation_and_missing(self, client, user_setup):
        from uuid import uuid4

        agent_id = _mk_agent(user_setup["user1_id"])
        run_id = uuid4().hex
        import hashlib
        _mk_run_with_frozen(user_setup["user1_id"], agent_id, run_id, [
            (hashlib.sha256(b"a").hexdigest(), "a.txt", "x"),
        ])
        assert client.get(
            f"/api/v2/artifacts/runs/{run_id}/zip", headers=_auth(user_setup["user2_id"]),
        ).status_code == 404
        assert client.get(
            "/api/v2/artifacts/runs/no-such-run/zip", headers=_auth(user_setup["user1_id"]),
        ).status_code == 404

    def test_run_zip_falls_back_to_summary_paths(self, client, user_setup):
        """旧版 Run 无冻结副本：按 summary 记录的工作区原路径打包（穿越路径拒绝）。"""
        from io import BytesIO
        from uuid import uuid4
        from zipfile import ZipFile

        from app.services.agent_runtime.backends import workspace_root

        agent_id = _mk_agent(user_setup["user1_id"])
        run_id = uuid4().hex
        summary = {"verification": {"artifacts": [{"path": "draft.md"}, {"path": "../evil.txt"}]}}
        session_id = _mk_run_with_frozen(user_setup["user1_id"], agent_id, run_id, [], summary=summary)
        root = workspace_root(user_setup["user1_id"], agent_id, session_id)
        os.makedirs(root, exist_ok=True)
        with open(os.path.join(root, "draft.md"), "w", encoding="utf-8") as f:
            f.write("草稿")
        r = client.get(f"/api/v2/artifacts/runs/{run_id}/zip", headers=_auth(user_setup["user1_id"]))
        assert r.status_code == 200
        with ZipFile(BytesIO(r.content)) as archive:
            assert archive.namelist() == ["draft.md"]
            assert archive.read("draft.md") == "草稿".encode("utf-8")

    def test_run_zip_without_deliverables_404(self, client, user_setup):
        from uuid import uuid4

        agent_id = _mk_agent(user_setup["user1_id"])
        run_id = uuid4().hex
        _mk_run_with_frozen(user_setup["user1_id"], agent_id, run_id, [])
        assert client.get(
            f"/api/v2/artifacts/runs/{run_id}/zip", headers=_auth(user_setup["user1_id"]),
        ).status_code == 404


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
