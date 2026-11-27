"""Conversation Memory（历史会话检索 MVP）：索引、检索、ACL、删除一致性与工具。"""
from __future__ import annotations

import math
import uuid
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import pytest
import pytest_asyncio
from sqlalchemy import delete, select

from app.core.config import settings
from app.core.database import AsyncSessionLocal, Base, init_db
from app.models.agent import Agent, AgentSession
from app.models.conversation_memory import (
    ConversationIndexEntry, ConversationIndexJob, ConversationMemoryExclusion,
    ConversationMemorySetting, ConversationMemoryShare,
)
from app.models.sandbox import Project, ProjectMember
from app.models.user import User
from app.services.conversation_memory import indexer, retrieval
from app.services.conversation_memory.context_builder import build_history_context
from app.services.conversation_memory.indexer import (
    delete_session_index, index_session, mark_session_deleted,
)
from app.services.conversation_memory.planner import parse_plan, plan_retrieval
from app.services.conversation_memory.repository import (
    enqueue_index_job, load_settings, set_exclusion, share_to_project, revoke_share,
    update_settings,
)
from app.services.conversation_memory.retrieval import (
    ConversationMemoryService, HistoryItem, HistoryRetrievalResult,
)
from app.services.conversation_memory.tools import execute_history_search
from app.services.agent_runtime.event_protocol import to_v2_event
from app.services.agent_runtime.tools.policy import get_tool_risk


class FakeEmbeddingProvider:
    """确定性离线向量（不依赖网络/模型下载），仅用于单元测试。"""

    def __init__(self, model: str) -> None:
        self.name = "fake"
        self.model = model
        self.dimension = 8

    async def embed(self, texts: List[str]) -> List[List[float]]:
        vectors: List[List[float]] = []
        keywords = ["架构", "部署", "预算", "历史", "记忆", "项目", "数据", "方案"]
        for text in texts:
            vector = [0.05] * self.dimension
            for index, keyword in enumerate(keywords):
                if keyword in text:
                    vector[index] += 1.0
            vector[7] += (len(text) % 7) / 100.0
            norm = math.sqrt(sum(value * value for value in vector)) or 1.0
            vectors.append([value / norm for value in vector])
        return vectors


def _provider(model: str) -> FakeEmbeddingProvider:
    return FakeEmbeddingProvider(model)


@pytest_asyncio.fixture(autouse=True)
async def _clean_memory_tables():
    await init_db()
    yield
    async with AsyncSessionLocal() as db:
        for model in (
            ConversationIndexEntry, ConversationIndexJob, ConversationMemorySetting,
            ConversationMemoryExclusion, ConversationMemoryShare,
        ):
            await db.execute(delete(model))
        await db.commit()


def _message(message_id: str, role: str, content: str, created_at: str) -> Dict[str, Any]:
    return {
        "message_id": message_id, "role": role, "content": content,
        "created_at": created_at,
    }


async def _seed(
    db,
    *,
    user: User,
    agent: Agent,
    title: str = "历史会话",
    project_id: Optional[int] = None,
    messages: Optional[List[Dict[str, Any]]] = None,
) -> AgentSession:
    session = AgentSession(
        agent_id=agent.id, user_id=user.id, title=title, project_id=project_id,
        messages=messages or [],
    )
    db.add(session)
    await db.flush()
    return session


async def _make_user(db, name: str) -> User:
    user = User(
        username=f"{name}_{uuid.uuid4().hex[:8]}",
        email=f"{name}_{uuid.uuid4().hex[:8]}@example.com",
        hashed_password="x",
    )
    db.add(user)
    await db.flush()
    return user


async def _make_agent(db) -> Agent:
    agent = Agent(name=f"助手-{uuid.uuid4().hex[:6]}")
    db.add(agent)
    await db.flush()
    return agent


