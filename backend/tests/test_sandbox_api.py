"""Workspace Sandbox API：项目/工作空间/服务/Preview/Runner 内部 API 契约。"""
import json
import os

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from starlette.websockets import WebSocketDisconnect

from app.core.config import settings
from app.core.security import create_access_token
from app.models.user import User


@pytest.fixture(scope="module")
def client():
    from app.main import app

    with TestClient(app) as c:
        yield c


def _user(username: str) -> int:
    engine = create_engine(os.environ["DATABASE_SYNC_URL"])
    with Session(engine) as s:
        existing = s.query(User).filter(User.username == username).one_or_none()
        if existing is not None:
            return existing.id
        user = User(
            username=username, email=f"{username}@test.dev",
            hashed_password="x", is_active=True,
        )
        s.add(user)
        s.commit()
        return user.id


def _auth(user_id: int) -> dict:
    return {"Authorization": f"Bearer {create_access_token(user_id)}"}


@pytest.fixture
def sandbox_enabled(monkeypatch):
    monkeypatch.setattr(settings, "SANDBOX_ENABLED", True)
    monkeypatch.setattr(settings, "SANDBOX_RUNNER_TOKEN", "test-runner-token-123456")
    yield


def test_project_workspace_and_sandbox_flow(client, sandbox_enabled, monkeypatch):
    user_id = _user("sbxapi-owner")
    headers = _auth(user_id)
    created = client.post("/api/v2/projects", json={"name": "API 项目"}, headers=headers)
    assert created.status_code == 200, created.text
    project_id = created.json()["id"]
    workspace = created.json()["workspace"]
    assert workspace["sandbox"] is None

    listed = client.get("/api/v2/projects", headers=headers)
    assert listed.status_code == 200
    assert any(item["id"] == project_id for item in listed.json()["items"])

    # 先注册 Runner，再申请 Sandbox（调度必须有可用节点）
    runner_headers = {"X-Runner-Token": settings.SANDBOX_RUNNER_TOKEN}
    unauthorized = client.post("/internal/runners/register", json={"node_name": "x"})
    assert unauthorized.status_code == 401
    registered = client.post("/internal/runners/register", json={
        "node_name": "api-runner",
        "cpu_total_millis": 8000, "cpu_allocatable_millis": 8000,
        "memory_total_bytes": 16 * 1024 ** 3,
        "memory_allocatable_bytes": 16 * 1024 ** 3,
        "disk_total_bytes": 100 * 1024 ** 3,
        "disk_allocatable_bytes": 100 * 1024 ** 3,
    }, headers=runner_headers)
    assert registered.status_code == 200, registered.text
    runner_id = registered.json()["runner_id"]
    heartbeat = client.post(f"/internal/runners/{runner_id}/heartbeat",
                            json={"status": "online"}, headers=runner_headers)
    assert heartbeat.status_code == 200

    provisioned = client.post(
        f"/api/v2/projects/{project_id}/workspace/provision", json={}, headers=headers,
    )
    assert provisioned.status_code == 200, provisioned.text
    sandbox = provisioned.json()["sandbox"]
    assert sandbox["observed_state"] == "queued"

    # Runner 内部 API：领取 / 完成（带 Fencing Token）
    claimed = client.post(f"/internal/runners/{runner_id}/jobs/claim",
                          json={"limit": 2}, headers=runner_headers)
    assert claimed.status_code == 200
    jobs_payload = claimed.json()["jobs"]
    assert len(jobs_payload) == 1
    job = jobs_payload[0]
    assert job["job_type"] == "provision"
    completed = client.post(f"/internal/jobs/{job['id']}/complete", json={
        "runner_id": runner_id, "lease_token": job["lease_token"],
        "result": {
            "container_id": "cid-1", "container_name": "gap-sbx-1-g1",
            "container_ip": "10.20.0.2", "image_digest": "sha256:test",
            "observed_state": "ready", "services": [],
        },
    }, headers=runner_headers)
    assert completed.status_code == 200, completed.text

    sandbox_resp = client.get(f"/api/v2/workspaces/{workspace['id']}/sandbox",
                              headers=headers)
    assert sandbox_resp.status_code == 200
    assert sandbox_resp.json()["sandbox"]["observed_state"] == "ready"

    # Service + Preview Grant（Agent 调用以 fake 替代）
    from app.services.sandbox import agent_client

    async def fake_start(sandbox_id, config):
        return {"observed_state": "healthy", "process_id": "123"}

    monkeypatch.setattr(agent_client, "start_service", fake_start)
    service = client.post(f"/api/v2/workspaces/{workspace['id']}/services", json={
        "name": "frontend", "command": "npm run dev -- --host 0.0.0.0", "port": 5173,
        "health_check_timeout": 30,
    }, headers=headers)
    assert service.status_code == 200, service.text
    service_id = service.json()["id"]
    assert service.json()["observed_state"] in ("healthy", "starting")
    services = client.get(f"/api/v2/workspaces/{workspace['id']}/services", headers=headers)
    assert services.status_code == 200
    assert any(item["name"] == "frontend" for item in services.json()["items"])
    grant = client.post(
        f"/api/v2/workspaces/{workspace['id']}/services/{service_id}/preview-grants",
        json={"ttl_seconds": 600}, headers=headers,
    )
    assert grant.status_code == 200, grant.text
    grant_data = grant.json()
    assert grant_data["token"] in grant_data["url"]
    revoked = client.delete(
        f"/api/v2/workspaces/{workspace['id']}/preview-grants/{grant_data['id']}",
        headers=headers,
    )
    assert revoked.status_code == 200

    stopped = client.post(f"/api/v2/workspaces/{workspace['id']}/sandbox/stop",
                          headers=headers)
    assert stopped.status_code == 200
    assert stopped.json()["sandbox"]["desired_state"] == "stopped"


