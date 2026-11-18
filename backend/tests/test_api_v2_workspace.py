"""P3-01：默认通用助手 + workspace/bootstrap + capabilities API v2。

运行：
    pytest backend/tests/test_api_v2_workspace.py -q
"""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.core.security import create_access_token


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
        u = User(username="ws_u1", email="ws_u1@test.dev", hashed_password="x", is_active=True)
        s.add(u)
        s.commit()
        return {"user_id": u.id}


def _auth(user_id: int) -> dict:
    return {"Authorization": f"Bearer {create_access_token(user_id)}"}


class TestBootstrap:
    def test_bootstrap_creates_default_assistant(self, client, user_setup):
        resp = client.get("/api/v2/workspace/bootstrap", headers=_auth(user_setup["user_id"]))
        assert resp.status_code == 200
        data = resp.json()
        assert data["user"]["id"] == user_setup["user_id"]
        default = data["default_assistant"]
        assert default["is_default"] is True
        assert default["capability_ids"] == ["general"]
        assert default["name"] == "通用助手"
        assert data["capabilities"]  # 能力包列表非空

    def test_bootstrap_idempotent_single_default(self, client, user_setup):
        r1 = client.get("/api/v2/workspace/bootstrap", headers=_auth(user_setup["user_id"]))
        r2 = client.get("/api/v2/workspace/bootstrap", headers=_auth(user_setup["user_id"]))
        assert r1.json()["default_assistant"]["id"] == r2.json()["default_assistant"]["id"]

    def test_bootstrap_requires_auth(self, client):
        resp = client.get("/api/v2/workspace/bootstrap")
        assert resp.status_code in (401, 403)

    def test_default_assistant_owner_isolation(self, client, user_setup):
        """默认助手按 owner_id 隔离：另一用户 bootstrap 得到自己的默认助手。"""
        import os

        from app.models.user import User

        engine = create_engine(os.environ["DATABASE_SYNC_URL"])
        with Session(engine) as s:
            u2 = User(username="ws_u2", email="ws_u2@test.dev", hashed_password="x", is_active=True)
            s.add(u2)
            s.commit()
            u2_id = u2.id
        r1 = client.get("/api/v2/workspace/bootstrap", headers=_auth(user_setup["user_id"]))
        r2 = client.get("/api/v2/workspace/bootstrap", headers=_auth(u2_id))
        assert r1.json()["default_assistant"]["id"] != r2.json()["default_assistant"]["id"]
        assert r2.json()["default_assistant"]["owner_id"] == u2_id


class TestCapabilities:
    def test_list_capability_packs(self, client, user_setup):
        resp = client.get("/api/v2/capabilities", headers=_auth(user_setup["user_id"]))
        assert resp.status_code == 200
        items = resp.json()["items"]
        ids = {c["id"] for c in items}
        assert {"general", "web_research", "doc_processing", "data_analysis",
                "knowledge", "coding", "business_automation"} <= ids
        general = next(c for c in items if c["id"] == "general")
        assert general["default_enabled"] is True