@pytest.mark.asyncio
async def test_index_builds_turn_and_segment_idempotently(monkeypatch):
    provider = _provider(f"fake_index_{uuid.uuid4().hex[:6]}")
    monkeypatch.setattr(indexer, "get_embedding_provider", lambda: provider)
    async with AsyncSessionLocal() as db:
        user = await _make_user(db, "index")
        agent = await _make_agent(db)
        session = await _seed(
            db, user=user, agent=agent,
            messages=[
                _message("u1", "user", "我们确定的 Agent Runtime 架构是什么？", "2026-11-20T10:00:00"),
                _message("a1", "assistant", "确定由 DeepAgents 负责执行，LangGraph 负责团队控制。",
                         "2026-11-20T10:00:05"),
                _message("u2", "user", "部署方案定在哪天？", "2026-11-21T09:00:00"),
                _message("a2", "assistant", "部署方案定在下周一，使用 Docker 重建。",
                         "2026-11-21T09:00:05"),
            ],
        )
        stats = await index_session(db, session)
        await db.commit()
        assert stats.created == 3  # 2 个 turn + 1 个 segment（首轮不足 2 轮不建 segment）
        rows = (await db.execute(
            select(ConversationIndexEntry).where(ConversationIndexEntry.session_id == session.id)
        )).scalars().all()
        assert {row.entry_type for row in rows} == {"turn", "segment"}
        assert all(row.embedding_status == "indexed" for row in rows)
        assert len({row.entry_key for row in rows}) == 3

        again = await index_session(db, session)
        await db.commit()
        assert again.created == 0
        assert again.embedded == 0
        assert len((await db.execute(
            select(ConversationIndexEntry).where(ConversationIndexEntry.session_id == session.id)
        )).scalars().all()) == 3


@pytest.mark.asyncio
async def test_search_isolates_users_and_supports_dense(monkeypatch):
    model = f"fake_search_{uuid.uuid4().hex[:6]}"
    provider = _provider(model)
    monkeypatch.setattr(indexer, "get_embedding_provider", lambda: provider)

    async def fake_embed_query(text: str) -> List[float]:
        return (await provider.embed([text]))[0]

    monkeypatch.setattr(retrieval, "embed_query", fake_embed_query)
    monkeypatch.setattr(retrieval, "get_embedding_provider", lambda: provider)

    async with AsyncSessionLocal() as db:
        alice = await _make_user(db, "alice")
        bob = await _make_user(db, "bob")
        agent = await _make_agent(db)
        alice_session = await _seed(
            db, user=alice, agent=agent, title="架构讨论",
            messages=[
                _message("u1", "user", "请确认 Agent Runtime 架构", "2026-11-20T10:00:00"),
                _message("a1", "assistant", "架构确定为 DeepAgents 执行 + LangGraph 团队控制。",
                         "2026-11-20T10:00:05"),
            ],
        )
        bob_session = await _seed(
            db, user=bob, agent=agent, title="Bob 私人会话",
            messages=[
                _message("u2", "user", "我的 Agent Runtime 架构偏好是极简", "2026-11-20T11:00:00"),
                _message("a2", "assistant", "已记录你的架构偏好。", "2026-11-20T11:00:05"),
            ],
        )
        await index_session(db, alice_session)
        await index_session(db, bob_session)
        await db.commit()

        service = ConversationMemoryService(db)
        alice_result = await service.search(
            user_id=alice.id, query="Agent Runtime 架构", mode="hybrid",
        )
        assert alice_result.items
        assert all(item.session_id == alice_session.id for item in alice_result.items)
        # 原始来源必须存在且属于 Alice
        bob_result = await service.search(
            user_id=alice.id, query="极简 架构偏好", mode="hybrid",
        )
        assert all(item.session_id != bob_session.id for item in bob_result.items)

        bob_own = await service.search(user_id=bob.id, query="Agent Runtime 架构", mode="bm25")
        assert all(item.session_id == bob_session.id for item in bob_own.items)

        # 直接按来源 ID 读取也执行同样的 ACL
        alice_source = await service.get_source(
            user_id=bob.id, source_id=f"history_src_{alice_result.items[0].entry_id}",
        )
        assert alice_source is None


