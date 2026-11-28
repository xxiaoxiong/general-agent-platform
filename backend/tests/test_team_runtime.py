"""Agent Team Runtime：Task Board / CAS / 租约 / Message Bus / Artifact /
Member Runtime / Team Graph 接线。

覆盖任务书 §8 的单元与集成场景：
- DAG 校验（自依赖/缺失/循环/角色唯一）、CAS 冲突、并发认领唯一性；
- 租约回收后旧成员完成被拒、完成幂等、重试上限；
- 消息幂等、Inbox 投递与 Ack、跨团队隔离；
- Artifact 引用读取、片段、大小上限与跨团队拒绝；
- Member 独立 Thread、多步骤执行、预算、Checkpoint 恢复与消息唤醒；
- 通用 Team Graph 接入 Task Board 与 Member Runtime 后的规划/并行/综合。
"""
from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
import pytest_asyncio
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy import delete, update

from app.core.database import AsyncSessionLocal, init_db
from app.models.agent import Agent, AgentRun
from app.models.team import (
    TeamArtifact, TeamMember, TeamMessage, TeamRun, TeamTask, TeamTaskDependency,
)
from app.models.user import User
from app.services.agent_runtime.context import (
    ExecutionBudget, ModelUsageCallback, RunScope, RuntimeContext,
)
from app.services.agent_runtime.events import EventBus
from app.services.agent_runtime.persistence import make_run_config
from app.services.agent_runtime.repositories.errors import PersistenceError
from app.services.agent_runtime.team.errors import (
    PlanValidationError, TaskConflict, TaskStateError, TeamNotFound,
)
from app.services.agent_runtime.team.repositories import MemberRepository
from app.services.agent_runtime.team.runtime.member_directory import MemberDirectory
from app.services.agent_runtime.team.runtime.member_factory import MemberAgentFactory
from app.services.agent_runtime.team.runtime.leader_agent import LeaderRunner
from app.services.agent_runtime.team.runtime.member_manager import MemberRuntimeManager
from app.services.agent_runtime.team.runtime.member_profile import MemberProfile
from app.services.agent_runtime.team.services.artifact_store import ArtifactStore
from app.services.agent_runtime.team.services.message_bus import MessageBusService
from app.services.agent_runtime.team.services.task_board import TaskBoardService
from app.services.agent_runtime.team.runtime.team_service import TeamService
from app.services.agent_runtime.team.tools.team_tools import HANDLERS as TEAM_HANDLERS
from app.services.agent_runtime.team_graph import TeamGraphContext, build_team_graph, drive_team_graph
from app.services.agent_runtime.tools.policy import ToolPolicy


class TeamEnv:
    def __init__(self, user_id: int, agent_id: int, run_id: str):
        self.user_id = user_id
        self.agent_id = agent_id
        self.run_id = run_id

    @property
    def team_run_id(self) -> str:
        return self.run_id


async def _create_env(objective: str = "核对交付方案") -> TeamEnv:
    await init_db()
    suffix = uuid.uuid4().hex[:10]
    async with AsyncSessionLocal() as db:
        user = User(
            username=f"team-user-{suffix}",
            email=f"team-{suffix}@example.com",
            hashed_password="x",
        )
        db.add(user)
        await db.flush()
        agent = Agent(name=f"team-agent-{suffix}", owner_id=user.id)
        db.add(agent)
        await db.flush()
        run_id = uuid.uuid4().hex
        db.add(AgentRun(
            id=run_id, agent_id=agent.id, user_id=user.id,
            thread_id=f"team:{run_id}", status="running",
            worker_id="test-worker", lease_version=1,
            lease_expires_at=datetime.utcnow() + timedelta(minutes=10),
        ))
        await db.commit()
        env = TeamEnv(user.id, agent.id, run_id)
    await TeamService().ensure(team_run_id=env.run_id, objective=objective)
    return env


async def _cleanup_env(env: TeamEnv) -> None:
    async with AsyncSessionLocal() as db:
        for model in (TeamArtifact, TeamMessage, TeamTaskDependency, TeamTask, TeamMember, TeamRun):
            await db.execute(delete(model).where(model.team_run_id == env.run_id))
        await db.execute(delete(AgentRun).where(AgentRun.id == env.run_id))
        await db.execute(delete(Agent).where(Agent.id == env.agent_id))
        await db.execute(delete(User).where(User.id == env.user_id))
        await db.commit()


@pytest_asyncio.fixture
async def env():
    env = await _create_env()
    try:
        yield env
    finally:
        await _cleanup_env(env)


async def _member(env: TeamEnv, member_id: str, role: str) -> None:
    async with AsyncSessionLocal() as db:
        await MemberRepository(db).create(
            member_id=member_id,
            team_run_id=env.run_id,
            role=role,
            name=role,
            thread_id=f"team:{env.run_id}:member:{member_id}",
            profile_snapshot={"role": role, "name": role},
            model_snapshot={"name": "fake"},
            budget={"max_model_calls": 4, "max_tool_calls": 4},
            status="ready",
        )
        await db.commit()


