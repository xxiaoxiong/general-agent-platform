"""目标模式（/goal）与执行中补充要求（steering）验收测试。

覆盖：
- 目标模式解除模型/工具/时长预算（低预算设置下仍可完成多轮执行）；
- 执行中补充要求受理（非目标模式 / 终态拒绝）、事件与会话历史落库；
- 补充要求在下一次模型调用前注入上下文（按稳定消息 ID 去重）。
"""
import json
import os
import threading
import time
import uuid
from typing import Any

import pytest
from fastapi.testclient import TestClient
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from sqlalchemy import create_engine, delete, select
from sqlalchemy.orm import Session

from app.models.agent import Agent, AgentRun, AgentRunEvent, AgentRunSteering, AgentSession
from app.models.model_provider import ModelConfig, ModelProvider
from app.models.user import User

sync_engine = create_engine(os.environ["DATABASE_SYNC_URL"])
STEER_TOKEN = "STEER-TOKEN-9F3A"


class GatedChatModel(BaseChatModel):
    """脚本化模型：记录每次调用看到的上下文，可在首次调用处等待补充要求提交。"""

    script: list = []
    seen: list = []
    gate: Any = None
    gate_timeout: float = 15.0

    @property
    def _llm_type(self):
        return "fake-goal"

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.seen.append([str(getattr(message, "content", "")) for message in messages])
        if self.gate is not None and not self.gate.is_set():
            self.gate.wait(self.gate_timeout)
        item = self.script.pop(0) if self.script else AIMessage(content="（结束）")
        return ChatResult(generations=[ChatGeneration(message=item)])


def _patch_model(monkeypatch, script, *, gate=None):
    import app.services.agent_runtime.factory as factory_mod
    import app.services.agent_runtime.service as svc_mod

    async def fake_load_model(self, model_config_id):
        return (
            {"name": "goal-provider", "base_url": "http://fake.local/v1", "api_key": "none",
             "extra_config": {}},
            {"name": "goal-model", "max_tokens": None, "supports_streaming": True,
             "supports_function_call": True, "extra_params": {}},
        )

    model = GatedChatModel(script=list(script), gate=gate)
    monkeypatch.setattr(svc_mod.AgentRuntimeService, "_load_model", fake_load_model)
    monkeypatch.setattr(factory_mod, "build_chat_model", lambda *a, **k: model)
    return model


def _auth(user_id: int) -> dict:
    from app.core.security import create_access_token

    return {"Authorization": f"Bearer {create_access_token(user_id)}"}


@pytest.fixture(scope="module")
def client():
    from app.main import app

    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def user_setup():
    from app.core.database import Base

    Base.metadata.create_all(sync_engine)
    suffix = uuid.uuid4().hex[:8]
    with Session(sync_engine) as s:
        user = User(username=f"goal_u_{suffix}", email=f"goal_{suffix}@test.dev",
                    hashed_password="x", is_active=True)
        s.add(user)
        s.commit()
        provider = ModelProvider(
            name=f"goal-provider-{suffix}", display_name="Goal Fake",
            provider_type="openai_compatible", base_url="http://fake.local/v1",
            api_key="none", is_active=True,
        )
        s.add(provider)
        s.commit()
        model = ModelConfig(
            provider_id=provider.id, name=f"goal-model-{suffix}", display_name="Goal Fake",
            model_type="chat", is_active=True, supports_streaming=True,
            supports_function_call=True,
        )
        s.add(model)
        s.commit()
        agent = Agent(
            name=f"goal-agent-{suffix}", system_prompt="测试目标模式",
            temperature="0.7", tools=[], is_active=True, owner_id=user.id,
            model_config_id=model.id,
        )
        s.add(agent)
        s.commit()
        ids = {"user_id": user.id, "agent_id": agent.id}
    yield ids
    with Session(sync_engine) as s:
        from app.models.goal import GoalContract, GoalIteration

        runs = [row[0] for row in s.execute(select(AgentRun.id).where(AgentRun.user_id == ids["user_id"]))]
        if runs:
            s.execute(delete(GoalIteration).where(GoalIteration.run_id.in_(runs)))
            s.execute(delete(GoalContract).where(GoalContract.run_id.in_(runs)))
            s.execute(delete(AgentRunSteering).where(AgentRunSteering.run_id.in_(runs)))
            s.execute(delete(AgentRunEvent).where(AgentRunEvent.run_id.in_(runs)))
            s.execute(delete(AgentRun).where(AgentRun.id.in_(runs)))
        s.execute(delete(AgentSession).where(AgentSession.user_id == ids["user_id"]))
        s.commit()