@pytest.mark.asyncio
async def test_project_share_and_revoke(monkeypatch):
    provider = _provider(f"fake_share_{uuid.uuid4().hex[:6]}")
    monkeypatch.setattr(indexer, "get_embedding_provider", lambda: provider)
    async with AsyncSessionLocal() as db:
        owner = await _make_user(db, "owner")
        member = await _make_user(db, "member")
        agent = await _make_agent(db)
        project = Project(name=f"项目-{uuid.uuid4().hex[:6]}", owner_id=owner.id)
        db.add(project)
        await db.flush()
        db.add(ProjectMember(project_id=project.id, user_id=member.id, role="viewer"))
        session = await _seed(
            db, user=owner, agent=agent, title="项目决策", project_id=project.id,
            messages=[
                _message("u1", "user", "项目架构确定用哪种方案？", "2026-11-22T10:00:00"),
                _message("a1", "assistant", "项目架构确定使用 DeepAgents + LangGraph 方案。",
                         "2026-11-22T10:00:05"),
            ],
        )
        await index_session(db, session)
        await db.commit()

        service = ConversationMemoryService(db)
        before = await service.search(user_id=member.id, query="项目架构方案", mode="bm25")
        assert before.items == []

        share = await share_to_project(
            db, user_id=owner.id, conversation_id=session.id, project_id=project.id,
        )
        await db.commit()
        assert share.project_id == project.id
        after = await service.search(user_id=member.id, query="项目架构方案", mode="bm25")
        assert after.items and after.items[0].scope == "project"
        assert after.items[0].conversation_title == "项目决策"

        # 用户关闭项目记忆读取后：共享历史与来源均不可访问
        await update_settings(db, member.id, {"project_memory_enabled": False})
        await db.commit()
        blocked = await service.search(user_id=member.id, query="项目架构方案", mode="bm25")
        assert blocked.items == []
        assert await service.get_source(
            user_id=member.id, source_id=f"history_src_{after.items[0].entry_id}",
        ) is None
        await update_settings(db, member.id, {"project_memory_enabled": True})
        await db.commit()

        await revoke_share(db, user_id=owner.id, conversation_id=session.id)
        await db.commit()
        revoked = await service.search(user_id=member.id, query="项目架构方案", mode="bm25")
        assert revoked.items == []


@pytest.mark.asyncio
async def test_delete_and_exclusion_remove_from_results(monkeypatch):
    provider = _provider(f"fake_delete_{uuid.uuid4().hex[:6]}")
    monkeypatch.setattr(indexer, "get_embedding_provider", lambda: provider)
    async with AsyncSessionLocal() as db:
        user = await _make_user(db, "delete")
        agent = await _make_agent(db)
        session = await _seed(
            db, user=user, agent=agent,
            messages=[
                _message("u1", "user", "预算上限是多少？", "2026-11-23T10:00:00"),
                _message("a1", "assistant", "预算上限确定为 3000 元。", "2026-11-23T10:00:05"),
            ],
        )
        await index_session(db, session)
        await db.commit()
        service = ConversationMemoryService(db)
        assert (await service.search(user_id=user.id, query="预算上限", mode="bm25")).items

        await set_exclusion(db, user_id=user.id, conversation_id=session.id, excluded=True)
        await db.commit()
        assert (await service.search(user_id=user.id, query="预算上限", mode="bm25")).items == []
        await set_exclusion(db, user_id=user.id, conversation_id=session.id, excluded=False)
        await db.commit()
        assert (await service.search(user_id=user.id, query="预算上限", mode="bm25")).items

        await mark_session_deleted(db, session.id)
        await db.commit()
        assert (await service.search(user_id=user.id, query="预算上限", mode="bm25")).items == []
        await delete_session_index(db, session.id)
        await db.commit()
        remaining = (await db.execute(
            select(ConversationIndexEntry).where(ConversationIndexEntry.session_id == session.id)
        )).scalars().all()
        assert remaining == []


@pytest.mark.asyncio
async def test_source_resolver_rechecks_session_existence(monkeypatch):
    provider = _provider(f"fake_source_{uuid.uuid4().hex[:6]}")
    monkeypatch.setattr(indexer, "get_embedding_provider", lambda: provider)
    async with AsyncSessionLocal() as db:
        user = await _make_user(db, "source")
        agent = await _make_agent(db)
        session = await _seed(
            db, user=user, agent=agent,
            messages=[
                _message("u1", "user", "记录一下决定", "2026-11-24T10:00:00"),
                _message("a1", "assistant", "决定采用方案 A。", "2026-11-24T10:00:05"),
            ],
        )
        await index_session(db, session)
        await db.commit()
        service = ConversationMemoryService(db)
        result = await service.search(user_id=user.id, query="决定 方案", mode="bm25")
        source = await service.get_source(
            user_id=user.id, source_id=f"history_src_{result.items[0].entry_id}",
        )
        assert source and "方案 A" in source["content"]

        await db.delete(session)
        await db.commit()
        assert await service.get_source(
            user_id=user.id, source_id=f"history_src_{result.items[0].entry_id}",
        ) is None


