"""使用真实 AsyncSession 验证审计快照与事务语义。"""

import asyncio
import json
from contextlib import contextmanager
from datetime import datetime
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import delete, insert, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models.agent import Agent, AgentSession
from app.models.audit import AuditLog
from app.models.base import BaseModel
from app.models.document import DocumentProfile
from app.models.model_provider import ModelProvider
from app.models.user import Role, User, UserRole
from app.models.workflow import Workflow
from app.services.agent_runtime.repositories.session_repository import SessionRepository
from app.services.agent_runtime.worker import RunWorker
from app.services.audit.capture import install_audit_capture, run_audit_origin, worker_audit_context
from app.services.audit.context import AuditContext, current_audit


@pytest_asyncio.fixture
async def sessions():
    install_audit_capture()
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as connection:
            await connection.run_sync(BaseModel.metadata.create_all)
        yield async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    finally:
        await engine.dispose()


@contextmanager
def actor(request_id="req-123"):
    context = AuditContext(request_id, "PATCH", "/api/test", "192.0.2.1", "test", 42, "fixture")
    token = current_audit.set(context)
    try:
        yield context
    finally:
        current_audit.reset(token)


async def changes(sessions):
    async with sessions() as db:
        return (await db.scalars(select(AuditLog).order_by(AuditLog.id))).all()


@pytest.mark.asyncio
async def test_persisted_nested_changes_and_secret_rotation(sessions):
    workflow = Workflow(
        name="审批流程", owner_id=42,
        definition={"nodes": [{"label": "旧步骤", "authorization": "initial-secret"}]},
    )
    provider = ModelProvider(
        name="example", display_name="测试模型", provider_type="openai_compatible",
        base_url="https://login:url-secret@example.test?signature=query-secret", api_key="old-key",
        extra_config={"custom": "old-config-secret"},
    )
    async with sessions.begin() as db:
        db.add_all([workflow, provider])
    assert await changes(sessions) == []

    with actor(), pytest.raises(ValueError):
        async with sessions.begin() as db:
            loaded = await db.get(Workflow, workflow.id)
            loaded.name = "回滚事务"
            await db.flush()
            raise ValueError("测试回滚")
    assert await changes(sessions) == []

    with actor():
        async with sessions.begin() as db:
            loaded_workflow = await db.get(Workflow, workflow.id)
            loaded_provider = await db.get(ModelProvider, provider.id)
            loaded_workflow.definition = {"nodes": [
                {"label": "新步骤", "api_key": "rotated-secret"},
                {"label": 'password="secret with spaces"'},
                {"name": "api_key", "value": "typed-field-secret"},
            ]}
            loaded_provider.api_key = "new-key"
            loaded_provider.extra_config = {"custom": "new-config-secret"}

    logs = {row.resource_type: row for row in await changes(sessions)}
    workflow_log = logs["workflows"]
    assert (workflow_log.event_type, workflow_log.action, workflow_log.request_id) == ("change", "update", "req-123")
    assert (workflow_log.user_id, workflow_log.username, workflow_log.http_method) == (42, "fixture", "PATCH")
    assert workflow_log.before_data["name"] == workflow_log.after_data["name"] == "审批流程"
    assert workflow_log.before_data["definition"]["nodes"][0]["label"] == "旧步骤"
    assert workflow_log.after_data["definition"]["nodes"][0]["label"] == "新步骤"
    assert workflow_log.changed_fields == ["definition"]
    provider_log = logs["model_providers"]
    assert provider_log.changed_fields == ["api_key", "extra_config"]
    for row in logs.values():
        payload = json.dumps([row.before_data, row.after_data])
        for secret in (
            "initial-secret", "rotated-secret", "secret with spaces", "url-secret",
            "old-key", "new-key", "old-config-secret", "new-config-secret", "query-secret", "typed-field-secret",
        ):
            assert secret not in payload