def _wait(client, run_id, user_id, predicate, timeout=20.0):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        info = client.get(f"/api/v2/runs/{run_id}", headers=_auth(user_id)).json()
        last = info
        if predicate(info):
            return info
        time.sleep(0.05)
    raise AssertionError(f"run {run_id} 未满足条件，最后状态：{last}")


def _events(client, run_id, user_id):
    response = client.get(f"/api/v2/runs/{run_id}/events", headers=_auth(user_id))
    assert response.status_code == 200
    envelopes = []
    for block in response.text.split("\n\n"):
        line = next((row for row in block.splitlines() if row.startswith("data: ")), None)
        if line:
            envelopes.append(json.loads(line[len("data: "):]))
    return envelopes


def test_goal_mode_disables_budget_and_injects_steering(client, user_setup, monkeypatch):
    """低预算设置下目标模式仍可多轮执行；执行中补充要求在下一轮注入并落库。"""
    from app.core.config import settings

    monkeypatch.setattr(settings, "AGENT_MAX_MODEL_CALLS", 1)
    monkeypatch.setattr(settings, "AGENT_MAX_TOOL_CALLS", 0)
    monkeypatch.setattr(settings, "AGENT_MAX_STEPS", 0)
    monkeypatch.setattr(settings, "AGENT_RUN_TIMEOUT", 0.0)

    gate = threading.Event()
    plan = AIMessage(content="", tool_calls=[{
        "name": "write_todos", "id": "call-plan",
        "args": {"todos": [{"content": "收集资料", "status": "completed"}]},
    }])
    model = _patch_model(
            monkeypatch,
            [plan, AIMessage(content=(
                "已按补充要求完成。\n\n## 目标核对\n"
                "| 目标项 | 完成情况 | 证据 |\n| --- | --- | --- |\n"
                "| 收集资料 | 已完成 | write_todos 记录 |\n"
                f"| 执行中补充要求：最终结果必须包含 {STEER_TOKEN}，并按两部分组织 | 已完成 | 正文包含该标识 |\n\n"
                f"第一部分完成。\n\n第二部分包含 {STEER_TOKEN}。"
            ))],
            gate=gate,
        )
    user_id = user_setup["user_id"]
    response = client.post("/api/v2/runs", headers=_auth(user_id), json={
        "message": "执行一个长任务", "assistant_id": user_setup["agent_id"],
        "use_memory": False, "goal_mode": True,
    })
    assert response.status_code == 200, response.text
    created = response.json()
    run_id = created["run_id"]
    assert created["goal_mode"] is True

    # 首次模型调用阻塞：确认运行中后提交补充要求
    _wait(client, run_id, user_id, lambda info: info["status"] == "running")
    time.sleep(0.5)  # 等待首次模型调用真正进入（模型在 gate 处阻塞，不影响 API）
    steer = client.post(f"/api/v2/runs/{run_id}/steer", headers=_auth(user_id), json={
        "message": f"补充要求：最终结果必须包含 {STEER_TOKEN}，并按两部分组织",
    })
    assert steer.status_code == 200, steer.text
    assert steer.json()["status"] == "queued"
    gate.set()

    info = _wait(client, run_id, user_id, lambda row: row["status"] in {
        "completed", "failed", "limit_exceeded", "cancelled",
    })
    assert info["status"] == "completed", info
    assert info["goal_mode"] is True
    # 目标模式不挂 ModelCallLimit：两次模型调用都真实发生
    assert len(model.seen) >= 2, model.seen
    assert any(STEER_TOKEN in message for message in model.seen[-1]), model.seen[-1]

    events = _events(client, run_id, user_id)
    names = [event["event"] for event in events]
    assert "run.steered" in names
    assert "run.steering_applied" in names

    with Session(sync_engine) as s:
        steering = s.execute(
            select(AgentRunSteering).where(AgentRunSteering.run_id == run_id)
        ).scalars().all()
        assert len(steering) == 1
        assert steering[0].delivered_at is not None
        assert steering[0].delivered_via == "agent"
        run = s.get(AgentRun, run_id)
        # 目标模式不套用普通任务的短预算，但保留全局安全截止（GOAL_MAX_SECONDS）
        assert run.budget_deadline_at is not None
        # Goal Runtime：目标契约必须持久化，并由目标验收而非模型自述决定完成
        from app.models.goal import GoalContract, GoalIteration

        contract = s.execute(
            select(GoalContract).where(GoalContract.run_id == run_id)
        ).scalar_one_or_none()
        assert contract is not None and contract.status == "completed"
        iterations = s.execute(
            select(GoalIteration).where(GoalIteration.goal_id == contract.goal_id)
        ).scalars().all()
        assert iterations and iterations[-1].status == "completed"
        session = s.get(AgentSession, run.session_id)
        assert any(STEER_TOKEN in str(message.get("content")) for message in session.messages)