# ── Task Board ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_task_board_plan_validation_cas_and_dependencies(env):
    board = TaskBoardService()
    await board.create_task(
        env.run_id, task_id="facts", subject="核对事实", description="交付事实",
        role="事实核对",
    )
    await board.create_task(
        env.run_id, task_id="design", subject="设计方案", description="交付方案",
        role="方案设计", dependencies=["facts"],
    )
    tasks = await board.validate_plan(env.run_id)
    assert [task["id"] for task in tasks] == ["facts", "design"]

    task = await board.get_task(env.run_id, "facts")
    with pytest.raises(TaskConflict):
        await board.update_task(
            env.run_id, "facts", expected_revision=task["revision"] + 5,
            actor={"member_id": "leader", "is_leader": True},
            changes={"subject": "改名"},
        )
    updated = await board.update_task(
        env.run_id, "facts", expected_revision=task["revision"],
        actor={"member_id": "leader", "is_leader": True},
        changes={"subject": "核对原始事实"},
    )
    assert updated["title"] == "核对原始事实"
    assert updated["revision"] == task["revision"] + 1

    # 依赖未完成：不能认领
    await _member(env, "mbr-a", "事实核对")
    with pytest.raises(TaskConflict):
        await board.claim(env.run_id, "design", member_id="mbr-a")

    # 自依赖 / 缺失依赖 / 循环依赖
    await board.create_task(
        env.run_id, task_id="self", subject="自依赖", description="x",
        role="自检", dependencies=["self"],
    )
    with pytest.raises(PlanValidationError, match="自身"):
        await board.validate_plan(env.run_id)
    await board.reset_plan(env.run_id)

    await board.create_task(env.run_id, task_id="a", subject="A", description="x", role="甲")
    await board.create_task(
        env.run_id, task_id="b", subject="B", description="x", role="乙",
        dependencies=["ghost"],
    )
    with pytest.raises(PlanValidationError, match="未登记"):
        await board.validate_plan(env.run_id)
    await board.reset_plan(env.run_id)

    await board.create_task(env.run_id, task_id="a", subject="A", description="x", role="甲",
                            dependencies=["b"])
    await board.create_task(env.run_id, task_id="b", subject="B", description="x", role="乙",
                            dependencies=["a"])
    with pytest.raises(PlanValidationError, match="循环"):
        await board.validate_plan(env.run_id)


@pytest.mark.asyncio
async def test_concurrent_claim_lease_reclaim_and_idempotent_complete(env):
    board = TaskBoardService()
    await board.create_task(env.run_id, task_id="only", subject="唯一任务", description="x", role="执行")
    await _member(env, "mbr-1", "执行")
    await _member(env, "mbr-2", "执行")

    results = await asyncio.gather(
        board.claim(env.run_id, "only", member_id="mbr-1"),
        board.claim(env.run_id, "only", member_id="mbr-2"),
        return_exceptions=True,
    )
    successes = [item for item in results if isinstance(item, dict)]
    failures = [item for item in results if isinstance(item, TaskConflict)]
    assert len(successes) == 1 and len(failures) == 1
    winner = successes[0]
    winner_member = winner["owner_member_id"]
    lease_token = winner["lease_token"]
    assert lease_token

    # 租约过期回收
    async with AsyncSessionLocal() as db:
        await db.execute(
            update(TeamTask)
            .where(TeamTask.team_run_id == env.run_id, TeamTask.task_id == "only")
            .values(lease_expires_at=datetime.utcnow() - timedelta(seconds=5))
        )
        await db.commit()
    assert await board.reclaim_expired(env.run_id) == 1
    reclaimed = await board.get_task(env.run_id, "only")
    assert reclaimed["board_status"] == "ready" and reclaimed["owner_member_id"] is None

    # 旧租约完成被拒
    with pytest.raises(TaskConflict):
        await board.complete(
            env.run_id, "only", member_id=winner_member,
            lease_token=lease_token, artifact_id="art-old",
        )

    other = "mbr-2" if winner_member == "mbr-1" else "mbr-1"
    claimed = await board.claim(env.run_id, "only", member_id=other)
    await board.mark_running(
        env.run_id, "only", member_id=other, lease_token=claimed["lease_token"],
    )
    outcome = await board.complete(
        env.run_id, "only", member_id=other,
        lease_token=claimed["lease_token"], artifact_id="art-1",
    )
    assert outcome == "completed"
    assert await board.complete(
        env.run_id, "only", member_id=other,
        lease_token=claimed["lease_token"], artifact_id="art-1",
    ) == "already"
    with pytest.raises(TaskConflict):
        await board.complete(
            env.run_id, "only", member_id=other,
            lease_token=claimed["lease_token"], artifact_id="art-other",
        )


