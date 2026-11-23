"""API 级集成冒烟（FastAPI TestClient + SQLite 业务库 + FakeChatModel）。

覆盖任务书 §8 验收场景的 HTTP 级证据（v2 契约：/api/v2/runs + /api/v2/conversations）：
- AT-01：创建 Run、事件流订阅、会话消息查询、删除会话
- AT-03：v2 事件流信封（tool.started / run.completed + X-Run-Id 头）
- P0-G：事件 sequence 序号 + GET /runs/{run_id} 断连查询
- AT-08：任务生命周期（超限/超时/空结果/取消的明确状态）
- AT-15：越权访问他人助手/Run 被拒（所有权校验）
- AT-16：工作流 Agent 节点（agent_invoke）端到端
"""
import json
import time
from typing import Any, List

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.core.security import create_access_token

TERMINAL_STATUSES = {"completed", "failed", "cancelled", "limit_exceeded"}


class FakeChatModel(BaseChatModel):
    """脚本化模型（API 冒烟用，无网络）。"""

    script: List[Any] = []

    @property
    def _llm_type(self) -> str:
        return "fake-api"

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        item = self.script.pop(0) if self.script else AIMessage(content="（脚本结束）")
        msg = item() if callable(item) else item
        return ChatResult(generations=[ChatGeneration(message=msg)])


def _tool_call_turn(name: str, args: dict, call_id: str = "c1") -> AIMessage:
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": call_id}])


@pytest.fixture(scope="module")
def client():
    from fastapi.testclient import TestClient
    from app.main import app

    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def db_setup():
    """创建两个用户 + 模型配置 + Agent（A 属用户 1）。"""
    import os

    from app.models.agent import Agent
    from app.models.model_provider import ModelConfig, ModelProvider
    from app.models.user import User

    engine = create_engine(os.environ["DATABASE_SYNC_URL"])
    with Session(engine) as s:
        u1 = User(username="api_u1", email="u1@test.dev", hashed_password="x", is_active=True)
        u2 = User(username="api_u2", email="u2@test.dev", hashed_password="x", is_active=True)
        s.add_all([u1, u2])
        s.flush()
        provider = ModelProvider(
            name="fake-provider", display_name="Fake", provider_type="openai_compatible",
            base_url="http://fake.local/v1", api_key="none", is_active=True,
        )
        s.add(provider)
        s.flush()
        mc = ModelConfig(
            provider_id=provider.id, name="fake-api", display_name="Fake API",
            model_type="chat", is_active=True, supports_streaming=True,
            supports_function_call=True,
        )
        s.add(mc)
        s.flush()
        agent = Agent(
            name="冒烟 Agent", description="api smoke", model_config_id=mc.id,
            system_prompt="你是冒烟测试助手。", tools=["calculator"], is_active=True,
            owner_id=u1.id,
        )
        s.add(agent)
        s.commit()
        return {"user1_id": u1.id, "user2_id": u2.id, "agent_id": agent.id, "model_config_id": mc.id}


def _auth(user_id: int) -> dict:
    return {"Authorization": f"Bearer {create_access_token(user_id)}"}


def _patch_model(monkeypatch, script):
    import app.services.agent_runtime.factory as factory_mod

    monkeypatch.setattr(factory_mod, "build_chat_model", lambda *a, **k: FakeChatModel(script=list(script)))


def _iter_sse(resp):
    """解析 SSE data: {json} 行为 dict 列表（v2 信封）。"""
    events = []
    for line in resp.iter_lines():
        if isinstance(line, bytes):
            line = line.decode("utf-8")
        if line.startswith("data: "):
            events.append(json.loads(line[6:]))
    return events


def _create_run(client, db_setup, message, *, idempotency_key=None, conversation_id=None):
    headers = dict(_auth(db_setup["user1_id"]))
    if idempotency_key:
        headers["Idempotency-Key"] = idempotency_key
    payload = {"message": message, "use_memory": False, "assistant_id": db_setup["agent_id"]}
    if conversation_id is not None:
        payload["conversation_id"] = conversation_id
    return client.post("/api/v2/runs", json=payload, headers=headers)


