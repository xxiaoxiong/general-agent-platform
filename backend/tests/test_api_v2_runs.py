"""P3-03/P3-06：Runs API v2 + 新事件协议（§7.1）+ 取消/重连续传。

运行：
    pytest backend/tests/test_api_v2_runs.py -q
"""
import json
import time

import pytest
from fastapi.testclient import TestClient
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from sqlalchemy import create_engine
from sqlalchemy.orm import Session


# ── 事件协议单测（§7.1 映射）─────────────────────────────────────────────────

class TestEventProtocol:
    def test_tool_started_mapping(self):
        from app.services.agent_runtime.event_protocol import to_v2_event

        ev = to_v2_event(
            run_id="r1", sequence=12, event_type="tool_started",
            payload={"tool": "web_search", "input": {"q": "x"}, "tool_call_id": "c1"},
        )
        assert ev["event"] == "tool.started"
        assert ev["run_id"] == "r1"
        assert ev["sequence"] == 12
        assert ev["trace_id"] == "r1"
        assert ev["data"]["tool_name"] == "web_search"
        assert ev["data"]["summary"]
        assert ev["timestamp"].endswith("Z")

    def test_terminal_events(self):
        from app.services.agent_runtime.event_protocol import is_terminal, to_v2_event

        completed = to_v2_event(run_id="r", sequence=3, event_type="run_completed",
                                payload={"final_text": "done"})
        assert completed["event"] == "run.completed"
        assert is_terminal(completed)

        waiting = to_v2_event(run_id="r", sequence=4, event_type="run_waiting_approval",
                              payload={"actions": [{"tool": "http_post"}]})
        assert waiting["event"] == "run.status_changed"
        assert waiting["data"]["status"] == "waiting_approval"
        assert waiting["data"]["actions"]
        assert is_terminal(waiting)

        delta = to_v2_event(run_id="r", sequence=5, event_type="model_delta",
                            payload={"text": "hi"})
        assert delta["event"] == "assistant.delta"
        assert not is_terminal(delta)

    def test_sse_format_lines(self):
        from app.services.agent_runtime.event_protocol import sse_format, to_v2_event

        frame = sse_format(to_v2_event(run_id="r1", sequence=7,
                                       event_type="run_started", payload={}))
        assert "id: 7\n" in frame
        assert "event: run.started\n" in frame
        assert frame.startswith("id: 7\nevent: run.started\ndata: ")
        json.loads(frame.split("data: ", 1)[1].strip())

    def test_synthetic_terminal(self):
        from app.services.agent_runtime.event_protocol import (
            is_terminal,
            synthetic_terminal_from_status,
        )

        ev = synthetic_terminal_from_status("r", "cancelled")
        assert ev["event"] == "run.cancelled"
        assert is_terminal(ev)


# ── Runs API v2 集成 ─────────────────────────────────────────────────────────


class FakeChatModel(BaseChatModel):
    script: list = []

    @property
    def _llm_type(self):
        return "fake-v2"

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        item = self.script.pop(0) if self.script else AIMessage(content="（结束）")
        msg = item() if callable(item) else item
        return ChatResult(generations=[ChatGeneration(message=msg)])


@pytest.fixture(scope="module")
def client():
    from app.main import app

    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def user_setup():
    import os

    from app.models.user import User

    engine = create_engine(os.environ["DATABASE_SYNC_URL"])
    with Session(engine) as s:
        u1 = User(username="run_u1", email="run_u1@test.dev", hashed_password="x", is_active=True)
        u2 = User(username="run_u2", email="run_u2@test.dev", hashed_password="x", is_active=True)
        s.add_all([u1, u2])
        s.commit()
        return {"user1_id": u1.id, "user2_id": u2.id}


def _auth(user_id: int) -> dict:
    from app.core.security import create_access_token

    return {"Authorization": f"Bearer {create_access_token(user_id)}"}