def test_cross_user_cannot_access_project(client, sandbox_enabled):
    owner_id = _user("sbxapi-a")
    other_id = _user("sbxapi-b")
    created = client.post("/api/v2/projects", json={"name": "私有项目"},
                          headers=_auth(owner_id))
    project_id = created.json()["id"]
    forbidden = client.get(f"/api/v2/projects/{project_id}", headers=_auth(other_id))
    assert forbidden.status_code == 404
    members = client.get(f"/api/v2/projects/{project_id}/members", headers=_auth(owner_id))
    assert members.status_code == 200
    assert members.json()["items"][0]["role"] == "admin"


def test_workspace_events_sse_auth_and_incremental(client, sandbox_enabled):
    """Workspace SSE：只有成员可订阅，事件带 workspace/generation 标识（§12.3）。"""
    owner_id = _user("sbxapi-sse")
    other_id = _user("sbxapi-sse-x")
    created = client.post("/api/v2/projects", json={"name": "SSE 项目"},
                          headers=_auth(owner_id))
    assert created.status_code == 200, created.text
    workspace_id = created.json()["workspace"]["id"]

    denied = client.get(f"/api/v2/workspaces/{workspace_id}/events",
                        headers=_auth(other_id))
    assert denied.status_code == 404

    with client.stream("GET", f"/api/v2/workspaces/{workspace_id}/events?after_id=0",
                       headers=_auth(owner_id)) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        lines: list[str] = []
        for line in response.iter_lines():
            if line:
                lines.append(line)
            if any(item.startswith("data: ") for item in lines):
                break
    payload_line = next(item for item in lines if item.startswith("data: "))
    event = json.loads(payload_line[6:])
    assert event["type"] in ("workspace.provisioning", "workspace.created")
    assert event["workspace_id"] == workspace_id
    assert event["id"] >= 1