def _wait_run(client, user_id, run_id, timeout=30.0):
    """轮询 Run 状态直到终态（执行由 Worker 在后台推进）。"""
    info = None
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = client.get(f"/api/v2/runs/{run_id}", headers=_auth(user_id))
        assert r.status_code == 200, r.text
        info = r.json()
        if info["status"] in TERMINAL_STATUSES:
            return info
        time.sleep(0.05)
    raise AssertionError(f"Run 未在 {timeout}s 内到达终态: {info}")


# ── AT-01 / AT-03：创建 Run + 事件流 ─────────────────────────────────────────

class TestRunFlow:
    def test_run_events_stream(self, client, db_setup, monkeypatch):
        _patch_model(monkeypatch, [
            _tool_call_turn("calculator", {"expression": "21*2"}),
            AIMessage(content="SSE_DONE_42"),
        ])
        r = _create_run(client, db_setup, "计算 21*2")
        assert r.status_code == 200, r.text
        assert r.headers.get("x-run-id"), "缺少 X-Run-Id 响应头"
        run_id = r.json()["run_id"]
        conversation_id = r.json()["conversation_id"]
        assert conversation_id

        info = _wait_run(client, db_setup["user1_id"], run_id)
        assert info["status"] == "completed", info

        # v2 事件流（§7.1 信封）：tool.started / tool.completed / run.completed
        er = client.get(f"/api/v2/runs/{run_id}/events", headers=_auth(db_setup["user1_id"]))
        assert er.status_code == 200
        assert er.headers["content-type"].startswith("text/event-stream")
        assert "id: " in er.text, "标准 SSE id 行缺失"
        events = _iter_sse(er)
        names = [e["event"] for e in events]
        assert "tool.started" in names, f"缺少工具调用事件: {names}"
        assert "tool.completed" in names, f"缺少工具结果事件: {names}"
        assert "run.completed" in names
        assert events[-1]["event"] == "run.completed", events[-1]
        tool = [e for e in events if e["event"] == "tool.started"][0]
        assert tool["data"]["tool_name"] == "calculator"
        # sequence 序号严格递增（P0-G）
        seqs = [e["sequence"] for e in events]
        assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs), f"sequence 异常: {seqs}"

        # 最终内容回写会话（AT-01）
        mr = client.get(f"/api/v2/conversations/{conversation_id}/messages",
                        headers=_auth(db_setup["user1_id"]))
        assert mr.status_code == 200
        msgs = mr.json()["items"]
        assert any(m.get("role") == "assistant" and "SSE_DONE_42" in (m.get("content") or "")
                   for m in msgs), f"最终内容缺失: {msgs}"

    def test_run_without_tools_completes(self, client, db_setup, monkeypatch):
        _patch_model(monkeypatch, [AIMessage(content="INVOKE_OK")])
        r = _create_run(client, db_setup, "你好")
        assert r.status_code == 200
        run_id = r.json()["run_id"]
        info = _wait_run(client, db_setup["user1_id"], run_id)
        assert info["status"] == "completed", info

    def test_run_idempotency_key_hits_original_run(self, client, db_setup, monkeypatch):
        """Idempotency-Key：幂等命中返回原 Run ID，不重复执行（FR-01 / §8.2）。"""
        _patch_model(monkeypatch, [AIMessage(content="IDEM_OK")])
        r1 = _create_run(client, db_setup, "幂等测试", idempotency_key="idem-1")
        assert r1.status_code == 200
        run_id = r1.json()["run_id"]

        # 同键重试：命中缓存（模型脚本已耗尽，若重复执行必然产出不同内容）
        r2 = _create_run(client, db_setup, "幂等测试", idempotency_key="idem-1")
        assert r2.status_code == 200
        assert r2.json()["idempotent_reuse"] is True
        assert r2.json()["run_id"] == run_id

        # 不同键 → 新 Run
        r3 = _create_run(client, db_setup, "幂等测试", idempotency_key="idem-2")
        assert r3.status_code == 200 and r3.json()["run_id"] != run_id

    def test_session_history_and_delete(self, client, db_setup, monkeypatch):
        _patch_model(monkeypatch, [AIMessage(content="记录一下")])
        r = _create_run(client, db_setup, "记录一下")
        assert r.status_code == 200
        run_id, conversation_id = r.json()["run_id"], r.json()["conversation_id"]
        _wait_run(client, db_setup["user1_id"], run_id)

        # 历史查询（AT-01）
        mr = client.get(f"/api/v2/conversations/{conversation_id}/messages",
                        headers=_auth(db_setup["user1_id"]))
        assert mr.status_code == 200
        msgs = mr.json()["items"]
        assert isinstance(msgs, list) and msgs

        # 删除会话后不可再查（AT-01）
        r = client.delete(f"/api/v2/conversations/{conversation_id}",
                          headers=_auth(db_setup["user1_id"]))
        assert r.status_code in (200, 204)
        r = client.get(f"/api/v2/conversations/{conversation_id}/messages",
                       headers=_auth(db_setup["user1_id"]))
        assert r.status_code == 404