@pytest.mark.asyncio
async def test_task_retry_limit_and_snapshot_members(env):
    board = TaskBoardService()
    await board.create_task(
        env.run_id, task_id="job", subject="重试任务", description="x", role="执行",
    )
    await _member(env, "mbr-r", "执行")
    claimed = await board.claim(env.run_id, "job", member_id="mbr-r")
    await board.fail(
        env.run_id, "job", member_id="mbr-r",
        lease_token=claimed["lease_token"], error="第一次失败",
    )
    assert await board.retry(env.run_id, "job") is True
    claimed = await board.claim(env.run_id, "job", member_id="mbr-r")
    await board.fail(
        env.run_id, "job", member_id="mbr-r",
        lease_token=claimed["lease_token"], error="第二次失败",
    )
    # 默认 max_retries=2：还可以重试一次
    assert await board.retry(env.run_id, "job") is True
    claimed = await board.claim(env.run_id, "job", member_id="mbr-r")
    await board.fail(
        env.run_id, "job", member_id="mbr-r",
        lease_token=claimed["lease_token"], error="第三次失败",
    )
    with pytest.raises(TaskStateError, match="上限"):
        await board.retry(env.run_id, "job")

    snapshot = await board.snapshot(env.run_id, phase="failed", summary="失败")
    assert snapshot["phase"] == "failed"
    assert snapshot["tasks"][0]["status"] == "failed"
    assert snapshot["members"][0]["member_id"] == "mbr-r"
    assert snapshot["members"][0]["role"] == "执行"


@pytest.mark.asyncio
async def test_cross_team_task_and_member_isolation(env):
    board = TaskBoardService()
    await board.create_task(env.run_id, task_id="shared", subject="任务", description="x", role="甲")
    other = await _create_env("另一个团队")
    try:
        await board.create_task(
            other.run_id, task_id="shared", subject="另一个任务", description="x", role="乙",
        )
        mine = await board.list_tasks(env.run_id)
        theirs = await board.list_tasks(other.run_id)
        assert mine[0]["title"] == "任务"
        assert theirs[0]["title"] == "另一个任务"
        with pytest.raises(TeamNotFound):
            await board.get_task(other.run_id, "missing")
    finally:
        await _cleanup_env(other)


# ── Message Bus / Artifact Store ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_message_bus_idempotency_inbox_ack_and_isolation(env):
    await _member(env, "mbr-a", "分析")
    await _member(env, "mbr-b", "设计")
    bus = MessageBusService()
    first = await bus.send(
        env.run_id, sender_member_id="mbr-a", recipient_member_id="mbr-b",
        payload={"text": "请补充接口"}, idempotency_key="call-1",
    )
    again = await bus.send(
        env.run_id, sender_member_id="mbr-a", recipient_member_id="mbr-b",
        payload={"text": "请补充接口"}, idempotency_key="call-1",
    )
    assert first["message_id"] == again["message_id"]
    assert again["created"] is False

    inbox = await bus.list_inbox(env.run_id, "mbr-b")
    assert [item["message_id"] for item in inbox] == [first["message_id"]]
    assert inbox[0]["status"] == "delivered"
    assert await bus.ack(env.run_id, "mbr-b", first["message_id"]) == "acked"
    assert await bus.ack(env.run_id, "mbr-b", first["message_id"]) == "already"
    assert await bus.wait_for_message(env.run_id, "mbr-b", timeout=0.1) is None

    other = await _create_env("消息隔离")
    try:
        await _member(other, "mbr-x", "分析")
        with pytest.raises(TeamNotFound):
            await bus.send(
                env.run_id, sender_member_id="mbr-a", recipient_member_id="mbr-x",
                payload={"text": "跨团队"},
            )
    finally:
        await _cleanup_env(other)


@pytest.mark.asyncio
async def test_artifact_store_snippet_checksum_and_isolation(env):
    await _member(env, "mbr-a", "分析")
    store = ArtifactStore()
    artifact = await store.create(
        env.run_id, member_id="mbr-a", task_id=None,
        name="调研摘要", content="A" * 120 + "结论",
    )
    assert artifact["checksum"] and artifact["size_bytes"] > 0
    snippet = await store.get(env.run_id, artifact["artifact_id"], snippet_chars=10, offset=120)
    assert snippet["content"] == "结论"
    assert snippet["snippet"]["total_chars"] == 122
    listed = await store.list(env.run_id)
    assert listed[0]["artifact_id"] == artifact["artifact_id"]
    previews = await store.previews(env.run_id, [artifact["artifact_id"]])
    assert previews[artifact["artifact_id"]]["name"] == "调研摘要"

    other = await _create_env("产物隔离")
    try:
        with pytest.raises(TeamNotFound):
            await store.get(other.run_id, artifact["artifact_id"])
    finally:
        await _cleanup_env(other)


# ── Member Runtime ─────────────────────────────────────────────────────────


class FakeAgent:
    def __init__(self, *, output: str, member_ctx=None, threads=None, payloads=None,
                 state_next=None, fail_times=None):
        self.output = output
        self.member_ctx = member_ctx
        self.threads = threads if threads is not None else []
        self.payloads = payloads if payloads is not None else []
        self.state_next = state_next if state_next is not None else []
        self.fail_times = fail_times if fail_times is not None else []

    async def aget_state(self, config):
        pending = self.state_next.pop(0) if self.state_next else ()
        return SimpleNamespace(next=pending)

    async def ainvoke(self, payload, config=None):
        self.threads.append(config["configurable"]["thread_id"])
        self.payloads.append(payload)
        if self.fail_times and self.fail_times[0] > 0:
            self.fail_times[0] -= 1
            raise PersistenceError("checkpoint 写入中断")
        if self.member_ctx is not None:
            self.member_ctx.model_calls += 1
            self.member_ctx.tool_calls += 1
            self.member_ctx.tokens_generated += 10
        return {"messages": [AIMessage(content=self.output)]}


