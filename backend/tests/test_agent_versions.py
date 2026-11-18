"""P2-01：AgentVersion 不可变发布版本与 Run 绑定。

运行：
    pytest backend/tests/test_agent_versions.py -q
"""
import pytest
import pytest_asyncio
from sqlalchemy import delete, select

from app.assistant.versions import (
    build_snapshot,
    config_from_version,
    ensure_version,
    latest_version,
    publish_version,
)
from app.core.database import AsyncSessionLocal, engine, init_db
from app.models.agent import Agent, AgentRun, AgentVersion
from app.models.user import User


@pytest_asyncio.fixture(autouse=True)
async def _db():
    await init_db()
    async with AsyncSessionLocal() as s:
        for model in (AgentRun, AgentVersion, Agent, User):
            await s.execute(delete(model))
        await s.commit()
    yield
    await engine.dispose()


async def _mk_agent(**overrides) -> int:
    async with AsyncSessionLocal() as s:
        agent = Agent(
            name=overrides.get("name", "版本测试 Agent"),
            system_prompt=overrides.get("system_prompt", "初始指令"),
            model_config_id=1,
            tools=["calculator"],
            temperature="0.3",
            max_iterations=5,
            is_active=True,
        )
        s.add(agent)
        await s.commit()
        return agent.id


class TestPublish:
    @pytest.mark.asyncio
    async def test_publish_increments_and_snapshots(self):
        agent_id = await _mk_agent(system_prompt="v1 指令")
        async with AsyncSessionLocal() as s:
            v1 = await publish_version(s, agent_id, created_by=42)
            assert v1.version_no == 1
            assert v1.snapshot["system_prompt"] == "v1 指令"
            assert v1.snapshot["model_policy"]["temperature"] == "0.3"
            assert v1.snapshot["created_by"] == 42
            await s.commit()

    @pytest.mark.asyncio
    async def test_snapshot_immutable_after_agent_edit(self):
        agent_id = await _mk_agent(system_prompt="原始指令")
        async with AsyncSessionLocal() as s:
            v1 = await publish_version(s, agent_id)
            await s.commit()

        # 修改 Agent 行
        async with AsyncSessionLocal() as s:
            agent = (await s.execute(
                select(Agent).where(Agent.id == agent_id)
            )).scalar_one()
            agent.system_prompt = "改过的指令"
            agent.tools = ["web_search"]
            await s.commit()

        async with AsyncSessionLocal() as s:
            row = (await s.execute(
                select(AgentVersion).where(AgentVersion.id == v1.id)
            )).scalar_one()
            assert row.snapshot["system_prompt"] == "原始指令", "旧版本快照不得随 Agent 修改变化"
            assert row.snapshot["tools"] == ["calculator"]

            v2 = await publish_version(s, agent_id)
            await s.commit()
            assert v2.version_no == 2
            assert v2.snapshot["system_prompt"] == "改过的指令"


class TestEnsureVersion:
    @pytest.mark.asyncio
    async def test_auto_publishes_v1_and_reuses_latest(self):
        agent_id = await _mk_agent()
        async with AsyncSessionLocal() as s:
            v1 = await ensure_version(s, agent_id, created_by=1)
            assert v1.version_no == 1
            again = await ensure_version(s, agent_id, created_by=1)
            assert again.id == v1.id, "已有版本时不得重复发布"
            await s.commit()

    @pytest.mark.asyncio
    async def test_synthetic_agent_returns_none(self):
        """合成 agent_config（无 Agent 行）不绑定版本、不报错。"""
        async with AsyncSessionLocal() as s:
            assert await ensure_version(s, 999999) is None
            await s.commit()

    @pytest.mark.asyncio
    async def test_latest_version(self):
        agent_id = await _mk_agent()
        async with AsyncSessionLocal() as s:
            await publish_version(s, agent_id)
            v2 = await publish_version(s, agent_id)
            await s.commit()
            latest = await latest_version(s, agent_id)
            assert latest.id == v2.id and latest.version_no == 2


class TestConfigFromVersion:
    @pytest.mark.asyncio
    async def test_roundtrip_maps_runtime_config(self):
        agent_id = await _mk_agent(system_prompt="指令 X")
        async with AsyncSessionLocal() as s:
            v = await publish_version(s, agent_id, capability_ids=["general", "web_research"])
            await s.commit()
            cfg = config_from_version(v)
        assert cfg["id"] == agent_id
        assert cfg["agent_version_id"] == v.id
        assert cfg["system_prompt"] == "指令 X"
        assert cfg["temperature"] == "0.3"
        assert cfg["tools"] == ["calculator"]
        assert cfg["capability_ids"] == ["general", "web_research"]


class TestRunBinding:
    @pytest.mark.asyncio
    async def test_run_binds_version(self):
        from app.services.agent_runtime.repositories import RunRepository

        agent_id = await _mk_agent()
        async with AsyncSessionLocal() as s:
            v = await ensure_version(s, agent_id, created_by=7)
            repo = RunRepository(s)
            row, created = await repo.create_run(
                run_id="vtest-run-1", agent_id=agent_id, agent_version_id=v.id,
                session_id=None, user_id=7, thread_id="t-vtest",
            )
            await s.commit()
            assert created
            fresh = await repo.get_run("vtest-run-1")
            assert fresh.agent_version_id == v.id