def test_goal_pause_stops_at_next_model_boundary(client, user_setup, monkeypatch):
    """用户暂停目标：下一个模型调用边界收敛，不产生第二笔模型/工具副作用。"""
    from app.core.config import settings

    monkeypatch.setattr(settings, "AGENT_RUN_TIMEOUT", 0.0)
    gate = threading.Event()
    plan = AIMessage(content="", tool_calls=[{
        "name": "write_todos", "id": "call-pause-plan",
        "args": {"todos": [{"content": "收集资料", "status": "pending"}]},
    }])
    model = _patch_model(
        monkeypatch,
        [plan, AIMessage(content="暂停后不应继续执行")],
        gate=gate,
    )
    user_id = user_setup["user_id"]
    response = client.post("/api/v2/runs", headers=_auth(user_id), json={
        "message": "执行一个可暂停的目标", "assistant_id": user_setup["agent_id"],
        "use_memory": False, "goal_mode": True,
    })
    assert response.status_code == 200, response.text
    run_id = response.json()["run_id"]
    _wait(client, run_id, user_id, lambda info: info["status"] == "running")
    time.sleep(0.4)
    paused = client.post(f"/api/v2/runs/{run_id}/pause", headers=_auth(user_id))
    assert paused.status_code == 200, paused.text
    assert paused.json()["status"] == "pause_requested"
    gate.set()

    info = _wait(client, run_id, user_id, lambda row: row["status"] in {
        "completed", "failed", "limit_exceeded", "cancelled",
    })
    assert info["status"] == "limit_exceeded", info
    assert len(model.seen) == 1, model.seen
    with Session(sync_engine) as s:
        run = s.get(AgentRun, run_id)
        assert run.pause_requested is True
        assert run.summary["stop"]["reason"] == "goal_paused"
        assert run.summary["stop"]["can_continue"] is True
        from app.models.goal import GoalContract

        contract = s.execute(
            select(GoalContract).where(GoalContract.run_id == run_id)
        ).scalar_one_or_none()
        assert contract is not None and contract.status == "paused"