def _manager(env: TeamEnv, ctx: RuntimeContext, builder) -> MemberRuntimeManager:
    return MemberRuntimeManager(
        team_run_id=env.run_id,
        run_id=env.run_id,
        parent_ctx=ctx,
        provider={},
        model_config={"name": "fake"},
        policy=ToolPolicy(allowed_tools=set(), enable_hitl=False),
        tool_specs=[],
        bus=EventBus(),
        board=TaskBoardService(),
        messages=MessageBusService(),
        artifacts=ArtifactStore(),
        directory=MemberDirectory(),
        checkpointer=InMemorySaver(),
        agent_builder=builder,
    )


def _parent_ctx(env: TeamEnv) -> RuntimeContext:
    return RuntimeContext(
        scope=RunScope(user_id=env.user_id),
        run_id=env.run_id,
        thread_id=f"team:{env.run_id}",
        agent_id=env.agent_id,
        project_id=None,
        agent_config={"system_prompt": "团队测试"},
        budget=ExecutionBudget(max_steps=100, max_tool_calls=100, max_model_calls=100),
        worker_id="test-worker",
        lease_version=1,
    )


@pytest.mark.asyncio
async def test_member_runtime_parallel_threads_artifacts_and_usage(env):
    board = TaskBoardService()
    await board.create_task(env.run_id, task_id="facts", subject="核对事实", description="x", role="事实核对")
    await board.create_task(env.run_id, task_id="design", subject="设计方案", description="x", role="方案设计")
    ctx = _parent_ctx(env)
    threads: list[str] = []
    payloads: list = []

    def builder(*, member, execution, profile, member_ctx, is_leader):
        return FakeAgent(
            output=f"成员成果：{execution.role}",
            member_ctx=member_ctx, threads=threads, payloads=payloads,
        )

    manager = _manager(env, ctx, builder)
    results = await asyncio.gather(
        manager.execute(
            {"id": "facts", "role": "事实核对", "title": "核对事实", "brief": "交付事实", "depends_on": []},
            objective="核对交付方案",
        ),
        manager.execute(
            {"id": "design", "role": "方案设计", "title": "设计方案", "brief": "交付方案", "depends_on": []},
            objective="核对交付方案",
        ),
    )
    assert [result.status for result in results] == ["completed", "completed"]
    assert len(set(threads)) == 2
    assert all(result.artifact_id for result in results)
    tasks = await board.list_tasks(env.run_id)
    assert {task["board_status"] for task in tasks} == {"completed"}
    assert all(task["result_artifact_id"] for task in tasks)
    members = await manager.list_members()
    assert {member["status"] for member in members} == {"completed"}
    assert ctx.model_calls == 2 and ctx.tool_calls == 2 and ctx.tokens_generated == 20

    # 同一任务重复执行：直接复用已完成产物，不再次调用 Agent
    again = await manager.execute(
        {"id": "facts", "role": "事实核对", "title": "核对事实", "brief": "交付事实", "depends_on": []},
        objective="核对交付方案",
    )
    assert again.ok and again.recovered and len(threads) == 2


@pytest.mark.asyncio
async def test_member_runtime_budget_failure_marks_task_failed(env):
    board = TaskBoardService()
    await board.create_task(env.run_id, task_id="job", subject="任务", description="x", role="执行")
    ctx = _parent_ctx(env)

    class BudgetAgent(FakeAgent):
        async def ainvoke(self, payload, config=None):
            from app.services.agent_runtime.tools.policy import BudgetExceededError

            raise BudgetExceededError("成员预算耗尽")

    def builder(*, member, execution, profile, member_ctx, is_leader):
        return BudgetAgent(output="", member_ctx=member_ctx)

    manager = _manager(env, ctx, builder)
    result = await manager.execute(
        {"id": "job", "role": "执行", "title": "任务", "brief": "x", "depends_on": []},
        objective="测试预算",
    )
    assert result.status == "failed" and "预算" in result.error
    task = await board.get_task(env.run_id, "job")
    assert task["board_status"] == "failed"
    member = (await manager.list_members())[0]
    assert member["status"] == "failed" and member["last_error"]


@pytest.mark.asyncio
async def test_member_runtime_resume_same_thread_after_checkpoint_interruption(env):
    board = TaskBoardService()
    await board.create_task(env.run_id, task_id="job", subject="任务", description="x", role="执行")
    ctx = _parent_ctx(env)
    threads: list[str] = []
    payloads: list = []
    state_next: list = [(), ("pending",)]
    fail_times = [1]

    def builder(*, member, execution, profile, member_ctx, is_leader):
        return FakeAgent(
            output="恢复后的成果", member_ctx=member_ctx,
            threads=threads, payloads=payloads,
            state_next=list(state_next), fail_times=list(fail_times),
        )

    # 两次 execute 需要共享状态序列：用同一 builder 闭包 + 可变列表
    def shared_builder(*, member, execution, profile, member_ctx, is_leader):
        return FakeAgent(
            output="恢复后的成果", member_ctx=member_ctx,
            threads=threads, payloads=payloads,
            state_next=state_next, fail_times=fail_times,
        )

    manager = _manager(env, ctx, shared_builder)
    task = {"id": "job", "role": "执行", "title": "任务", "brief": "x", "depends_on": []}
    first = await manager.execute(task, objective="恢复测试")
    assert first.status == "interrupted"
    released = await board.get_task(env.run_id, "job")
    assert released["board_status"] == "ready"

    second = await manager.execute(task, objective="恢复测试")
    assert second.ok and second.recovered is True
    assert len(threads) == 2 and threads[0] == threads[1]
    assert payloads[1] is None
    member = (await manager.list_members())[0]
    assert member["recovery_count"] >= 1


