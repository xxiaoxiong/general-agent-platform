"""验证真实请求身份、各结果、表项差异和审计接口权限。"""

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.identity import audit, auth
from app.api.chat import conversations, files
from app.core import database
from app.core.config import settings
from app.core.security import create_access_token, get_password_hash
from app.models.agent import Agent, AgentSession
from app.models.audit import AuditLog
from app.models.user import User
from app.services.audit.http import AuditHTTPMiddleware


@pytest_asyncio.fixture
async def audited_platform(tmp_path, monkeypatch):
    from app.services.audit.capture import install_audit_capture

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'audit.db'}")
    sessions = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    monkeypatch.setattr(database, "AsyncSessionLocal", sessions)
    monkeypatch.setattr(settings, "AGENT_FILES_ROOT", str(tmp_path / "workspace"))
    async with engine.begin() as connection:
        await connection.run_sync(database.Base.metadata.create_all)
    async with sessions() as db:
        db.add_all([
            User(id=11, username="auditor", email="audit@example.test", hashed_password="x", is_superuser=True),
            User(id=12, username="owner", email="owner@example.test", hashed_password=get_password_hash("test-example-pass")),
            Agent(id=13, name="审计测试助手", owner_id=12, is_active=True),
        ])
        await db.flush()
        db.add(AgentSession(id=14, agent_id=13, user_id=12, title="初始会话名", messages=[]))
        db.add(AuditLog(id=41, action="login", module="auth", status="success"))
        await db.commit()

    install_audit_capture()
    app = FastAPI()
    app.add_middleware(AuditHTTPMiddleware)
    app.include_router(audit.router, prefix="/api/v1")
    app.include_router(auth.router, prefix="/api/v1")
    app.include_router(conversations.router, prefix="/api/v2")
    app.include_router(files.router, prefix="/api/v2")
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            yield client, sessions
    finally:
        await engine.dispose()


def _headers(user: int) -> dict:
    return {"Authorization": f"Bearer {create_access_token(user)}", "User-Agent": "Audit-Test"}


@pytest.mark.asyncio
async def test_http_read_reject_login_and_conversation_changes(audited_platform):
    client, _sessions = audited_platform
    owner, auditor = _headers(12), _headers(11)
    endpoint = "/api/v2/conversations/14"
    changed = await client.patch(endpoint, headers=owner, json={"title": "巡检讨论", "is_pinned": True})
    assert changed.status_code == 200, changed.text
    correlation = changed.headers["x-request-id"]
    audit_url = "/api/v1/audit"
    assert (await client.get(audit_url, headers=owner)).status_code == 403
    response = await client.get(audit_url, headers=auditor, params={"request_id": correlation})
    assert response.status_code == 200
    events = response.json()["items"]
    assert {item["event_type"] for item in events} == {"change", "request"}
    operation = next(item for item in events if item["event_type"] == "request")
    assert operation["status_code"] == 200 and operation["username"] == "owner"
    assert operation["request_path"] == "/api/v2/conversations/{session_id}"
    change = next(item for item in events if item["event_type"] == "change")
    assert {"title", "pinned_at"} <= set(change["changed_fields"])
    assert "before_data" not in change and "after_data" not in change
    detail_url = f"{audit_url}/{change['id']}"
    assert (await client.get(detail_url, headers=owner)).status_code == 403
    details = (await client.get(detail_url, headers=auditor)).json()
    assert details["before_data"]["title"] == "初始会话名"
    assert details["after_data"]["title"] == "巡检讨论"
    assert details["before_data"]["pinned_at"] is None
    assert details["after_data"]["pinned_at"]
    legacy = (await client.get(audit_url, headers=auditor, params={"event_type": "legacy"})).json()
    assert [item["id"] for item in legacy["items"]] == [41]

    for response, expected in (
        (await client.get(endpoint, headers=owner, params={"secret": "query-must-not-be-stored"}), "success"),
        (await client.patch(endpoint, headers=owner, json={"title": " "}), "failed"),
        (await client.patch(endpoint, json={"title": "不允许修改"}), "denied"),
        (await client.post("/api/v1/auth/login", json={"username": "owner", "password": "invalid-example-pass"}), "denied"),
    ):
        items = (await client.get(audit_url, headers=auditor, params={"request_id": response.headers["x-request-id"]})).json()["items"]
        assert len(items) == 1, items
        assert items[0]["status"] == expected and items[0]["event_type"] == "request"
        assert "query-must-not-be-stored" not in str(items) and "invalid-example-pass" not in str(items)


@pytest.mark.asyncio
async def test_file_activity_only_captures_attachment_metadata(audited_platform):
    client, sessions = audited_platform
    owner = _headers(12)
    upload = await client.post("/api/v2/conversations/14/files", headers=owner,
                               files={"file": ("notes.txt", b"sensitive-body", "text/plain")})
    assert upload.status_code == 200, upload.text
    deleted = await client.delete("/api/v2/conversations/14/files/notes.txt", headers=owner)
    assert deleted.status_code == 200, deleted.text
    async with sessions() as db:
        rows = (await db.scalars(select(AuditLog).where(AuditLog.resource_type == "workspace_files")
                                 .order_by(AuditLog.id))).all()
    assert len(rows) == 2
    assert [(row.action, row.request_id) for row in rows] == [
        ("create", upload.headers["x-request-id"]), ("delete", deleted.headers["x-request-id"]),
    ]
    expected = {"name": "notes.txt", "size": len(b"sensitive-body")}
    assert rows[0].before_data is None and rows[0].after_data == expected
    assert rows[1].before_data == expected and rows[1].after_data is None