# ── AT-15：越权拒绝 ─────────────────────────────────────────────────────────

class TestOwnership:
    def test_other_user_cannot_access_agent(self, client, db_setup):
        _id = db_setup["agent_id"]
        other = _auth(db_setup["user2_id"])
        assert client.get(f"/api/v2/assistants/{_id}", headers=other).status_code == 404
        r = client.post("/api/v2/runs",
                        json={"message": "x", "use_memory": False, "assistant_id": _id},
                        headers=other)
        assert r.status_code == 404

    def test_other_user_cannot_query_run(self, client, db_setup, monkeypatch):
        _patch_model(monkeypatch, [AIMessage(content="PRIVATE")])
        r = _create_run(client, db_setup, "hi")
        run_id = r.json()["run_id"]
        _wait_run(client, db_setup["user1_id"], run_id)
        assert client.get(f"/api/v2/runs/{run_id}",
                          headers=_auth(db_setup["user2_id"])).status_code in (403, 404)


# ── AT-16：工作流 Agent 节点端到端 ──────────────────────────────────────────

class TestWorkflowAgentNode:
    def test_workflow_agent_invoke_node(self, client, db_setup, monkeypatch):
        _patch_model(monkeypatch, [
            _tool_call_turn("calculator", {"expression": "6*7"}),
            AIMessage(content="WF_DONE_42"),
        ])
        definition = {
            "nodes": [
                {"id": "start", "type": "start"},
                {"id": "agent1", "type": "agent_invoke",
                 "config": {"agent_id": db_setup["agent_id"], "input": "{{text}}"}},
                {"id": "end", "type": "end"},
            ],
            "edges": [
                {"from": "start", "to": "agent1"},
                {"from": "agent1", "to": "end"},
            ],
        }
        r = client.post("/api/v1/workflows",
                        json={"name": "Agent 节点冒烟", "definition": definition},
                        headers=_auth(db_setup["user1_id"]))
        assert r.status_code in (200, 201), r.text
        wf_id = r.json()["id"]

        r = client.post(f"/api/v1/workflows/{wf_id}/execute",
                        json={"input_data": {"text": "计算 6*7"}},
                        headers=_auth(db_setup["user1_id"]))
        assert r.status_code == 200, r.text
        out = r.json()
        assert out["status"] == "success", out
        assert not out.get("error_msg")
        node_results = out.get("node_results") or {}
        dumped = json.dumps(node_results, ensure_ascii=False)
        assert "WF_DONE_42" in dumped, f"Agent 节点输出缺失: {dumped[:500]}"


# ── AT-08 / P0-A：任务生命周期（预算超限/超时/空结果/取消）─────────────────