@pytest.mark.asyncio
async def test_member_message_wake_up_produces_artifact_and_reply(env):
    ctx = _parent_ctx(env)
    manager = _manager(
        env, ctx,
        lambda *, member, execution, profile, member_ctx, is_leader: FakeAgent(
            output="已补充接口说明", member_ctx=member_ctx,
        ),
    )
    leader = await manager.ensure_member(role="团队负责人", is_leader=True)
    idle = await manager.ensure_member(
        role="补充分析", task_id="extra",
        profile=MemberProfile(role="补充分析", name="补充分析"),
    )
    messages = MessageBusService()
    request = await messages.send(
        env.run_id, sender_member_id=leader["member_id"],
        recipient_member_id=idle["member_id"],
        payload={"text": "请补充接口说明"},
    )
    results = await manager.wake_idle_members(max_count=1)
    assert len(results) == 1 and results[0].artifact_id
    outbox = await messages.list_inbox(env.run_id, leader["member_id"])
    assert any(message["artifact_id"] == results[0].artifact_id for message in outbox)
    assert (await messages.get(env.run_id, request["message_id"]))["status"] == "acked"


# ── Team Graph 接线 ────────────────────────────────────────────────────────


class FakeLeader:
    def __init__(self, board: TaskBoardService, team_run_id: str):
        self.board = board
        self.team_run_id = team_run_id
        self.stages: list[str] = []

    async def invoke(self, stage: str, instructions: str, user: str, **kwargs):
        self.stages.append(stage)
        if stage == "planning":
            await self.board.create_task(
                self.team_run_id, task_id="facts", subject="核对事实",
                description="交付事实与依据", role="事实核对",
            )
            await self.board.create_task(
                self.team_run_id, task_id="design", subject="设计方案",
                description="交付可执行方案", role="方案设计",
            )
            return '{"acceptance":["事实已核对","方案已交付"]}', False
        if stage == "review":
            return json.dumps({"checks": [
                {"criterion": 1, "status": "supported", "evidence": "事实清晰", "task_id": "facts"},
                {"criterion": 2, "status": "supported", "evidence": "方案完整", "task_id": "design"},
            ]}), False
        return "团队综合交付", False


class FakeCollab:
    def __init__(self, *, team_run_id: str, board: TaskBoardService, leader: FakeLeader,
                 members: MemberRuntimeManager, max_parallel: int = 2):
        self.team_run_id = team_run_id
        self.board = board
        self.leader = leader
        self.members = members
        self.max_parallel = max_parallel
        self.team = SimpleNamespace()

    async def snapshot(self, *, phase, running=(), summary="", lead="团队负责人"):
        return await self.board.snapshot(
            self.team_run_id, phase=phase, running=running, summary=summary, lead=lead,
        )

    async def wake_idle_members(self, *, max_count: int = 2):
        return []


@pytest.mark.asyncio
async def test_team_graph_uses_task_board_and_member_runtime(env):
    board = TaskBoardService()
    ctx = _parent_ctx(env)
    threads: list[str] = []

    def builder(*, member, execution, profile, member_ctx, is_leader):
        threads.append(member["thread_id"])
        return FakeAgent(
            output=f"交付：{execution.role}", member_ctx=member_ctx, threads=threads,
        )

    manager = _manager(env, ctx, builder)
    leader = FakeLeader(board, env.run_id)
    collab = FakeCollab(
        team_run_id=env.run_id, board=board, leader=leader, members=manager,
    )
    events: list[dict] = []

    async def emit(event_type: str, payload: dict) -> None:
        assert event_type == "team.updated"
        events.append(payload)

    async def never(*args, **kwargs):
        raise AssertionError("collaboration 路径不应调用旧 invoke/worker")

    graph = build_team_graph(
        InMemorySaver(),
        TeamGraphContext(never, emit, remaining_model_calls=lambda: 50, collaboration=collab),
    )
    result = await drive_team_graph(
        graph, {"configurable": {"thread_id": f"team:{env.run_id}"}},
        {"objective": "核对交付方案", "reference_context": "验收日期为周五"},
    )
    assert result["final_text"] == "团队综合交付"
    assert leader.stages == ["planning", "synthesis"]
    assert [event["phase"] for event in events] == [
        "planning", "working", "working", "synthesizing", "completed",
    ]
    tasks = await board.list_tasks(env.run_id)
    assert {task["board_status"] for task in tasks} == {"completed"}
    assert tasks[0]["result"] and tasks[0]["owner_member_id"]
    assert len(set(threads)) == 2
    members = await manager.list_members()
    assert {member["status"] for member in members} == {"completed"}