def test_planner_parse_rules():
    plan = parse_plan(
        '{"need_history": true, "scope_hint": "current_project", '
        '"queries": ["之前确定的架构", "架构"], "reason": "续接既有方案"}'
    )
    assert plan.need_history and plan.queries == ["之前确定的架构", "架构"]
    with pytest.raises(Exception):
        parse_plan('{"need_history": true, "queries": []}')
    no_history = parse_plan('{"need_history": false, "queries": ["不应保留"]}')
    assert no_history.queries == []


@pytest.mark.asyncio
async def test_planner_falls_back_without_blocking(monkeypatch):
    async def broken(*args, **kwargs):
        raise RuntimeError("model unavailable")

    monkeypatch.setattr("app.services.conversation_memory.planner._invoke", broken)
    outcome = await plan_retrieval(
        provider={}, model_config={}, request="继续上次的架构改造", timeout=2,
    )
    assert outcome.fallback is True
    assert outcome.plan is None


def test_context_builder_budget_and_empty():
    result = HistoryRetrievalResult(retrieval_id="r", query="q", scope="auto", items=[])
    text, sources, stats = build_history_context(result)
    assert text == "" and sources == [] and stats["injected_items"] == 0

    item = HistoryItem(
        entry_id=1, session_id=10, user_id=1, project_id=None,
        entry_type="turn", role="turn", message_ids=["u1", "a1"],
        content="之前确定的架构是 DeepAgents 负责执行。" * 10,
        created_at=None, score=0.5, conversation_title="架构讨论",
    )
    result.items = [item]
    text, sources, stats = build_history_context(result, budget_tokens=260)
    assert "不可信资料" in text
    assert sources and sources[0]["conversation_id"] == 10
    assert stats["injected_tokens"] <= 260


@pytest.mark.asyncio
async def test_memory_tool_respects_user_switch(monkeypatch):
    provider = _provider(f"fake_tool_{uuid.uuid4().hex[:6]}")
    monkeypatch.setattr(indexer, "get_embedding_provider", lambda: provider)
    async with AsyncSessionLocal() as db:
        user = await _make_user(db, "tool")
        agent = await _make_agent(db)
        session = await _seed(
            db, user=user, agent=agent,
            messages=[
                _message("u1", "user", "上次的部署方案是什么？", "2026-11-25T10:00:00"),
                _message("a1", "assistant", "上次确定部署方案为 Docker Compose。", "2026-11-25T10:00:05"),
            ],
        )
        await index_session(db, session)
        await db.commit()
        ctx = SimpleNamespace(
            scope=SimpleNamespace(user_id=user.id), agent_config={}, project_id=None,
        )
        ok = await execute_history_search(ctx, {"query": "部署方案", "limit": 3})
        assert ok.status == "succeeded"
        assert ok.metadata["sources"]
        assert "conversation_history" in ok.text

        async with AsyncSessionLocal() as db2:
            await update_settings(db2, user.id, {"history_enabled": False})
            await db2.commit()
        disabled = await execute_history_search(ctx, {"query": "部署方案"})
        assert disabled.status == "failed"
        assert "已关闭" in (disabled.error or "")


def test_history_sources_event_mapping():
    envelope = to_v2_event(
        run_id="run-1", sequence=3, event_type="history_sources",
        payload={"sources": [{"source_id": "history_src_1", "conversation_id": 2}],
                 "queries": ["架构"]},
    )
    assert envelope["event"] == "history.sources"
    assert envelope["data"]["sources"][0]["source_id"] == "history_src_1"


def test_memory_tool_risk_registered():
    assert get_tool_risk("search_conversation_history").risk.value == "read"
    assert get_tool_risk("get_conversation_source").risk.value == "read"