def _patch_model(monkeypatch, script):
    import app.services.agent_runtime.factory as factory_mod
    import app.services.agent_runtime.service as svc_mod

    async def fake_load_model(self, model_config_id):
        return (
            {"name": "fake-provider", "base_url": "http://fake.local/v1", "api_key": "none",
             "extra_config": {}},
            {"name": "fake", "max_tokens": None, "supports_streaming": True,
             "supports_function_call": True, "extra_params": {}},
        )

    monkeypatch.setattr(svc_mod.AgentRuntimeService, "_load_model", fake_load_model)
    monkeypatch.setattr(factory_mod, "build_chat_model", lambda *a, **k: FakeChatModel(script=list(script)))


def _iter_v2_sse(resp):
    """解析 v2 SSE 帧（id:/event:/data:）→ envelope 列表。"""
    envelopes = []
    for block in resp.text.split("\n\n"):
        block = block.strip()
        if not block or not block.startswith("id:"):
            continue
        data_line = next((l for l in block.splitlines() if l.startswith("data: ")), None)
        assert data_line is not None, block
        envelopes.append(json.loads(data_line[len("data: "):]))
    return envelopes


def _wait_run(client, run_id, user_id, timeout=20.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = client.get(f"/api/v2/runs/{run_id}", headers=_auth(user_id))
        assert r.status_code == 200
        info = r.json()
        if info["status"] in ("completed", "failed", "cancelled", "limit_exceeded",
                              "waiting_approval", "needs_reconciliation"):
            return info
        time.sleep(0.1)
    raise AssertionError(f"run {run_id} 未在 {timeout}s 内终结")


class TestRunsAPIv2:
    def test_create_run_events_and_reconnect(self, client, user_setup, monkeypatch):
        """UX-05/UX-08：创建 Run（X-Run-Id）→ v2 事件流 → Last-Event-ID 续传。"""
        from langchain_core.messages import AIMessage as A

        _patch_model(monkeypatch, [A(content="V2_HELLO")])
        resp = client.post(
            "/api/v2/runs",
            json={"message": "你好", "use_memory": False},
            headers=_auth(user_setup["user1_id"]),
        )
        assert resp.status_code == 200
        assert resp.headers.get("X-Run-Id")
        body = resp.json()
        run_id = body["run_id"]
        assert body["conversation_id"] > 0
        assert body["events_url"].endswith(f"/runs/{run_id}/events")

        info = _wait_run(client, run_id, user_setup["user1_id"])
        assert info["status"] == "completed"

        # v2 事件流：标准 SSE 帧 + §7.1 信封 + 序号连续
        r = client.get(f"/api/v2/runs/{run_id}/events", headers=_auth(user_setup["user1_id"]))
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/event-stream")
        envelopes = _iter_v2_sse(r)
        assert envelopes
        names = [e["event"] for e in envelopes]
        assert names[0] == "run.started"
        assert names[-1] == "run.completed"
        assert "assistant.delta" in names
        seqs = [e["sequence"] for e in envelopes]
        assert seqs == sorted(seqs)
        assert all(e["run_id"] == run_id for e in envelopes)
        assert all(e["trace_id"] for e in envelopes)
        assert envelopes[-1]["data"]["final_text"] == "V2_HELLO"

        # after_seq / Last-Event-ID 续传（§8.4 4）：不重复、不丢失、有序
        first_seq = seqs[0]
        resumed = _iter_v2_sse(client.get(
            f"/api/v2/runs/{run_id}/events",
            params={"after_seq": first_seq},
            headers=_auth(user_setup["user1_id"]),
        ))
        assert [e["sequence"] for e in resumed] == seqs[1:]
        by_header = _iter_v2_sse(client.get(
            f"/api/v2/runs/{run_id}/events",
            headers={**_auth(user_setup["user1_id"]), "Last-Event-ID": str(first_seq)},
        ))
        assert [e["sequence"] for e in by_header] == seqs[1:]

    def test_run_owner_isolation(self, client, user_setup, monkeypatch):
        _patch_model(monkeypatch, [AIMessage(content="PRIVATE")])
        resp = client.post(
            "/api/v2/runs", json={"message": "hi", "use_memory": False},
            headers=_auth(user_setup["user1_id"]),
        )
        run_id = resp.json()["run_id"]
        _wait_run(client, run_id, user_setup["user1_id"])

        assert client.get(f"/api/v2/runs/{run_id}",
                          headers=_auth(user_setup["user2_id"])).status_code == 404
        assert client.get(f"/api/v2/runs/{run_id}/events",
                          headers=_auth(user_setup["user2_id"])).status_code == 404
        assert client.post(f"/api/v2/runs/{run_id}/cancel",
                           headers=_auth(user_setup["user2_id"])).status_code == 404

    def test_conversation_messages_written_back(self, client, user_setup, monkeypatch):
        """UX-10：Run 完成后会话消息写回（用户 + 助手）。"""
        _patch_model(monkeypatch, [AIMessage(content="WRITE_BACK")])
        resp = client.post(
            "/api/v2/runs", json={"message": "写回测试", "use_memory": False},
            headers=_auth(user_setup["user1_id"]),
        )
        conv_id = resp.json()["conversation_id"]
        run_id = resp.json()["run_id"]
        _wait_run(client, run_id, user_setup["user1_id"])

        # 写回经 Outbox 异步投递：轮询消息历史
        deadline = time.time() + 10
        items = []
        while time.time() < deadline:
            items = client.get(
                f"/api/v2/conversations/{conv_id}/messages",
                headers=_auth(user_setup["user1_id"]),
            ).json()["items"]
            if len(items) >= 2:
                break
            time.sleep(0.1)
        roles = [m.get("role") for m in items]
        assert "user" in roles and "assistant" in roles

    def test_cancel_terminal_run_reports_status(self, client, user_setup, monkeypatch):
        """UX-07：对终态 Run 取消 → 明确终态反馈，不伪造取消成功。"""
        _patch_model(monkeypatch, [AIMessage(content="DONE")])
        resp = client.post(
            "/api/v2/runs", json={"message": "hi", "use_memory": False},
            headers=_auth(user_setup["user1_id"]),
        )
        run_id = resp.json()["run_id"]
        info = _wait_run(client, run_id, user_setup["user1_id"])
        assert info["status"] == "completed"

        r = client.post(f"/api/v2/runs/{run_id}/cancel",
                        json={"reason": "用户停止"},
                        headers=_auth(user_setup["user1_id"]))
        assert r.status_code == 200
        assert r.json()["status"] == "completed"

    def test_cancel_nonexistent_run(self, client, user_setup):
        r = client.post("/api/v2/runs/does-not-exist/cancel",
                        headers=_auth(user_setup["user1_id"]))
        assert r.status_code == 404

    def test_resume_precheck_errors(self, client, user_setup, monkeypatch):
        """UX-09：审批决策校验映射（未知动作 400 / 不存在 404）。"""
        _patch_model(monkeypatch, [AIMessage(content="R")])
        resp = client.post(
            "/api/v2/runs", json={"message": "hi", "use_memory": False},
            headers=_auth(user_setup["user1_id"]),
        )
        run_id = resp.json()["run_id"]
        _wait_run(client, run_id, user_setup["user1_id"])

        # 无审批记录时未知 action_id → 400（ApprovalInvalid）
        r = client.post(f"/api/v2/runs/{run_id}/resume",
                        json={"decisions": [{"type": "approve", "action_id": "nope"}]},
                        headers=_auth(user_setup["user1_id"]))
        assert r.status_code == 400

        r = client.post("/api/v2/runs/no-such-run/resume",
                        json={"decisions": [{"type": "approve"}]},
                        headers=_auth(user_setup["user1_id"]))
        assert r.status_code == 404

    def test_create_run_idempotency(self, client, user_setup, monkeypatch):
        """P1-01：同 Key 同摘要复用原 Run，不重复执行。"""
        _patch_model(monkeypatch, [AIMessage(content="IDEM_V2")])
        headers = {**_auth(user_setup["user1_id"]), "Idempotency-Key": "v2-idem-1"}
        r1 = client.post("/api/v2/runs", json={"message": "幂等", "use_memory": False}, headers=headers)
        r2 = client.post("/api/v2/runs", json={"message": "幂等", "use_memory": False}, headers=headers)
        assert r1.status_code == 200 and r2.status_code == 200
        assert r1.json()["run_id"] == r2.json()["run_id"]
        assert r2.json().get("idempotent_reuse") is True

        # 同 Key 不同摘要 → 409
        r3 = client.post("/api/v2/runs", json={"message": "不同输入", "use_memory": False}, headers=headers)
        assert r3.status_code == 409