@pytest.mark.asyncio
async def test_team_graph_resume_reuses_completed_board_tasks(env):
    """Controller Checkpoint 丢失但 Task Board 已完成：调度阶段直接采纳事实。"""
    board = TaskBoardService()
    await board.create_task(env.run_id, task_id="facts", subject="核对事实", description="x", role="事实核对")
    await board.create_task(env.run_id, task_id="design", subject="设计方案", description="x", role="方案设计")
    ctx = _parent_ctx(env)
    manager = _manager(env, ctx, lambda **kwargs: FakeAgent(output="不会执行"))
    claimed = await board.claim(env.run_id, "facts", member_id="mbr-done")
    await board.mark_running(
        env.run_id, "facts", member_id="mbr-done", lease_token=claimed["lease_token"],
    )
    await board.complete(
        env.run_id, "facts", member_id="mbr-done",
        lease_token=claimed["lease_token"], artifact_id=None,
    )
    leader = FakeLeader(board, env.run_id)
    # 只让 design 需要执行
    leader_stages: list[str] = []

    async def planning(stage, instructions, user, **kwargs):
            leader_stages.append(stage)
            if stage == "planning":
                return '{"acceptance":["事实已核对","方案已交付"]}', False
            return "综合交付", False

    leader.invoke = planning
    collab = FakeCollab(
        team_run_id=env.run_id, board=board, leader=leader, members=manager,
    )

    async def emit(event_type, payload):
        return None

    async def never(*args, **kwargs):
        raise AssertionError("不应调用旧执行路径")

    graph = build_team_graph(
        InMemorySaver(),
        TeamGraphContext(never, emit, remaining_model_calls=lambda: 50, collaboration=collab),
    )
    result = await drive_team_graph(
        graph, {"configurable": {"thread_id": f"team-resume:{env.run_id}"}},
        {"objective": "核对交付方案"},
    )
    assert result["final_text"] == "综合交付"
    # facts 已完成被直接采纳，只有 design 触发成员执行
    members = await manager.list_members()
    executed_tasks = {member["task_id"] for member in members if member["task_id"]}
    assert executed_tasks == {"design"}


# ── MemberAgentFactory / MemberProfile / Team Tools 权限 ────────────────────


def test_member_profile_validation_and_role_override():
    profile = MemberProfile.from_task(
        {"id": "research", "role": "资料研究员", "title": "调研"},
        team_config={
            "members": {
                "资料研究员": {
                    "model_config_id": 7,
                    "tools": ["web_search", "datetime_now"],
                    "max_model_calls": 3,
                    "max_tool_calls": 2,
                    "timeout_seconds": 30,
                }
            }
        },
    )
    assert profile.role == "资料研究员"
    assert profile.model_config_id == 7
    assert profile.tools == frozenset({"web_search", "datetime_now"})
    assert profile.max_model_calls == 3 and profile.max_tool_calls == 2
    assert profile.to_snapshot()["model_config_id"] == 7
    assert profile.budget_snapshot()["timeout_seconds"] == 30.0

    with pytest.raises(ValueError):
        MemberProfile(role="", name="x").validate()
    with pytest.raises(ValueError):
        MemberProfile(role="x", name="x", max_model_calls=0).validate()
    with pytest.raises(ValueError):
        MemberProfile(role="x", name="x", allow_spawn=True).validate()
    with pytest.raises(ValueError):
        MemberProfile.from_snapshot({"role": "x", "timeout_seconds": -1})


