"""P3-03/P3-06：Runs API v2 + 新事件协议（§7.1）+ 取消/重连续传。

运行：
    pytest backend/tests/test_api_v2_runs.py -q
"""
import json
import time
from pathlib import Path

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

    def test_team_mode_requires_explicit_boolean_and_participates_in_idempotency(self, client, user_setup, monkeypatch):
        _patch_model(monkeypatch, [AIMessage(content="单助手回答")])
        user_id = user_setup["user1_id"]
        headers = {**_auth(user_id), "Idempotency-Key": "v2-team-mode-opt-in"}
        body = {"message": "请解释物料盘点规则", "use_memory": False}

        invalid = client.post("/api/v2/runs", json={**body, "team_mode": "true"}, headers=headers)
        assert invalid.status_code == 422
        assert invalid.json()["detail"][0]["loc"] == ["body", "team_mode"]

        omitted = client.post("/api/v2/runs", json=body, headers=headers)
        assert omitted.status_code == 200
        run_id = omitted.json()["run_id"]
        disabled = client.post("/api/v2/runs", json={**body, "team_mode": False}, headers=headers)
        assert disabled.status_code == 200
        assert disabled.json()["run_id"] == run_id
        assert disabled.json()["idempotent_reuse"] is True

        enabled = client.post("/api/v2/runs", json={**body, "team_mode": True}, headers=headers)
        assert enabled.status_code == 409
        assert _wait_run(client, run_id, user_id)["status"] == "completed"
        history = client.get(f"/api/v2/runs/{run_id}/events", headers=_auth(user_id))
        assert history.status_code == 200
        events = _iter_v2_sse(history)
        assert all(event["event"] != "team.updated" for event in events)
        assert events[-1]["data"]["final_text"] == "单助手回答"

    def test_long_task_repairs_a_missing_artifact_and_preserves_the_real_final_answer(self, client, user_setup, monkeypatch, tmp_path):
        from app.core.config import settings

        def call(name, args, identifier):
            return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": identifier}])

        monkeypatch.setattr(settings, "AGENT_FILES_ROOT", str(tmp_path))
        monkeypatch.setattr(settings, "AGENT_VERIFICATION_MAX_ROUNDS", 2)
        _patch_model(monkeypatch, [
            call("write_todos", {"todos": [{"content": "核对清单", "status": "in_progress"},
                                          {"content": "保存事实明细", "status": "pending"}]}, "deliver-plan"),
            call("write_file", {"file_path": "/workspace/audit/report.md", "content": "# 盘点报告\n\n三箱实物。"}, "deliver-report"),
            AIMessage(content="初稿。报告写好了，全部完成。"),
            call("write_file", {"file_path": "/workspace/audit/facts.json", "content": '{"boxes":3}'}, "deliver-facts"),
            call("write_todos", {"todos": [{"content": "核对清单", "status": "completed"},
                                          {"content": "保存事实明细", "status": "completed"}]}, "deliver-plan-done"),
            AIMessage(content="盘点结束。报告与事实明细均可下载。"),
        ])
        owner = user_setup["user1_id"]
        headers = _auth(owner)
        started = client.post("/api/v2/runs", json={"message": "分阶段核对库存，实盘三箱。保存为 `audit/report.md` 与 `audit/facts.json` 两个文件。"}, headers=headers)
        assert started.status_code == 200, started.text
        run = started.json()
        assert _wait_run(client, run["run_id"], owner)["status"] == "completed"
        events = _iter_v2_sse(client.get(run["events_url"], headers=headers))
        corrections = [event["data"] for event in events if event["event"] == "run.retrying"]
        assert len(corrections) == 1
        assert {check["id"] for check in corrections[0]["failed_checks"]} == {"artifact_produced", "plan_finished"}
        assert events[-1]["data"]["final_text"] == "盘点结束。报告与事实明细均可下载。"
        verification = events[-1]["data"]["summary"]["verification"]
        assert verification["status"] == "passed" and verification["fix_rounds"] == 1
        artifacts = {item["path"]: item["download_url"] for item in verification["artifacts"]}
        assert set(artifacts) == {"audit/report.md", "audit/facts.json"}
        assert client.get(artifacts["audit/facts.json"], headers=headers).json() == {"boxes": 3}
        assert client.get(artifacts["audit/facts.json"], headers=_auth(user_setup["user2_id"])).status_code == 404

        # 同名工作文件可继续修改，旧交付仍指向本轮已验收的内容。
        _patch_model(monkeypatch, [
            call("edit_file", {"file_path": "/workspace/audit/facts.json", "old_string": '{"boxes":3}',
                               "new_string": '{"boxes":4}'}, "deliver-edit"),
            AIMessage(content="已核对并保存为四箱。"),
        ])
        updated = client.post("/api/v2/runs", json={
            "conversation_id": run["conversation_id"], "task_mode": "chat",
            "message": "把箱数改成四箱并保存为 audit/facts.json。",
        }, headers=headers)
        assert updated.status_code == 200, updated.text
        new_run = updated.json()
        assert _wait_run(client, new_run["run_id"], owner)["status"] == "completed"
        new_events = _iter_v2_sse(client.get(new_run["events_url"], headers=headers))
        new_artifact = new_events[-1]["data"]["summary"]["verification"]["artifacts"][0]
        assert client.get(new_artifact["download_url"], headers=headers).json() == {"boxes": 4}
        assert client.get(artifacts["audit/facts.json"], headers=headers).json() == {"boxes": 3}

    def test_exhausted_verification_keeps_partial_text_and_reports_failure(self, client, user_setup, monkeypatch):
        model_output = "初步发现，建议继续核对。"
        _patch_model(monkeypatch, [AIMessage(content=model_output), AIMessage(content=model_output)])
        owner = user_setup["user1_id"]
        headers = _auth(owner)
        started = client.post("/api/v2/runs", json={"message": "深入调研行业现状，引用至少两个来源。"}, headers=headers).json()
        assert _wait_run(client, started["run_id"], owner)["status"] == "failed"
        events = _iter_v2_sse(client.get(started["events_url"], headers=headers))
        assert events[-1]["event"] == "run.failed"
        assert events[-1]["data"]["final_text"] == model_output
        report = events[-1]["data"]["summary"]["verification"]
        assert report["stop_reason"] == "no_progress"
        assert any(check["id"] == "citations_present" and not check["passed"] for check in report["checks"])

    @pytest.mark.parametrize("reason", ["budget", "execution"])
    def test_interrupted_complex_task_preserves_confirmed_files(self, client, user_setup, monkeypatch, tmp_path, reason):
        from app.core.config import settings

        def failure():
            raise RuntimeError("模型连接中断")

        monkeypatch.setattr(settings, "AGENT_FILES_ROOT", str(tmp_path))
        monkeypatch.setattr(settings, "AGENT_MAX_MODEL_CALLS", 1 if reason == "budget" else 10)
        excerpt = "已生成报告草稿，事实表尚待保存。"
        _patch_model(monkeypatch, [
            AIMessage(content=excerpt, tool_calls=[{"name": "write_file", "id": "draft-before-interruption", "args": {
                "file_path": "/workspace/report.md", "content": "# 报告\n\n第一部分已核对。"}}]),
            failure,
        ])
        owner = user_setup["user1_id"]
        headers = _auth(owner)
        assistant = client.post("/api/v2/assistants", headers=headers, json={"name": f"长任务中断验收-{reason}"})
        assert assistant.status_code == 200, assistant.text
        response = client.post("/api/v2/runs", headers=headers, json={
            "assistant_id": assistant.json()["id"], "task_mode": "chat",
            "message": "分阶段核对并保存为 report.md 和 facts.json 两个文件。",
        })
        assert response.status_code == 200, response.text
        run = response.json()
        expected = "limit_exceeded" if reason == "budget" else "failed"
        assert _wait_run(client, run["run_id"], owner)["status"] == expected
        events = _iter_v2_sse(client.get(run["events_url"], headers=headers))
        assert events[-1]["event"] == "run.failed"
        terminal = events[-1]["data"]
        assert terminal["status"] == expected and terminal["final_text"] == excerpt
        verification = terminal["summary"]["verification"]
        assert verification["stop_reason"] == reason and verification["status"] != "passed"
        assert {item["id"] for item in verification["checks"] if not item["passed"]} == {
            "artifact_produced", "execution_interrupted",
        }
        assert [item["path"] for item in verification["artifacts"]] == ["report.md"]
        downloaded = client.get(verification["artifacts"][0]["download_url"], headers=headers)
        assert downloaded.status_code == 200 and "第一部分已核对" in downloaded.text
        if reason == "budget":
            return

        _patch_model(monkeypatch, [
            AIMessage(content="", tool_calls=[{"name": "write_file", "id": "write-missing-facts", "args": {
                "file_path": "/workspace/facts.json", "content": '{"total": 2}'}}]),
            AIMessage(content="两份文件已齐备，事实表已补齐。"),
        ])
        continued = client.post("/api/v2/runs", headers=headers, json={
            "conversation_id": run["conversation_id"], "message": "继续",
        })
        assert continued.status_code == 200, continued.text
        next_run = continued.json()
        assert _wait_run(client, next_run["run_id"], owner)["status"] == "completed"
        history = _iter_v2_sse(client.get(next_run["events_url"], headers=headers))
        started_tools = [event["data"] for event in history if event["event"] == "tool.started"]
        assert len(started_tools) == 1 and started_tools[0]["tool_call_id"] == "write-missing-facts"
        report = history[-1]["data"]["summary"]["verification"]
        assert report["status"] == "passed"
        files = {item["path"]: item["download_url"] for item in report["artifacts"]}
        assert set(files) == {"report.md", "facts.json"}
        assert client.get(files["report.md"], headers=headers).content == downloaded.content
        assert client.get(files["facts.json"], headers=headers).json() == {"total": 2}

    def test_model_length_stop_continues_the_same_answer(self, client, user_setup, monkeypatch):
        _patch_model(monkeypatch, [AIMessage(content="第一部分。", response_metadata={"finish_reason": "length"}),
                                  AIMessage(content="第二部分。")])
        owner = user_setup["user1_id"]
        headers = _auth(owner)
        started = client.post("/api/v2/runs", json={"message": "用两段话解释拼图游戏。"}, headers=headers).json()
        assert _wait_run(client, started["run_id"], owner)["status"] == "completed"
        events = _iter_v2_sse(client.get(started["events_url"], headers=headers))
        assert events[-1]["data"]["final_text"] == "第一部分。\n第二部分。"
        assert any(event["event"] == "run.retrying" and event["data"]["failed_checks"][0]["id"] == "answer_complete" for event in events)

    def test_files_exist_but_table_numbers_must_match_source_before_completion(self, client, user_setup, monkeypatch, tmp_path):
        from app.core.config import settings

        monkeypatch.setattr(settings, "AGENT_FILES_ROOT", str(tmp_path))
        path = "/workspace/facts.json"
        bad, good = '{"valid_rows":1}', '{"valid_rows":2}'
        _patch_model(monkeypatch, [
            AIMessage(content="", tool_calls=[{"name": "write_file", "id": "write-bad-count", "args": {
                "file_path": path, "content": bad}}]),
            AIMessage(content="有效记录 1 条，已完成。"),
            AIMessage(content="", tool_calls=[{"name": "edit_file", "id": "correct-count", "args": {
                "file_path": path, "old_string": bad, "new_string": good}}]),
            AIMessage(content="有效记录 2 条，通过复算。"),
        ])
        owner = user_setup["user1_id"]
        headers = _auth(owner)
        message = ("核对有效记录数，按编号去重，忽略 VOID。保存为 facts.json，字段 valid_rows 为有效记录数。\n"
                   "字段为 编号,数值,状态：\nS1,2,OK\nS1,2,OK\nS2,7,VOID\nS3,3,OK")
        run = client.post("/api/v2/runs", json={"message": message}, headers=headers).json()
        assert _wait_run(client, run["run_id"], owner)["status"] == "completed"
        events = _iter_v2_sse(client.get(run["events_url"], headers=headers))
        retry = next(event for event in events if event["event"] == "run.retrying")
        assert [check["id"] for check in retry["data"]["failed_checks"]] == ["table_facts"]
        report = events[-1]["data"]["summary"]["verification"]
        assert report["fix_rounds"] == 1
        assert client.get(report["artifacts"][0]["download_url"], headers=headers).json() == {"valid_rows": 2}

    def test_project_repair_keeps_the_original_bundle_and_uses_a_new_run(self, client, user_setup, monkeypatch, tmp_path):
        from app.core.config import settings
        from app.services.agent_runtime import team_runtime

        owner = user_setup["user1_id"]
        headers = _auth(owner)
        fixture = Path(__file__).parent / "e2e" / "fixtures" / "team-project"
        tasks = [
            {"id": "api", "role": "后端工程", "title": "交付 API", "brief": "实现工单接口", "depends_on": [],
             "files": ["openapi.json", "backend/app.py"]},
            {"id": "ui", "role": "前端工程", "title": "交付界面", "brief": "实现工单界面", "depends_on": ["api"],
             "files": ["frontend/index.html", "frontend/style.css", "frontend/app.js"]},
            {"id": "qa", "role": "测试工程", "title": "测试交付", "brief": "交付用例与文档", "depends_on": ["ui"],
             "files": ["tests/test_api.py", "README.md"]},
        ]
        plan = {"name": "巡检工单台", "summary": "跟踪巡检工单", "features": ["录入和筛选工单"],
                "api_contract": "GET /api/tickets 返回工单列表", "tasks": tasks}
        should_repair = False
        calls = []

        class ProjectModel(FakeChatModel):
            def _generate(self, messages, stop=None, run_manager=None, **kwargs):
                system = messages[0].content
                data = json.loads(messages[-1].content)
                if "项目立项" in system:
                    text = json.dumps(plan, ensure_ascii=False)
                else:
                    path = data["path"]
                    calls.append(path)
                    filename = path + ".fixture" if path == "tests/test_api.py" else path
                    text = (fixture / filename).read_text(encoding="utf-8")
                    if path == "frontend/app.js" and not should_repair:
                        text = "// 未实现点击事件和工单 API 请求\n"
                return ChatResult(generations=[ChatGeneration(message=AIMessage(content=text))])

        monkeypatch.setattr(settings, "AGENT_FILES_ROOT", str(tmp_path))
        _patch_model(monkeypatch, [])
        monkeypatch.setattr(team_runtime, "build_chat_model", lambda *args, **kwargs: ProjectModel())
        started = client.post("/api/v2/runs", json={"message": "帮我构建前后端分离的工单管理项目", "team_mode": True}, headers=headers)
        assert started.status_code == 200, started.text
        initial = started.json()
        assert _wait_run(client, initial["run_id"], owner)["status"] == "failed"
        older = _iter_v2_sse(client.get(initial["events_url"], headers=headers))
        draft = next(event["data"] for event in older if event["event"] == "project.delivery")
        original_bytes = client.get(draft["download_url"], headers=headers).content
        assert draft["state"] == "failed" and original_bytes[:2] == b"PK"
        assert {issue["path"] for issue in draft["issues"]} == {"frontend/app.js"}
        url = f"/api/v2/runs/{initial['run_id']}/project-repair"
        assert client.post(url, json={}, headers=_auth(user_setup["user2_id"])).status_code == 404

        should_repair = True
        calls.clear()
        repair_headers = {**headers, "Idempotency-Key": initial["run_id"] + "-repair"}
        repaired = client.post(url, json={}, headers=repair_headers)
        assert repaired.status_code == 200, repaired.text
        new = repaired.json()
        assert new["conversation_id"] == initial["conversation_id"]
        assert new["run_id"] != initial["run_id"]
        duplicate = client.post(url, json={}, headers=repair_headers)
        assert duplicate.json()["run_id"] == new["run_id"]
        assert client.post(url, json={"model_config_id": 101}, headers=repair_headers).status_code == 409
        assert _wait_run(client, new["run_id"], owner)["status"] == "completed"
        events = _iter_v2_sse(client.get(new["events_url"], headers=headers))
        assert calls == ["frontend/app.js"]
        assert any("已载入草稿" in event["data"].get("detail", "") for event in events if event["event"] == "project.progress")
        delivery = next(event["data"] for event in events if event["event"] == "project.delivery")
        assert delivery["state"] == "passed" and delivery["download_url"] != draft["download_url"]
        assert client.get(draft["download_url"], headers=headers).content == original_bytes