def test_steer_works_for_normal_run_and_rejects_terminal(client, user_setup, monkeypatch):
    """执行中补充是通用能力：普通（非目标模式）Run 同样受理并注入。"""
    from app.core.config import settings

    monkeypatch.setattr(settings, "AGENT_RUN_TIMEOUT", 0.0)
    token = "NORMAL-STEER-TOKEN"
    gate = threading.Event()
    plan = AIMessage(content="", tool_calls=[{
        "name": "write_todos", "id": "call-normal-plan",
        "args": {"todos": [{"content": "执行任务", "status": "completed"}]},
    }])
    model = _patch_model(
        monkeypatch, [plan, AIMessage(content="普通任务完成")], gate=gate,
    )
    user_id = user_setup["user_id"]
    response = client.post("/api/v2/runs", headers=_auth(user_id), json={
        "message": "普通任务", "assistant_id": user_setup["agent_id"], "use_memory": False,
    })
    run_id = response.json()["run_id"]
    assert response.json()["goal_mode"] is False
    _wait(client, run_id, user_id, lambda info: info["status"] == "running")
    time.sleep(0.5)

    accepted = client.post(f"/api/v2/runs/{run_id}/steer", headers=_auth(user_id), json={
        "message": f"执行中补充：结果必须包含 {token}",
    })
    assert accepted.status_code == 200, accepted.text
    gate.set()
    assert _wait(client, run_id, user_id, lambda info: info["status"] == "completed")["status"] == "completed"
    assert any(token in message for message in model.seen[-1]), model.seen[-1]

    events = _events(client, run_id, user_id)
    names = [event["event"] for event in events]
    assert "run.steered" in names and "run.steering_applied" in names
    with Session(sync_engine) as s:
        steering = s.execute(
            select(AgentRunSteering).where(AgentRunSteering.run_id == run_id)
        ).scalars().all()
        assert len(steering) == 1 and steering[0].delivered_via == "agent"
        session = s.get(AgentSession, s.get(AgentRun, run_id).session_id)
        assert any(token in str(message.get("content")) for message in session.messages)

    finished = client.post(f"/api/v2/runs/{run_id}/steer", headers=_auth(user_id), json={
        "message": "执行中补充",
    })
    assert finished.status_code == 409
    missing = client.post(f"/api/v2/runs/{uuid.uuid4().hex}/steer",
                          headers=_auth(user_id), json={"message": "x"})
    assert missing.status_code == 404


@pytest.mark.asyncio(loop_scope="session")
async def test_steer_rejects_team_run(user_setup):
    """团队协作运行不接入补充注入：明确拒绝，避免受理后不生效。"""
    from datetime import datetime

    from app.core.database import AsyncSessionLocal
    from app.models.agent import AgentRun
    from app.services.agent_runtime.steering import SteeringError, submit_steering

    run_id = uuid.uuid4().hex
    async with AsyncSessionLocal() as session:
        session.add(AgentRun(
            id=run_id, agent_id=user_setup["agent_id"], user_id=user_setup["user_id"],
            thread_id=f"team-{run_id}", status="running",
            input_payload={"execution_context": {"team_mode": True}},
            agent_config_snapshot={}, created_at=datetime.utcnow(),
        ))
        await session.commit()
    try:
        with pytest.raises(SteeringError) as exc:
            await submit_steering(run_id, user_setup["user_id"], "团队任务补充")
        assert exc.value.status_code == 409
        assert "团队" in str(exc.value)
    finally:
        async with AsyncSessionLocal() as session:
            row = await session.get(AgentRun, run_id)
            if row is not None:
                await session.delete(row)
                await session.commit()