def test_member_agent_factory_scopes_tools_and_middleware(monkeypatch):
    from app.services.agent_runtime.team.runtime import member_factory
    from app.services.tools import get_tool_by_name

    captured = {}
    sentinel_checkpointer = object()

    class FakeModel:
        pass

    def fake_create_deep_agent(model, tools, **kwargs):
        captured.update({"model": model, "tools": tools, **kwargs})
        return SimpleNamespace(name="fake-agent")

    import deepagents

    monkeypatch.setattr(deepagents, "create_deep_agent", fake_create_deep_agent)
    monkeypatch.setattr(member_factory, "build_chat_model", lambda *args, **kwargs: FakeModel())

    policy = ToolPolicy(
        allowed_tools={"datetime_now", "web_search", "http_post", "write_file", "delete"},
        enable_hitl=True,
    )
    specs = [
        spec for spec in (
            get_tool_by_name("datetime_now"), get_tool_by_name("web_search"),
            get_tool_by_name("http_post"), get_tool_by_name("write_file"),
            get_tool_by_name("delete"),
        ) if spec is not None
    ]
    factory = MemberAgentFactory(
        policy=policy, tool_specs=specs, bus=EventBus(),
        checkpointer=sentinel_checkpointer,
    )
    team_context = {
        "team_run_id": "run-1", "member_id": "mbr-1", "role": "资料研究员",
        "is_leader": False, "task_id": "research", "leader_member_id": "leader",
        "board": None, "messages": None, "artifacts": None, "members": None,
    }
    member_ctx = RuntimeContext(
        scope=RunScope(user_id=1), run_id="run-1",
        thread_id="team:run-1:member:mbr-1", agent_id=1,
        budget=ExecutionBudget(max_steps=4, max_tool_calls=4, max_model_calls=4),
        worker_id="w", lease_version=1, team_context=team_context,
    )
    execution = SimpleNamespace(
        member_id="mbr-1", role="资料研究员", thread_id=member_ctx.thread_id,
        task_id="research", objective="调研行业趋势",
        reference_context="参考资料", assistant_prompt="",
        task_brief="收集资料并给出结论", predecessors=[],
        acceptance=["给出结论"], findings=[], prior_result="",
        incoming_messages=[], profile=None,
    )
    profile = MemberProfile(
        role="资料研究员", name="资料研究员",
        tools=frozenset({"datetime_now", "web_search"}),
    )
    agent = factory.build(
        member_ctx=member_ctx, execution=execution, profile=profile,
        provider={"name": "p", "base_url": "http://x"}, model_config={"name": "m"},
        is_leader=False, remaining_model_calls=4, remaining_tool_calls=4,
    )
    assert agent.name == "fake-agent"
    tool_names = {tool.name for tool in captured["tools"]}
    assert "datetime_now" in tool_names
    assert "team_artifact_create" in tool_names and "team_inbox_list" in tool_names
    assert "team_task_create" not in tool_names          # Leader 专属
    assert "web_search" in tool_names                    # 只读网络工具（父策略允许）
    assert "http_post" not in tool_names                 # 审批类
    assert "write_file" not in tool_names and "delete" not in tool_names
    # DeepAgents 基栈必需的虚拟 read_file（StateBackend，无宿主访问）
    fs_middleware = next(
        item for item in captured["middleware"]
        if type(item).__name__ == "FilesystemMiddleware"
    )
    assert {tool.name for tool in fs_middleware.tools} == {"read_file"}
    middleware_names = {type(item).__name__ for item in captured["middleware"]}
    assert "FilesystemMiddleware" in middleware_names
    assert "ModelCallLimitMiddleware" in middleware_names
    assert "ToolCallLimitMiddleware" in middleware_names
    assert captured["subagents"][0]["name"] == "general-purpose"
    assert captured["subagents"][0]["tools"] == []
    assert captured["checkpointer"] is sentinel_checkpointer
    assert "不可信" in captured["system_prompt"]
    assert "收集资料并给出结论" in captured["system_prompt"]


@pytest.mark.asyncio
async def test_team_tools_permissions_and_role_resolution(env):
    board = TaskBoardService()
    await _member(env, "mbr-a", "分析")
    await _member(env, "mbr-b", "设计")

    async with AsyncSessionLocal() as db:
        db.add(TeamMember(
            member_id="leader", team_run_id=env.run_id, role="团队负责人",
            name="团队负责人", thread_id=f"team:{env.run_id}:member:leader",
            profile_snapshot={}, model_snapshot={}, budget={}, status="ready",
        ))
        await db.commit()

    def context(member_id: str, role: str, is_leader: bool):
        return SimpleNamespace(
            run_id=env.run_id,
            team_context={
                "team_run_id": env.run_id, "member_id": member_id, "role": role,
                "is_leader": is_leader, "task_id": None, "leader_member_id": "leader",
                "board": board,
                "messages": MessageBusService(), "artifacts": ArtifactStore(),
                "members": MemberDirectory(),
            },
        )

    member_ctx = context("mbr-a", "分析", False)
    denied = await TEAM_HANDLERS["team_task_create"](
        member_ctx, {"task_id": "x", "role": "分析", "title": "t", "brief": "b"},
    )
    assert getattr(denied, "status", "") == "failed"

    leader_ctx = context("leader", "团队负责人", True)
    created = await TEAM_HANDLERS["team_task_create"](
        leader_ctx,
        {"task_id": "research", "role": "分析", "title": "调研", "brief": "收集资料"},
    )
    assert '"ok": true' in created

    task = await board.get_task(env.run_id, "research")
    assigned = await TEAM_HANDLERS["team_assign_task"](
        leader_ctx,
        {"task_id": "research", "member": "分析", "expected_revision": task["revision"]},
    )
    assert '"ok": true' in assigned
    assert (await board.get_task(env.run_id, "research"))["owner_member_id"] == "mbr-a"

    # 成员只能把本人任务标记 blocked；不能改标题
    denied_update = await TEAM_HANDLERS["team_task_update"](
        member_ctx,
        {"task_id": "research", "expected_revision": 99, "subject": "劫持"},
    )
    assert getattr(denied_update, "status", "") == "failed"

    sent = await TEAM_HANDLERS["team_send_message"](
        member_ctx, {"recipient": "设计", "payload": {"text": "请评审方案"}},
    )
    assert '"ok": true' in sent
    inbox = await TEAM_HANDLERS["team_inbox_list"](context("mbr-b", "设计", False), {})
    assert "请评审方案" in inbox

    artifact = await TEAM_HANDLERS["team_artifact_create"](
        member_ctx, {"name": "调研稿", "content": "调研结论正文"},
    )
    assert "art-" in artifact
    artifact_id = json.loads(artifact)["artifact"]["artifact_id"]
    got = await TEAM_HANDLERS["team_artifact_get"](
        context("mbr-b", "设计", False), {"artifact_id": artifact_id},
    )
    assert "调研结论正文" in got
    wrong_team = await TEAM_HANDLERS["team_artifact_get"](
        SimpleNamespace(team_context={
            "team_run_id": "other-run", "member_id": "mbr-a", "role": "分析",
            "is_leader": False, "task_id": None, "leader_member_id": "leader",
            "board": board, "messages": MessageBusService(),
            "artifacts": ArtifactStore(), "members": MemberDirectory(),
        }),
        {"artifact_id": artifact_id},
    )
    assert getattr(wrong_team, "status", "") == "failed"