def test_file_tree_lazy_directory_listing(client, sandbox_enabled, monkeypatch):
    """文件树按目录懒加载（支持完整浏览），路径穿越被拒绝。"""
    from app.services.sandbox import session_files

    owner_id = _user("sbxapi-tree")
    headers = _auth(owner_id)
    created = client.post("/api/v2/projects", json={"name": "树项目"}, headers=headers)
    assert created.status_code == 200, created.text
    project_id = created.json()["id"]
    conversation = client.post("/api/v2/conversations", json={
        "title": "树会话", "project_id": project_id,
    }, headers=headers)
    assert conversation.status_code == 200, conversation.text
    session_id = conversation.json()["id"]

    async def fake_list(_db, _session, rel_path=""):
        assert rel_path == ""
        return [
            {"path": "frontend", "name": "frontend", "is_dir": True, "size": 0, "mtime": 0.0},
            {"path": "report.md", "name": "report.md", "is_dir": False, "size": 3, "mtime": 1.0},
        ]

    monkeypatch.setattr(session_files, "list_directory", fake_list)
    response = client.get(
        f"/api/v2/conversations/{session_id}/file-tree", headers=headers)
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["path"] == ""
    assert [item["name"] for item in data["entries"]] == ["frontend", "report.md"]
    assert data["entries"][1]["kind"]

    bad = client.get(
        f"/api/v2/conversations/{session_id}/file-tree?path=../secret", headers=headers)
    assert bad.status_code == 400


def _ready_workspace(client, headers: dict, username: str) -> int:
    """创建一个 ready Sandbox 的 Workspace（注册 Runner → provision → 完成 Job）。"""
    from app.models.sandbox import SandboxRunnerNode

    # 历史用例留下的 Runner 可能仍在调度池：先排空，确保 Job 落到当前测试节点
    engine = create_engine(os.environ["DATABASE_SYNC_URL"])
    with Session(engine) as session:
        session.query(SandboxRunnerNode).update({"status": "draining"})
        session.commit()
    created = client.post(
        "/api/v2/projects", json={"name": f"浏览器项目-{username}"}, headers=headers,
    )
    assert created.status_code == 200, created.text
    project_id = created.json()["id"]
    workspace_id = created.json()["workspace"]["id"]
    runner_headers = {"X-Runner-Token": settings.SANDBOX_RUNNER_TOKEN}
    registered = client.post("/internal/runners/register", json={
        "node_name": f"browser-runner-{username}",
        "cpu_total_millis": 8000, "cpu_allocatable_millis": 8000,
        "memory_total_bytes": 16 * 1024 ** 3,
        "memory_allocatable_bytes": 16 * 1024 ** 3,
        "disk_total_bytes": 100 * 1024 ** 3,
        "disk_allocatable_bytes": 100 * 1024 ** 3,
    }, headers=runner_headers)
    assert registered.status_code == 200, registered.text
    runner_id = registered.json()["runner_id"]
    client.post(f"/internal/runners/{runner_id}/heartbeat",
                json={"status": "online"}, headers=runner_headers)
    provisioned = client.post(
        f"/api/v2/projects/{project_id}/workspace/provision", json={}, headers=headers,
    )
    assert provisioned.status_code == 200, provisioned.text
    sandbox_id = provisioned.json()["sandbox"]["id"]
    job = None
    # 模块级 client 会保留其他用例的排队 Job：领到属于当前 Sandbox 的 provision 为止
    for _ in range(5):
        claimed = client.post(f"/internal/runners/{runner_id}/jobs/claim",
                              json={"limit": 8}, headers=runner_headers)
        assert claimed.status_code == 200, claimed.text
        job = next(
            (item for item in claimed.json()["jobs"]
             if item["sandbox_id"] == sandbox_id and item["job_type"] == "provision"),
            None,
        )
        if job is not None:
            break
    assert job is not None, "未领取到当前 Workspace 的 provision Job"
    completed = client.post(f"/internal/jobs/{job['id']}/complete", json={
        "runner_id": runner_id, "lease_token": job["lease_token"],
        "result": {
            "container_id": f"cid-{username}", "container_name": f"gap-sbx-{username}-g1",
            "container_ip": "10.20.0.2", "image_digest": "sha256:test",
            "observed_state": "ready", "services": [],
        },
    }, headers=runner_headers)
    assert completed.status_code == 200, completed.text
    return workspace_id