@pytest.mark.asyncio
async def test_conversation_metadata_sql_expressions_and_noop(sessions):
    session = AgentSession(agent_id=1, user_id=42, title="原对话", messages=[{"content": "私有内容"}])
    async with sessions.begin() as db:
        db.add_all([Agent(id=1, name="助手"), session])

    with actor("rename"):
        async with sessions.begin() as db:
            assert await SessionRepository(db).update_metadata(session.id, 42, title="已重命名", is_pinned=True)
    with actor("replay"):
        async with sessions.begin() as db:
            assert await SessionRepository(db).update_metadata(session.id, 42, title="已重命名", is_pinned=True)
            assert not await SessionRepository(db).update_metadata(session.id, 99, title="其他账号")
    with actor("rollback"):
        async with sessions() as db:
            assert await SessionRepository(db).update_metadata(session.id, 42, is_pinned=False)
            await db.rollback()

    row, = await changes(sessions)
    assert row.request_id == "rename"
    assert row.before_data["title"] == "原对话"
    assert row.before_data["pinned_at"] is None
    assert row.after_data["title"] == "已重命名"
    assert row.after_data["title_edited"] is True
    assert datetime.fromisoformat(row.after_data["pinned_at"])
    assert row.before_data["message_count"] == row.after_data["message_count"] == 1
    assert "messages" not in row.after_data
    assert row.changed_fields == ["pinned_at", "title", "title_edited"]


@pytest.mark.asyncio
async def test_bulk_role_replacement_and_orm_create_delete(sessions):
    async with sessions.begin() as db:
        user = User(username="member", email="member@example.test", hashed_password="hidden")
        former_role, new_role = Role(name="former", display_name="旧角色"), Role(name="new", display_name="新角色")
        db.add_all([user, former_role, new_role])
        await db.flush()
        db.add(UserRole(user_id=user.id, role_id=former_role.id))

    with actor("role-edit"):
        async with sessions.begin() as db:
            result = await db.execute(delete(UserRole).where(UserRole.user_id == user.id))
            assert result.rowcount == 1
            db.add(UserRole(user_id=user.id, role_id=new_role.id))
            transient = Role(name="temporary", display_name="临时角色")
            db.add(transient)
            await db.flush()
            transient.display_name = "未写入的角色变更"
            await db.delete(transient)

    events = await changes(sessions)
    deleted_binding, created_binding = [row for row in events if row.resource_type == "user_roles"]
    assert deleted_binding.before_data["role_id"] == former_role.id
    assert deleted_binding.after_data is None
    assert created_binding.before_data is None
    assert created_binding.after_data["role_id"] == new_role.id
    created_role, deleted_role = [row for row in events if row.resource_type == "roles"]
    assert created_role.after_data["is_system"] is False
    assert deleted_role.before_data["display_name"] == "临时角色"
    assert {row.request_id for row in events} == {"role-edit"}


@pytest.mark.asyncio
async def test_durable_worker_keeps_origin_and_limits_snapshot_size(sessions):
    with actor("create-run"):
        origin = run_audit_origin()
    job = RunWorker._job_from_row(SimpleNamespace(
        id="run1", user_id=42, agent_config_snapshot={}, input_payload={"audit_origin": origin},
    ))

    async def publish():
        async with sessions.begin() as db:
            db.add(DocumentProfile(name="手册规范", owner_id=42, document_type="manual"))
            result = await db.execute(
                insert(Workflow.__table__).values(
                    name="复杂流程", owner_id=42,
                    definition={"nodes": [{"label": "中" * 4000} for _ in range(50)]},
                )
            )
            assert result.inserted_primary_key[0] is not None

    with actor("unrelated") as unrelated:
        await asyncio.create_task(publish(), context=worker_audit_context(job.audit_origin))
        assert current_audit.get() is unrelated
    logs = {row.resource_type: row for row in await changes(sessions)}
    assert logs.keys() == {"document_profiles", "workflows"}
    assert {row.request_id for row in logs.values()} == {"create-run"}
    assert logs["document_profiles"].after_data["status"] == "draft"
    snapshot = json.dumps(logs["workflows"].after_data)
    assert len(snapshot) < 66_000
    assert "部分内容已截断" in json.loads(snapshot)["definition"]["nodes"][0]["label"]
