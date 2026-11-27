"""Conversation Memory API：设置 / 状态 / 重建 / 排除 / 分享 与越权防护。"""
from __future__ import annotations

import uuid

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.chat.memory import router
from app.api.deps import get_current_user
from app.core.database import Base, get_db
from app.models.agent import Agent, AgentSession
from app.models.conversation_memory import (
    ConversationIndexEntry, ConversationIndexJob, ConversationMemorySetting,
)
from app.models.sandbox import Project
from app.models.user import User


@pytest.mark.asyncio
async def test_memory_api_settings_exclusion_share_and_isolation():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

        async with sessions.begin() as db:
            owner = User(
                username=f"mem_owner_{uuid.uuid4().hex[:6]}",
                email=f"mem_owner_{uuid.uuid4().hex[:6]}@example.com",
                hashed_password="x",
            )
            other = User(
                username=f"mem_other_{uuid.uuid4().hex[:6]}",
                email=f"mem_other_{uuid.uuid4().hex[:6]}@example.com",
                hashed_password="x",
            )
            agent = Agent(name="记忆助手")
            db.add_all([owner, other, agent])
            await db.flush()
            project = Project(name=f"记忆项目-{uuid.uuid4().hex[:6]}", owner_id=owner.id)
            db.add(project)
            await db.flush()
            session = AgentSession(
                agent_id=agent.id, user_id=owner.id, title="可共享会话",
                project_id=project.id, messages=[],
            )
            db.add(session)
            await db.flush()
            entry = ConversationIndexEntry(
                entry_key="turn:a1", session_id=session.id, user_id=owner.id,
                agent_id=agent.id, project_id=project.id, entry_type="turn",
                message_ids=["u1", "a1"], role="turn", content="测试内容",
                content_hash="hash", token_count=3, index_version="cvm1",
                visibility_scope="user",
            )
            db.add(entry)
            session_id, project_id = session.id, project.id

        async def isolated_db():
            async with sessions() as db:
                try:
                    yield db
                except Exception:
                    await db.rollback()
                    raise

        current = {"user": owner}
        app = FastAPI()
        app.include_router(router, prefix="/api/v2")
        app.dependency_overrides[get_db] = isolated_db
        app.dependency_overrides[get_current_user] = lambda: current["user"]

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            settings_res = await client.get("/api/v2/memory/settings")
            assert settings_res.status_code == 200
            assert settings_res.json()["settings"]["history_enabled"] is True

            patched = await client.patch(
                "/api/v2/memory/settings", json={"auto_retrieval_enabled": False},
            )
            assert patched.status_code == 200
            assert patched.json()["settings"]["auto_retrieval_enabled"] is False
            async with sessions() as db:
                row = (await db.execute(
                    select(ConversationMemorySetting).where(
                        ConversationMemorySetting.user_id == owner.id
                    )
                )).scalar_one()
                assert row.auto_retrieval_enabled is False

            reindex = await client.post("/api/v2/memory/reindex")
            assert reindex.status_code == 200
            assert reindex.json()["status"] == "queued"
            async with sessions() as db:
                job = (await db.execute(
                    select(ConversationIndexJob).where(
                        ConversationIndexJob.user_id == owner.id
                    )
                )).scalar_one()
                assert job.job_type == "reindex_user"

            excluded = await client.put(
                f"/api/v2/memory/conversations/{session_id}/exclusion",
                json={"excluded": True, "reason": "私人内容"},
            )
            assert excluded.status_code == 200
            async with sessions() as db:
                row = (await db.execute(
                    select(ConversationIndexEntry).where(
                        ConversationIndexEntry.session_id == session_id
                    )
                )).scalar_one()
                assert row.is_excluded is True

            shared = await client.post(
                f"/api/v2/memory/conversations/{session_id}/share",
                json={"project_id": project_id},
            )
            assert shared.status_code == 200
            assert shared.json()["project_id"] == project_id
            async with sessions() as db:
                row = (await db.execute(
                    select(ConversationIndexEntry).where(
                        ConversationIndexEntry.session_id == session_id
                    )
                )).scalar_one()
                assert row.visibility_scope == "project"

            revoked = await client.delete(f"/api/v2/memory/conversations/{session_id}/share")
            assert revoked.status_code == 200
            assert revoked.json()["revoked"] is True

            # 其他用户：读取统一设置可以，但操作 owner 的会话必须 404
            current["user"] = other
            denied = await client.put(
                f"/api/v2/memory/conversations/{session_id}/exclusion",
                json={"excluded": True},
            )
            assert denied.status_code == 404
            denied_share = await client.post(
                f"/api/v2/memory/conversations/{session_id}/share",
                json={"project_id": project_id},
            )
            assert denied_share.status_code == 404
    finally:
        await engine.dispose()