def test_browser_session_uses_internal_preview_and_checks_acl(
    client, sandbox_enabled, monkeypatch,
):
    """内置浏览器会话：内部 Preview 域名 + 服务/项目权限；未启用时 503。"""
    from app.services.sandbox import agent_client, browser as browser_service

    monkeypatch.setattr(settings, "BROWSER_SERVICE_TOKEN", "test-browser-token-123456")
    monkeypatch.setattr(settings, "PREVIEW_INTERNAL_BASE_DOMAIN", "preview.internal")
    monkeypatch.setattr(settings, "PREVIEW_REQUIRE_LOGIN", False)
    captured: dict = {}

    async def fake_create(session_id, url, *, width, height):
        captured.update(session_id=session_id, url=url, width=width, height=height)

    async def fake_start(_sandbox_id, _config):
        return {"observed_state": "healthy", "process_id": "1"}

    monkeypatch.setattr(browser_service, "create_remote_session", fake_create)
    monkeypatch.setattr(agent_client, "start_service", fake_start)

    owner_id = _user("sbxapi-browser")
    headers = _auth(owner_id)
    workspace_id = _ready_workspace(client, headers, "sbxapi-browser")
    service = client.post(f"/api/v2/workspaces/{workspace_id}/services", json={
        "name": "frontend", "command": "npm run dev -- --host 0.0.0.0", "port": 5173,
    }, headers=headers)
    assert service.status_code == 200, service.text
    service_id = service.json()["id"]

    created = client.post(
        f"/api/v2/workspaces/{workspace_id}/services/{service_id}/browser-sessions",
        json={"viewport_width": 1000, "viewport_height": 700}, headers=headers,
    )
    assert created.status_code == 200, created.text
    data = created.json()
    assert data["session_id"].startswith("bs")
    assert data["session_id"] in data["ws_url"] and "token=" in data["ws_url"]
    assert data["token"] and data["ttl_seconds"] == settings.BROWSER_SESSION_TTL
    assert data["viewport"] == {"width": 1000, "height": 700}
    assert data["preview_url"]
    assert captured["session_id"] == data["session_id"]
    assert captured["width"] == 1000 and captured["height"] == 700
    assert ".preview.internal:8081" in captured["url"]
    assert "access_token" not in captured["url"]

    outsider_id = _user("sbxapi-browser-out")
    denied = client.post(
        f"/api/v2/workspaces/{workspace_id}/services/{service_id}/browser-sessions",
        json={}, headers=_auth(outsider_id),
    )
    assert denied.status_code == 404

    monkeypatch.setattr(settings, "BROWSER_SERVICE_TOKEN", "")
    disabled = client.post(
        f"/api/v2/workspaces/{workspace_id}/services/{service_id}/browser-sessions",
        json={}, headers=headers,
    )
    assert disabled.status_code == 503
    assert disabled.json()["code"] == "browser_unavailable"


def test_browser_stream_rejects_invalid_token(client, sandbox_enabled):
    with pytest.raises(WebSocketDisconnect) as exc_info:
        with client.websocket_connect(
            "/api/v2/workspaces/browser-sessions/bs-x/stream?token=invalid",
        ):
            pass
    assert exc_info.value.code == 4403


def test_sandbox_disabled_returns_503(client, monkeypatch):
    monkeypatch.setattr(settings, "SANDBOX_ENABLED", False)
    user_id = _user("sbxapi-off")
    resp = client.get("/api/v2/projects", headers=_auth(user_id))
    assert resp.status_code == 503
    assert resp.json()["code"] == "sandbox_disabled"