class TestBudgetAndLifecycle:
    @pytest.mark.asyncio
    async def test_tool_budget_exceeded_status(self, client, db_setup, monkeypatch):
        """工具调用次数超限 -> 明确 limit_exceeded 状态（与 failed/成功区分）。

        会话内 Run 的暂停错误文案是统一用户提示，预算/超时的区分细节在服务层
        错误信息中验证（原 /invoke 同步契约的覆盖迁移至服务层）。
        """
        import app.core.config as config_mod
        import app.services.agent_runtime.factory as factory_mod
        import app.services.agent_runtime.service as svc_mod

        from app.services.agent_runtime import get_runtime_service

        monkeypatch.setattr(config_mod.settings, "AGENT_MAX_TOOL_CALLS", 2, raising=False)

        counter = {"n": 0}

        def endless_tool_call(**kw):
            counter["n"] += 1
            return _tool_call_turn("calculator", {"expression": "1+1"}, f"tc{counter['n']}")

        monkeypatch.setattr(factory_mod, "build_chat_model",
                            lambda *a, **k: FakeChatModel(script=[endless_tool_call] * 30))

        async def fake_load_model(self, model_config_id):
            return (
                {"name": "p", "base_url": "http://x", "api_key": "k", "extra_config": {}},
                {"name": "fake", "max_tokens": None, "supports_streaming": True,
                 "supports_function_call": True, "extra_params": {}},
            )

        monkeypatch.setattr(svc_mod.AgentRuntimeService, "_load_model", fake_load_model)
        svc = get_runtime_service()
        run_id, stream = await svc.start_run(
            agent_config={"id": 990002, "tools": ["calculator"],
                          "system_prompt": "t", "temperature": "0.1"},
            messages=[{"role": "user", "content": "一直计算"}],
            user_id=db_setup["user1_id"],
        )
        events = [e async for e in stream]
        assert events, "应有执行事件"
        info = await svc.get_run(run_id, db_setup["user1_id"])
        assert info["status"] == "limit_exceeded", info
        assert "预算" in (info.get("error") or ""), info

    @pytest.mark.asyncio
    async def test_run_timeout_status(self, client, db_setup, monkeypatch):
        """运行超时 -> limit_exceeded，错误信息含超时（与预算超限可区分）"""
        import time as _time

        import app.core.config as config_mod
        import app.services.agent_runtime.factory as factory_mod
        import app.services.agent_runtime.service as svc_mod

        from app.services.agent_runtime import get_runtime_service

        monkeypatch.setattr(config_mod.settings, "AGENT_RUN_TIMEOUT", 0.3, raising=False)

        def slow_answer(**kw):
            _time.sleep(0.8)
            return AIMessage(content="迟到的回答")

        monkeypatch.setattr(factory_mod, "build_chat_model",
                            lambda *a, **k: FakeChatModel(script=[slow_answer]))

        async def fake_load_model(self, model_config_id):
            return (
                {"name": "p", "base_url": "http://x", "api_key": "k", "extra_config": {}},
                {"name": "fake", "max_tokens": None, "supports_streaming": True,
                 "supports_function_call": True, "extra_params": {}},
            )

        monkeypatch.setattr(svc_mod.AgentRuntimeService, "_load_model", fake_load_model)
        svc = get_runtime_service()
        run_id, stream = await svc.start_run(
            agent_config={"id": 990003, "tools": [],
                          "system_prompt": "t", "temperature": "0.1"},
            messages=[{"role": "user", "content": "慢一点"}],
            user_id=db_setup["user1_id"],
        )
        events = [e async for e in stream]
        assert events, "应有执行事件"
        info = await svc.get_run(run_id, db_setup["user1_id"])
        assert info["status"] == "limit_exceeded", info
        assert "超时" in (info.get("error") or ""), info

    def test_empty_result_is_success(self, client, db_setup, monkeypatch):
        """AT-08：空结果是正常完成，不当错误"""
        _patch_model(monkeypatch, [AIMessage(content="")])
        r = _create_run(client, db_setup, "什么都不说")
        assert r.status_code == 200
        run_id = r.json()["run_id"]
        info = _wait_run(client, db_setup["user1_id"], run_id)
        assert info["status"] == "completed", info

    @pytest.mark.asyncio
    async def test_cancel_not_misreported_as_success(self, client, db_setup, monkeypatch):
        """AT-08：取消 -> cancelled（done.status=cancelled），不被误报成功"""
        import asyncio
        import app.services.agent_runtime.factory as factory_mod
        import app.services.agent_runtime.service as svc_mod
        from langchain_core.language_models.chat_models import BaseChatModel
        from langchain_core.outputs import ChatGeneration, ChatResult

        from app.services.agent_runtime import get_runtime_service

        gate: Any = asyncio.Event()
        entered: Any = asyncio.Event()

        class HangingChatModel(BaseChatModel):
            @property
            def _llm_type(self) -> str:
                return "hang"

            def bind_tools(self, tools, **kwargs):
                return self

            def _generate(self, messages, stop=None, run_manager=None, **kwargs):
                raise RuntimeError("同步路径不使用")

            async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
                entered.set()
                await gate.wait()
                return ChatResult(generations=[ChatGeneration(message=AIMessage(content="late"))])

        async def fake_load_model(self, model_config_id):
            return (
                {"name": "p", "base_url": "http://x", "api_key": "k", "extra_config": {}},
                {"name": "fake", "max_tokens": None, "supports_streaming": True,
                 "supports_function_call": True, "extra_params": {}},
            )

        monkeypatch.setattr(svc_mod.AgentRuntimeService, "_load_model", fake_load_model)
        monkeypatch.setattr(factory_mod, "build_chat_model", lambda *a, **k: HangingChatModel())

        svc = get_runtime_service()
        run_id, stream = await svc.start_run(
            agent_config={"id": 990001, "tools": ["calculator"],
                          "system_prompt": "t", "temperature": "0.1"},
            messages=[{"role": "user", "content": "hi"}],
            user_id=db_setup["user1_id"],
        )
        await asyncio.wait_for(entered.wait(), timeout=10)
        res = await svc.cancel_run(run_id, db_setup["user1_id"])
        assert res["ok"], res

        events = [e async for e in stream]
        done = [e for e in events if e.get("type") == "done"]
        assert done and done[-1]["status"] == "cancelled", done
        info = await svc.get_run(run_id, db_setup["user1_id"])
        assert info["status"] == "cancelled"


