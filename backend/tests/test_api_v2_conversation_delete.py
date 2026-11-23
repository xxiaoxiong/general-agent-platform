"""会话删除在外键启用时保留历史 Run、事件和评测。"""

from datetime import datetime, timedelta

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import event
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.deps import get_current_user
from app.api.chat.conversations import router
from app.core.database import Base, get_db
from app.models.agent import Agent, AgentRun, AgentRunEvent, AgentSession
from app.models.evolution import AgentEvaluation
from app.models.user import User


@pytest.mark.asyncio
async def test_delete_conversation_detaches_terminal_references_only():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    sessions = async_sessionmaker(engine, expire_on_commit=False)

    @event.listens_for(engine.sync_engine, "connect")
    def _enable_foreign_keys(connection, _):
        cursor = connection.cursor()
        cursor.execute("PRAGMA foreign_keys = ON")
        cursor.close()

    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

        async with sessions.begin() as db:
            owner = User(username="workspace_owner", email="owner@example.com", hashed_password="fixture")
            other = User(username="other_owner", email="other@example.com", hashed_password="fixture")
            agent = Agent(name="验收助手")
            db.add_all([owner, other, agent])
            await db.flush()
            completed = AgentSession(
                agent_id=agent.id,
                user_id=owner.id,
                title="已完成",
                active_run_id="run-completed",
                active_run_lease_expires_at=datetime.utcnow() + timedelta(minutes=1),
            )
            waiting = AgentSession(agent_id=agent.id, user_id=owner.id, title="等待审批")
            inaccessible = AgentSession(agent_id=agent.id, user_id=other.id, title="其他用户")
            db.add_all([completed, waiting, inaccessible])
            await db.flush()
            run = AgentRun(
                id="run-completed",
                agent_id=agent.id,
                session_id=completed.id,
                user_id=owner.id,
                thread_id="thread-completed",
                status="completed",
                final_text="保留运行结果",
            )
            paused = AgentRun(
                id="run-waiting",
                agent_id=agent.id,
                session_id=waiting.id,
                user_id=owner.id,
                thread_id="thread-waiting",
                status="waiting_approval",
            )
            db.add_all([run, paused])
            await db.flush()
            run_event = AgentRunEvent(run_id=run.id, sequence=1, event_type="run_completed")
            evaluation = AgentEvaluation(
                run_id=run.id, agent_id=agent.id, session_id=completed.id,
                status="computed", overall=0.9, langfuse_trace_id="retained-trace",
            )
            db.add_all([run_event, evaluation])
            await db.flush()
            original_run_updated_at, original_evaluation_updated_at = run.updated_at, evaluation.updated_at

        async def isolated_db():
            async with sessions() as db:
                try:
                    yield db
                except Exception:
                    await db.rollback()
                    raise

        app = FastAPI()
        app.include_router(router, prefix="/api/v2")
        app.dependency_overrides[get_db] = isolated_db
        app.dependency_overrides[get_current_user] = lambda: owner

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            missing = await client.delete(f"/api/v2/conversations/{inaccessible.id}")
            assert missing.status_code == 404
            busy = await client.delete(f"/api/v2/conversations/{waiting.id}")
            assert busy.status_code == 409
            deleted = await client.delete(f"/api/v2/conversations/{completed.id}")
            assert deleted.status_code == 200
            assert deleted.json() == {"message": "已删除"}

        async with sessions() as db:
            assert await db.get(AgentSession, completed.id) is None
            assert await db.get(AgentSession, waiting.id) is not None
            assert await db.get(AgentSession, inaccessible.id) is not None
            assert (await db.get(AgentRun, paused.id)).session_id == waiting.id
            retained_run = await db.get(AgentRun, run.id)
            assert retained_run.session_id is None
            assert retained_run.final_text == "保留运行结果"
            assert retained_run.updated_at == original_run_updated_at
            assert (await db.get(AgentRunEvent, run_event.id)).run_id == run.id
            retained_evaluation = await db.get(AgentEvaluation, evaluation.id)
            assert retained_evaluation.session_id is None
            assert retained_evaluation.overall == 0.9
            assert retained_evaluation.langfuse_trace_id == "retained-trace"
            assert retained_evaluation.updated_at == original_evaluation_updated_at
    finally:
        await engine.dispose()