@pytest.mark.asyncio(loop_scope="session")
async def test_document_graph_prompt_accumulates_steering(monkeypatch):
    """文档图：补充要求按稳定 ID 去重后累积注入（跨章节持续生效）。"""
    from types import SimpleNamespace

    from langchain_core.messages import HumanMessage, SystemMessage

    from app.services.agent_runtime import steering as steering_mod
    from app.services.agent_runtime.document_graph import DocumentGraphContext
    from app.services.documents.workflow_support import attach_steering

    rows = [{"id": 1, "message_id": "m-1", "content": "补充 A", "delivered_at": None}]
    delivered = []

    async def fake_load(run_id):
        return list(rows)

    async def fake_mark(ids, via):
        delivered.extend(ids)

    async def fake_publish(bus, run_id, thread_id, items):
        return None

    monkeypatch.setattr(steering_mod, "load_steering", fake_load)
    monkeypatch.setattr(steering_mod, "mark_steering_delivered", fake_mark)
    monkeypatch.setattr(steering_mod, "publish_steering_applied", fake_publish)

    context = DocumentGraphContext.__new__(DocumentGraphContext)
    context.runtime = SimpleNamespace(goal_mode=True)
    context.run = SimpleNamespace(run_id="r-doc", thread_id="t-doc", bus=None)
    context._steering_ids = set()
    context._steering_messages = []

    messages = await attach_steering(context, [SystemMessage(content="s"), HumanMessage(content="u")])
    assert len(messages) == 3 and "补充 A" in str(messages[-1].content)
    assert delivered == [1]
    again = await attach_steering(context, messages)
    assert len(again) == 4 and delivered == [1]

    rows.append({"id": 2, "message_id": "m-2", "content": "补充 B", "delivered_at": None})
    combined = await attach_steering(context, [])
    assert "补充 A" in str(combined[-1].content) and "补充 B" in str(combined[-1].content)


def test_steering_event_v2_mapping():
    from app.services.agent_runtime.event_protocol import to_v2_event

    submitted = to_v2_event(run_id="r1", sequence=3, event_type="run_steered",
                            payload={"message_id": "m1", "content": "补充"})
    assert submitted["event"] == "run.steered"
    assert submitted["data"]["content"] == "补充"
    applied = to_v2_event(run_id="r1", sequence=4, event_type="run_steering_applied",
                          payload={"messages": [{"message_id": "m1", "content": "补充"}]})
    assert applied["event"] == "run.steering_applied"


def test_exported_document_uses_requested_title():
    """导出既有稿件：用户点名的《标题》优先于素材首个章节标题。"""
    from app.services.documents.graphs import requested_document_title

    assert requested_document_title("写一份《企业级 AI Agent 可观测与评测建设指南》Word 文档") \
        == "企业级 AI Agent 可观测与评测建设指南"
    assert requested_document_title("把上面的调研写成《选型报告》") == "选型报告"
    assert requested_document_title("写一份报告") == ""


def test_goal_protocol_carries_objective_and_completion_audit():
    from app.services.agent_runtime.context import RunScope, RuntimeContext
    from app.services.agent_runtime.goal import goal_protocol_prompt

    ctx = RuntimeContext(RunScope(1), "run-goal", "thread", 7, goal_mode=True)
    ctx.agent_config["_goal_objective"] = "调研并交付一份可核验报告"
    prompt = goal_protocol_prompt(ctx)
    assert "目标模式执行协议" in prompt
    assert "调研并交付一份可核验报告" in prompt
    assert "write_todos" in prompt
    assert "目标核对" in prompt
    assert "不得虚报" in prompt


@pytest.mark.asyncio(loop_scope="session")
async def test_goal_mode_requires_plan_and_completion_audit(monkeypatch):
    from unittest.mock import AsyncMock

    from langchain_core.messages import AIMessage

    from app.services.agent_runtime import completion as completion_mod
    from app.services.agent_runtime.context import RunScope, RuntimeContext
    from app.services.assistant.task_contract import build_task_contract

    contract = build_task_contract("写一份项目总结", capability_ids=[])
    ctx = RuntimeContext(RunScope(1), "run-goal-audit", "thread", 7, session_id=5,
                          task_contract=contract, goal_mode=True)
    monkeypatch.setattr(completion_mod, "_collect_evidence", AsyncMock(return_value=([], [], False)))
    middleware = completion_mod.CompletionMiddleware(ctx, contract)
    result = await middleware.aafter_model({
        "messages": [AIMessage(content="最终结论正文。", id="answer-goal")],
        "todos": [],
    }, None)
    assert result["jump_to"] == "model"
    feedback = result["messages"][0].content
    assert "write_todos" in feedback and "目标核对" in feedback
    failed = {item["id"] for item in result["completion_review"]["report"]["checks"] if not item["passed"]}
    assert {"goal_plan_present", "goal_audit_present"} <= failed