# ── FR-07：SSE 事件回放 / 断线续传 ─────────────────────────────────────────


class TestRunEventsReplay:
    def test_events_replay_after_seq_and_last_event_id(self, client, db_setup, monkeypatch):
        """GET /runs/{id}/events：v2 信封 + sequence 连续 + after_seq/Last-Event-ID 续传"""
        _patch_model(monkeypatch, [AIMessage(content="HELLO_EVT")])
        r = _create_run(client, db_setup, "hi")
        assert r.status_code == 200
        run_id = r.json()["run_id"]
        _wait_run(client, db_setup["user1_id"], run_id)

        def fetch(after_seq=None, last_event_id=None):
            params = {"after_seq": after_seq} if after_seq is not None else {}
            headers = dict(_auth(db_setup["user1_id"]))
            if last_event_id is not None:
                headers["Last-Event-ID"] = str(last_event_id)
            r = client.get(
                f"/api/v2/runs/{run_id}/events", params=params, headers=headers,
            )
            assert r.status_code == 200
            assert r.headers["content-type"].startswith("text/event-stream")
            return r

        # 全量回放：v2 信封（event/sequence/run_id/data）+ sequence 连续 + 终止事件
        r = fetch()
        assert "id: " in r.text  # 标准 SSE id 行
        events = _iter_sse(r)
        assert events and events[-1]["event"] == "run.completed"
        seqs = [e["sequence"] for e in events]
        assert seqs == list(range(seqs[0], seqs[0] + len(seqs))), f"sequence 不连续: {seqs}"
        assert all("event" in e and "run_id" in e for e in events)

        # after_seq 续传：只返回 sequence 大于指定值的事件，直至终止
        resumed = _iter_sse(fetch(after_seq=events[0]["sequence"]))
        assert [e["sequence"] for e in resumed] == [e["sequence"] for e in events][1:]
        assert resumed[-1]["event"] == "run.completed"

        # Last-Event-ID（标准 SSE 重连头）与 after_seq 等价
        by_header = _iter_sse(fetch(last_event_id=events[0]["sequence"]))
        assert [e["sequence"] for e in by_header] == [e["sequence"] for e in resumed]

    def test_events_replay_forbidden_for_other_user(self, client, db_setup, monkeypatch):
        _patch_model(monkeypatch, [AIMessage(content="x")])
        r = _create_run(client, db_setup, "hi")
        run_id = r.json()["run_id"]
        _wait_run(client, db_setup["user1_id"], run_id)
        r = client.get(
            f"/api/v2/runs/{run_id}/events",
            headers=_auth(db_setup["user2_id"]),
        )
        assert r.status_code in (403, 404)