@pytest.mark.asyncio
async def test_index_worker_processes_index_and_delete_jobs(monkeypatch):
    provider = _provider(f"fake_worker_{uuid.uuid4().hex[:6]}")
    monkeypatch.setattr(indexer, "get_embedding_provider", lambda: provider)
    from app.services.conversation_memory.worker import ConversationIndexWorker

    async with AsyncSessionLocal() as db:
        user = await _make_user(db, "worker")
        agent = await _make_agent(db)
        session = await _seed(
            db, user=user, agent=agent,
            messages=[
                _message("u1", "user", "记录部署方案", "2026-11-26T10:00:00"),
                _message("a1", "assistant", "部署方案使用 Docker Compose。", "2026-11-26T10:00:05"),
            ],
        )
        await enqueue_index_job(
            db, session_id=session.id, user_id=user.id, message_ids=["a1"],
        )
        await db.commit()
        session_id = session.id

    worker = ConversationIndexWorker()
    processed = await worker.pump(limit=5)
    assert processed >= 1
    async with AsyncSessionLocal() as db:
        entries = (await db.execute(
            select(ConversationIndexEntry).where(
                ConversationIndexEntry.session_id == session_id
            )
        )).scalars().all()
        assert entries and all(entry.embedding_status == "indexed" for entry in entries)
        job = (await db.execute(
            select(ConversationIndexJob).where(ConversationIndexJob.session_id == session_id)
        )).scalar_one()
        assert job.status == "done"

        from app.services.conversation_memory.repository import enqueue_delete_job

        await enqueue_delete_job(db, session_id=session_id, user_id=user.id)
        await db.commit()

    await worker.pump(limit=5)
    async with AsyncSessionLocal() as db:
        remaining = (await db.execute(
            select(ConversationIndexEntry).where(
                ConversationIndexEntry.session_id == session_id
            )
        )).scalars().all()
        assert remaining == []


@pytest.mark.asyncio
async def test_prepare_history_context_uses_planner_and_budget(monkeypatch):
    provider = _provider(f"fake_prepare_{uuid.uuid4().hex[:6]}")
    monkeypatch.setattr(indexer, "get_embedding_provider", lambda: provider)
    async with AsyncSessionLocal() as db:
        user = await _make_user(db, "prepare")
        agent = await _make_agent(db)
        session = await _seed(
            db, user=user, agent=agent, title="部署决策",
            messages=[
                _message("u1", "user", "部署方案确定了吗？", "2026-11-26T10:00:00"),
                _message("a1", "assistant", "部署方案确定为 Docker Compose 单机模式。",
                         "2026-11-26T10:00:05"),
            ],
        )
        await index_session(db, session)
        await db.commit()
        user_id = user.id

    from app.services.conversation_memory import service as memory_service
    from app.services.conversation_memory.planner import PlannerOutcome, RetrievalPlan

    async def planned(*args, **kwargs):
        return PlannerOutcome(
            plan=RetrievalPlan(
                need_history=True, scope_hint="user_history",
                queries=["部署方案"], reason="续接部署决策",
            ),
            fallback=False,
        )

    monkeypatch.setattr(memory_service, "plan_retrieval", planned)
    prepared = await memory_service.prepare_history_context(
        run_id="run-eval", user_id=user_id, project_id=None,
        messages=[{"role": "user", "content": "继续上次的部署方案"}],
        provider={}, model_config={}, agent_config={},
    )
    assert prepared.enabled is True
    assert prepared.sources and prepared.context
    assert "不可信资料" in prepared.context
    assert prepared.stats["injected_tokens"] <= 1200

    async def no_history(*args, **kwargs):
        return PlannerOutcome(
            plan=RetrievalPlan(need_history=False, queries=[], reason="普通问答"),
            fallback=False,
        )

    monkeypatch.setattr(memory_service, "plan_retrieval", no_history)
    skipped = await memory_service.prepare_history_context(
        run_id="run-eval-2", user_id=user_id, project_id=None,
        messages=[{"role": "user", "content": "今天天气怎么样"}],
        provider={}, model_config={}, agent_config={},
    )
    assert skipped.enabled is False
    assert skipped.context == ""


def test_history_sources_event_storage_not_truncated():
    from app.services.agent_runtime.events import RuntimeEvent, event_storage_payload

    sources = [{
        "source_id": f"history_src_{index}", "conversation_id": index,
        "conversation_title": f"会话 {index}", "message_ids": [f"m{index}"],
        "snippet": "片段" * 80, "created_at": "2026-11-20T10:00:00",
    } for index in range(1, 6)]
    payload = event_storage_payload(RuntimeEvent(
        type="history_sources", run_id="run-evt", data={"sources": sources, "queries": ["架构"]},
    ))
    assert len(payload["sources"]) == 5
    assert payload["sources"][0]["source_id"] == "history_src_1"