@pytest.mark.asyncio
async def test_leader_stage_threads_are_isolated_and_rotatable(env):
    """Leader 规划/综合使用独立阶段线程，避免未完成规划 Checkpoint 被误续跑。"""
    ctx = _parent_ctx(env)
    threads: list[str] = []

    def builder(*, member, execution, profile, member_ctx, is_leader):
        return FakeAgent(output="阶段输出", member_ctx=member_ctx, threads=threads)

    manager = _manager(env, ctx, builder)
    leader = LeaderRunner(
        manager=manager, team_run_id=env.run_id, objective="测试阶段隔离",
    )
    await leader.invoke("planning", "规划说明", "目标")
    await leader.invoke("synthesis", "综合说明", "成果数据")
    base = f"team:{env.run_id}:member:leader"
    assert threads == [base, f"{base}:synthesis"]
    rotated = await leader.rotate_thread("测试轮换")
    assert rotated.startswith(base + ":r")
    assert (await manager.get_member("leader"))["thread_id"] == rotated


class ScriptedChatModel(BaseChatModel):
    """按脚本返回 AIMessage 的 ChatModel：验证真实 DeepAgents 多步工具循环。"""

    responses: list = []

    @property
    def _llm_type(self) -> str:
        return "scripted-team-model"

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        message = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        return ChatResult(generations=[ChatGeneration(message=message)])


@pytest.mark.asyncio
async def test_member_deepagents_executes_multistep_tool_loop(env, monkeypatch):
    """真实 create_deep_agent + 工具循环：成员连续调用两个工具后交付。"""
    from app.services.agent_runtime.team.runtime import member_factory
    from app.services.agent_runtime.team.runtime.member_context import MemberExecutionContext
    from app.services.tools import get_tool_by_name

    await _member(env, "mbr-loop", "分析")
    board = TaskBoardService()
    messages = MessageBusService()
    artifacts = ArtifactStore()
    team_context = {
        "team_run_id": env.run_id, "member_id": "mbr-loop", "role": "分析",
        "is_leader": False, "task_id": None, "leader_member_id": "leader",
        "board": board, "messages": messages, "artifacts": artifacts,
        "members": MemberDirectory(),
    }
    member_ctx = RuntimeContext(
        scope=RunScope(user_id=env.user_id), run_id=env.run_id,
        thread_id=f"team:{env.run_id}:member:mbr-loop", agent_id=env.agent_id,
        budget=ExecutionBudget(max_steps=6, max_tool_calls=6, max_model_calls=6),
        worker_id="test-worker", lease_version=1, team_context=team_context,
    )
    model = ScriptedChatModel(responses=[
        AIMessage(content="", tool_calls=[{
            "name": "datetime_now", "args": {}, "id": "call-1", "type": "tool_call",
        }]),
        AIMessage(content="", tool_calls=[{
            "name": "team_artifact_create",
            "args": {"name": "中间稿", "content": "两步工具调用成果"},
            "id": "call-2", "type": "tool_call",
        }]),
        AIMessage(content="最终交付：两步工具调用完成"),
    ])
    monkeypatch.setattr(member_factory, "build_chat_model", lambda *args, **kwargs: model)
    factory = MemberAgentFactory(
        policy=ToolPolicy(allowed_tools={"datetime_now"}, enable_hitl=True),
        tool_specs=[get_tool_by_name("datetime_now")],
        bus=EventBus(), checkpointer=InMemorySaver(),
    )
    profile = MemberProfile(
        role="分析", name="分析", max_model_calls=8, max_tool_calls=8,
        tools=frozenset({"datetime_now"}),
    )
    execution = MemberExecutionContext(
        team_run_id=env.run_id, member_id="mbr-loop", role="分析",
        thread_id=member_ctx.thread_id, task_id=None,
        objective="验证多步工具循环", task_brief="先查时间，再保存中间成果，最后交付",
        profile=profile,
    )
    agent = factory.build(
        member_ctx=member_ctx, execution=execution, profile=profile,
        provider={"name": "p", "base_url": "http://x"},
        model_config={"name": "m", "max_tokens": 512},
        is_leader=False, remaining_model_calls=8, remaining_tool_calls=8,
    )
    config = make_run_config(member_ctx.thread_id, env.run_id, env.user_id)
    config["recursion_limit"] = 40
    config["callbacks"] = [ModelUsageCallback(member_ctx)]
    result = await agent.ainvoke(
        {"messages": [HumanMessage(content="执行任务")]}, config,
    )
    messages_out = result.get("messages") or []
    tool_messages = [item for item in messages_out if getattr(item, "type", "") == "tool"]
    assert len(tool_messages) >= 2
    assert "最终交付" in str(messages_out[-1].content)
    stored = await artifacts.list(env.run_id)
    assert any(item["name"] == "中间稿" for item in stored)
    assert member_ctx.model_calls >= 3
    assert member_ctx.tool_calls == 2