# ── AT-15 / §11-5：Reconciliation 仅限管理员（未授权被拒） ─────────────────

class TestReconcileAuthz:
    def test_reconcile_requires_superuser(self, client, db_setup):
        """P1-03 D：未授权用户（非超管）不能进行 Reconciliation（403）；
        超管通过 get_current_superuser 门禁。"""
        import os

        from app.models.user import User

        # 非超管（db_setup 的 u2 默认 is_superuser=False）-> 403
        r = client.post(
            "/api/v2/runs/some-run/reconcile",
            json={"tool_call_id": "tc-x", "disposition": "unresolved"},
            headers=_auth(db_setup["user2_id"]),
        )
        assert r.status_code == 403, f"非超管不得调用 Reconciliation: {r.status_code}"

        # 超管 -> 通过授权门禁（不是 401/403）
        engine = create_engine(os.environ["DATABASE_SYNC_URL"])
        with Session(engine) as s:
            admin = User(
                username="api_admin", email="admin@test.dev",
                hashed_password="x", is_active=True, is_superuser=True,
            )
            s.add(admin)
            s.commit()
            admin_id = admin.id
        r = client.post(
            "/api/v2/runs/some-run/reconcile",
            json={"tool_call_id": "tc-x", "disposition": "unresolved"},
            headers=_auth(admin_id),
        )
        assert r.status_code not in (401, 403), \
            f"超管应通过 Reconciliation 授权门禁: {r.status_code}"


# ── A22：助手列表用户隔离（P0-03）────────────────────────────────────────

class TestAgentListIsolation:
    def test_list_assistants_isolated_by_owner(self, client, db_setup):
        """列表不得泄露他人助手：普通用户只见自己的 + 公开（owner 为空）的"""
        import os

        from app.models.agent import Agent
        from app.models.user import User

        engine = create_engine(os.environ["DATABASE_SYNC_URL"])
        with Session(engine) as s:
            public = Agent(
                name="公开 Agent", description="public", system_prompt="t",
                tools=[], is_active=True, owner_id=None,
            )
            admin = User(
                username="iso_admin", email="iso_admin@test.dev",
                hashed_password="x", is_active=True, is_superuser=True,
            )
            s.add_all([public, admin])
            s.commit()
            public_id, admin_id = public.id, admin.id

        own_id = db_setup["agent_id"]
        u2 = _auth(db_setup["user2_id"])

        # 用户 2：看不到用户 1 的助手，能看到公开助手
        r = client.get("/api/v2/assistants", headers=u2)
        assert r.status_code == 200
        ids = {a["id"] for a in r.json()["items"]}
        assert own_id not in ids, f"他人助手泄露到列表: {ids}"
        assert public_id in ids
        assert r.json()["total"] == len(ids), "total 与 items 必须同口径过滤"

        # 用户 1：能看到自己的 + 公开的
        r = client.get("/api/v2/assistants", headers=_auth(db_setup["user1_id"]))
        ids = {a["id"] for a in r.json()["items"]}
        assert own_id in ids and public_id in ids

        # 超管：可见全部
        r = client.get("/api/v2/assistants", headers=_auth(admin_id))
        ids = {a["id"] for a in r.json()["items"]}
        assert own_id in ids and public_id in ids


# ── A23：全局异常响应脱敏（P0-03）──────────────────────────────────────────

class TestExceptionMasking:
    def test_global_handler_hides_exception_detail(self):
        """500 响应只含安全错误码 + request_id，不得回显 str(exc)"""
        import asyncio
        import json as _json

        from starlette.requests import Request

        from app.main import global_exception_handler

        scope = {
            "type": "http", "method": "GET", "path": "/api/v1/boom",
            "query_string": b"", "headers": [], "scheme": "http",
            "server": ("testserver", 80), "client": ("127.0.0.1", 1),
        }
        secret = "DB_PASSWORD=hunter2-internal"
        resp = asyncio.run(global_exception_handler(Request(scope), RuntimeError(secret)))

        assert resp.status_code == 500
        body = _json.loads(resp.body)
        assert "request_id" in body and body["request_id"], body
        assert "detail" not in body, f"detail 字段泄露: {body}"
        assert secret not in resp.body.decode(), "异常原文不得出现在响应中"
