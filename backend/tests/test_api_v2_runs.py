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

    def test_tool_completed_carries_sources(self):
        from app.services.agent_runtime.event_protocol import to_v2_event

        ev = to_v2_event(
            run_id="r1", sequence=8, event_type="tool_completed",
            payload={"tool": "web_search", "tool_call_id": "c1", "output": "…",
                     "sources": [{"id": 1, "title": "T", "url": "https://example.com/a", "desc": ""}]},
        )
        assert ev["event"] == "tool.completed"
        assert ev["data"]["sources"][0]["url"] == "https://example.com/a"

        plain = to_v2_event(run_id="r1", sequence=9, event_type="tool_completed",
                            payload={"tool": "calculator", "tool_call_id": "c2", "output": "42"})
        assert "sources" not in plain["data"]

    def test_tool_and_subagent_events_carry_attribution(self):
        """子代理工具事件带 subagent 归属；subagent.started/completed 携带 name 状态。"""
        from app.services.agent_runtime.event_protocol import to_v2_event

        tagged = to_v2_event(
            run_id="r1", sequence=10, event_type="tool_completed",
            payload={"tool": "web_search", "tool_call_id": "c3", "output": "…", "subagent": "researcher"},
        )
        assert tagged["event"] == "tool.completed"
        assert tagged["data"]["subagent"] == "researcher"

        plain = to_v2_event(run_id="r1", sequence=11, event_type="tool_completed",
                            payload={"tool": "calculator", "tool_call_id": "c4", "output": "42"})
        assert "subagent" not in plain["data"]

        started = to_v2_event(run_id="r1", sequence=12, event_type="subagent_started",
                              payload={"name": "researcher", "tool_call_id": "c5"})
        assert started["event"] == "subagent.started"
        assert started["data"]["name"] == "researcher"
        assert started["data"]["tool_call_id"] == "c5"

        done = to_v2_event(run_id="r1", sequence=13, event_type="subagent_completed",
                           payload={"name": "researcher", "tool_call_id": "c5", "status": "done"})
        assert done["event"] == "subagent.completed"
        assert done["data"]["status"] == "done"

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

    def test_terminal_verification_includes_generated_artifacts_after_source_files(self):
        from app.services.agent_runtime.events import RUN_CANCELLED, RuntimeEvent, event_storage_payload

        diagnostic = "浏览器没有保存工单。" * 50
        artifacts = [{"path": f"src/file{index}.py"} for index in range(25)]
        artifacts.append({"path": "project.zip", "download_url": "/api/v2/artifacts/verified"})
        report = {"version": 2, "artifacts": artifacts, "checks": [
            {"id": "runtime_browser", "passed": False, "detail": diagnostic},
        ]}
        stored = event_storage_payload(RuntimeEvent(type=RUN_CANCELLED, run_id="r", data={
            "summary": {"verification": report},
        }))
        assert stored["summary"]["verification"] == report


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

    def test_stalled_verification_pauses_with_partial_text(self, client, user_setup, monkeypatch):
        model_output = "初步发现，建议继续核对。"
        _patch_model(monkeypatch, [AIMessage(content=model_output), AIMessage(content=model_output)])
        owner = user_setup["user1_id"]
        headers = _auth(owner)
        started = client.post("/api/v2/runs", json={"message": "深入调研行业现状，引用至少两个来源。"}, headers=headers).json()
        detail = _wait_run(client, started["run_id"], owner)
        assert detail["status"] == "limit_exceeded"
        assert detail["summary"]["stop"] == {"reason": "no_progress", "can_continue": False}
        events = _iter_v2_sse(client.get(started["events_url"], headers=headers))
        assert events[-1]["event"] == "run.failed"
        assert events[-1]["data"]["final_text"] == model_output
        assert events[-1]["data"]["summary"]["stop"] == detail["summary"]["stop"]
        report = events[-1]["data"]["summary"]["verification"]
        assert report["stop_reason"] == "no_progress"
        assert any(check["id"] == "citations_present" and not check["passed"] for check in report["checks"])

    def test_main_project_requires_runtime_evidence_and_downloads_the_verified_snapshot(self, client, user_setup, monkeypatch, tmp_path):
        import io
        from zipfile import ZipFile

        from app.core.config import settings
        from app.services.agent_runtime import workspace_delivery
        from app.services.agent_runtime.project_verifier_protocol import source_digest

        def call(name, args, identifier):
            return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": identifier}])

        monkeypatch.setattr(settings, "AGENT_FILES_ROOT", str(tmp_path))
        monkeypatch.setattr(settings, "AGENT_PROJECT_VERIFIER_DIR", str(tmp_path / "worker"))
        observed = []
        api = {"openapi": "3.1.0", "info": {"title": "工单", "version": "1.0"}, "paths": {"/api/items": {"get": {}}}}

        async def verifier(files, scope, *, capture_openapi=False, capture_browser_errors=False):
            assert scope == "fullstack"
            if capture_openapi:
                return {"openapi": api}
            assert capture_browser_errors
            assert json.loads(files["openapi.json"]) == api
            has_browser = "tests/browser-checks.json" in files
            observed.append((source_digest(files), has_browser))
            checks = {"runtime.start": "passed", "runtime.web": "passed", "runtime.web_build": "passed",
                      "runtime.tests.1": "passed", "runtime.browser": "passed"}
            checks["runtime.browser.flow.1" if has_browser else "runtime.browser.flows"] = "passed" if has_browser else "skipped"
            return {"source_digest": observed[-1][0], "checks": [{"id": key, "status": status} for key, status in checks.items()]}

        monkeypatch.setattr(workspace_delivery, "verify_runtime", verifier)
        contract = '{"version":1,"scenarios":[{"name":"登记工单","steps":[{"action":"open","path":"/"},{"action":"click","selector":"#create"},{"action":"expect_text","selector":"#list","value":"已登记"}]}]}'
        _patch_model(monkeypatch, [
            call("write_file", {"file_path": "/workspace/frontend/index.html", "content": '<button id="create">登记</button>'}, "project-page"),
            call("write_file", {"file_path": "/workspace/backend/main.py", "content": "from fastapi import FastAPI\napp = FastAPI()"}, "project-api"),
            AIMessage(content="已创建文件，交付完成。"),
            call("write_file", {"file_path": "/workspace/tests/browser-checks.json", "content": contract}, "project-browser-checks"),
            call("verify_workspace_project", {}, "project-verify"),
            AIMessage(content="浏览器验证通过，可下载工程包。"),
        ])
        owner = user_setup["user1_id"]
        headers = _auth(owner)
        response = client.post("/api/v2/runs", headers=headers, json={
            "team_mode": False, "task_mode": "chat", "message": (
                "构建并交付可直接运行的前后端分离项目。执行真实生产构建，提供可执行测试、OpenAPI 和源码 ZIP。请用浏览器验证登记工单。"
            ),
        })
        assert response.status_code == 200, response.text
        run = response.json()
        assert _wait_run(client, run["run_id"], owner)["status"] == "completed"
        assert [value for _, value in observed] == [False, True]
        events = _iter_v2_sse(client.get(run["events_url"], headers=headers))
        assert any(event["event"] == "tool.completed" and event["data"]["tool_call_id"] == "project-verify" for event in events)
        assert any(event["event"] == "run.retrying" and {"runtime_browser", "archive"} <= {
            row["id"] for row in event["data"]["failed_checks"]} for event in events)
        review = events[-1]["data"]["summary"]["verification"]
        assert all(row["passed"] for row in review["checks"])
        published = next(row for row in review["artifacts"] if row["path"].endswith(".zip"))
        data = client.get(published["download_url"], headers=headers)
        assert data.status_code == 200
        with ZipFile(io.BytesIO(data.content)) as package:
            archive_files = {name: package.read(name).decode() for name in package.namelist() if name != "verification.json"}
            assert json.loads(package.read("verification.json"))["source_digest"] == observed[-1][0] == source_digest(archive_files)
            assert archive_files["tests/browser-checks.json"] == contract

    @pytest.mark.parametrize("ending", ["browser_failed", "budget_after_verified", "source_changed"])
    def test_interrupted_main_project_uses_latest_exact_verifier_receipt(self, client, user_setup, monkeypatch, tmp_path, ending):
        import asyncio
        from threading import Event

        from app.core.config import settings
        from app.services.agent_runtime import workspace_delivery
        from app.services.agent_runtime.events import WORKSPACE_PROJECT_REVIEWED
        from app.services.agent_runtime.project_verifier_protocol import source_digest

        def call(name, args, identifier):
            return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": identifier}])

        async def verifier(files, scope, *, capture_openapi=False, capture_browser_errors=False):
            assert scope == "fullstack"
            if capture_openapi:
                return {"openapi": {"openapi": "3.1.0", "info": {"title": "工单", "version": "1.0"},
                                    "paths": {"/api/items": {"post": {}}}}}
            assert capture_browser_errors
            checks = [{"id": key, "status": "passed"} for key in (
                "runtime.start", "runtime.web", "runtime.web_build", "runtime.tests.1", "runtime.browser",
            )]
            if ending == "browser_failed":
                detail = "登记工单后列表缺少新记录" if "新增" in files["frontend/index.html"] else "先前浏览器报错"
                checks.append({"id": "runtime.browser.flow.1", "status": "failed", "detail": detail})
            else:
                checks.append({"id": "runtime.browser.flow.1", "status": "passed"})
            return {"source_digest": source_digest(files), "checks": checks}

        monkeypatch.setattr(settings, "AGENT_FILES_ROOT", str(tmp_path))
        monkeypatch.setattr(settings, "AGENT_PROJECT_VERIFIER_DIR", str(tmp_path / "worker"))
        monkeypatch.setattr(workspace_delivery, "verify_runtime", verifier)
        script = [
            call("write_file", {"file_path": "/workspace/frontend/index.html", "content": "<button>登记</button>"}, "write-page"),
            call("write_file", {"file_path": "/workspace/backend/main.py", "content": "from fastapi import FastAPI\napp = FastAPI()"}, "write-api"),
            call("verify_workspace_project", {}, "verify-project"),
        ]
        if ending in {"browser_failed", "source_changed"}:
            script.append(call("edit_file", {"file_path": "/workspace/frontend/index.html", "old_string": "登记",
                                             "new_string": "新增"}, "edit-page"))
        if ending == "browser_failed":
            script.append(call("verify_workspace_project", {}, "verify-updated-project"))
        waiting = Event()
        generate = FakeChatModel._agenerate

        async def wait_after_tools(self, *args, **kwargs):
            if not self.script:
                waiting.set()
                await asyncio.sleep(60)
            return await generate(self, *args, **kwargs)

        monkeypatch.setattr(FakeChatModel, "_agenerate", wait_after_tools)
        if ending == "budget_after_verified":
            monkeypatch.setattr(settings, "AGENT_MAX_MODEL_CALLS", len(script))
        _patch_model(monkeypatch, script)
        owner = user_setup["user1_id"]
        headers = _auth(owner)
        response = client.post("/api/v2/runs", headers=headers, json={
            "team_mode": False, "task_mode": "chat",
            "message": "构建并交付可直接运行的前后端分离项目。执行真实生产构建，提供可执行测试、OpenAPI 和源码 ZIP。请用浏览器验证登记工单。",
        })
        assert response.status_code == 200, response.text
        run = response.json()
        if ending == "budget_after_verified":
            assert _wait_run(client, run["run_id"], owner)["status"] == "limit_exceeded"
        else:
            assert waiting.wait(10)
            assert client.post(f"/api/v2/runs/{run['run_id']}/cancel", headers=headers).status_code == 200
            assert _wait_run(client, run["run_id"], owner)["status"] == "cancelled"
        events = _iter_v2_sse(client.get(run["events_url"], headers=headers))
        assert all(event["event"] != WORKSPACE_PROJECT_REVIEWED for event in events)
        report = events[-1]["data"]["summary"]["verification"]
        checks = {row["id"]: row for row in report["checks"]}
        assert not checks["execution_interrupted"]["passed"]
        assert report["status"] != "passed"
        paths = {artifact["path"]: artifact["download_url"] for artifact in report["artifacts"]}
        assert {"frontend/index.html", "backend/main.py"} <= paths.keys()
        archives = [url for path, url in paths.items() if path.endswith(".zip")]
        if ending == "browser_failed":
            assert report["stop_reason"] == "cancelled"
            assert not checks["runtime_browser"]["passed"]
            assert "登记工单后列表缺少新记录" in checks["runtime_browser"]["detail"]
            assert "先前浏览器报错" not in checks["runtime_browser"]["detail"]
            assert not archives
        elif ending == "source_changed":
            assert report["stop_reason"] == "cancelled"
            assert not checks["project_sources"]["passed"]
            assert "runtime_browser" not in checks
            assert not archives and "openapi.json" not in paths
        else:
            assert report["stop_reason"] == "budget"
            assert checks["runtime_browser"]["passed"]
            assert len(archives) == 1 and client.get(archives[0], headers=headers).content.startswith(b"PK")
            assert client.get(paths["openapi.json"], headers=headers).json()["info"]["title"] == "工单"

    def test_stopping_main_file_task_retains_saved_evidence_for_continue(self, client, user_setup, monkeypatch, tmp_path):
        import asyncio
        from threading import Event

        from app.core.config import settings

        def write(path, content, identifier):
            return AIMessage(content="", tool_calls=[{"name": "write_file", "id": identifier, "args": {
                "file_path": f"/workspace/{path}", "content": content,
            }}])

        monkeypatch.setattr(settings, "AGENT_FILES_ROOT", str(tmp_path))
        waiting = Event()
        generate = FakeChatModel._agenerate

        async def wait_after_first_write(self, *args, **kwargs):
            if not self.script:
                waiting.set()
                await asyncio.sleep(60)
            return await generate(self, *args, **kwargs)

        monkeypatch.setattr(FakeChatModel, "_agenerate", wait_after_first_write)
        _patch_model(monkeypatch, [write("report.md", "# 已确认的盘点报告", "saved-before-stop")])
        owner = user_setup["user1_id"]
        headers = _auth(owner)
        run = client.post("/api/v2/runs", headers=headers, json={
            "message": "分阶段盘点并保存为 report.md 和 facts.json 两份文件。",
        }).json()
        assert waiting.wait(5)
        assert client.post(f"/api/v2/runs/{run['run_id']}/cancel", headers=headers).status_code == 200
        assert _wait_run(client, run["run_id"], owner)["status"] == "cancelled"
        terminal = _iter_v2_sse(client.get(run["events_url"], headers=headers))[-1]
        assert terminal["event"] == "run.cancelled"
        report = terminal["data"]["summary"]["verification"]
        assert report["stop_reason"] == "cancelled"
        assert [item["path"] for item in report["artifacts"]] == ["report.md"]

        _patch_model(monkeypatch, [write("facts.json", '{"count":2}', "save-remaining"), AIMessage(content="两份文件已齐备。")])
        next_run = client.post("/api/v2/runs", headers=headers, json={
            "conversation_id": run["conversation_id"], "message": "继续",
        }).json()
        assert _wait_run(client, next_run["run_id"], owner)["status"] == "completed"
        events = _iter_v2_sse(client.get(next_run["events_url"], headers=headers))
        assert {item["path"] for item in events[-1]["data"]["summary"]["verification"]["artifacts"]} == {"report.md", "facts.json"}

    def test_main_parallel_duplicate_plan_calls_keep_other_tasks_and_do_not_consume_the_budget(self, client, user_setup, monkeypatch):
        from app.core.config import settings

        def item(content, status):
            return {"content": content, "status": status}

        def call(todos, identifier):
            return {"name": "write_todos", "id": identifier, "args": {"todos": todos}}

        monkeypatch.setattr(settings, "AGENT_MAX_TOOL_CALLS", 4)
        first = [item("核对库存", "in_progress"), item("核对出入库", "pending"), item("整理结论", "pending")]
        bulk = [call(json.dumps([item("核对库存", "completed")], ensure_ascii=False), f"repeated-plan-{index}") for index in range(40)]
        bulk.append(call(json.dumps([item("核对出入库", "completed")], ensure_ascii=False), "another-item"))
        bulk.extend(call(json.dumps([item("整理结论", "completed")], ensure_ascii=False)[:-1], f"malformed-{index}") for index in range(40))
        done = [{**entry, "status": "completed"} for entry in first]
        _patch_model(monkeypatch, [
            AIMessage(content="", tool_calls=[call(first, "initial-plan")]),
            AIMessage(content="", tool_calls=bulk),
            AIMessage(content="完成。"),
            AIMessage(content="", tool_calls=[call(done, "confirmed-plan")]),
            AIMessage(content="已完成盘点。"),
        ])
        owner = user_setup["user1_id"]
        headers = _auth(owner)
        response = client.post("/api/v2/runs", headers=headers, json={"message": "按多个阶段盘点并核对库存和出入库，最后整理结论。"})
        assert response.status_code == 200
        run = response.json()
        assert _wait_run(client, run["run_id"], owner)["status"] == "completed"
        events = _iter_v2_sse(client.get(run["events_url"], headers=headers))
        actual = [event["data"]["tool_call_id"] for event in events if event["event"] == "tool.started"]
        assert actual == ["initial-plan", "repeated-plan-0", "malformed-0", "confirmed-plan"]
        assert any(event["event"] == "tool.failed" and event["data"]["tool_call_id"] == "malformed-0" for event in events)
        assert any(event["event"] == "run.retrying" and any(check["id"] == "plan_finished" for check in event["data"]["failed_checks"])
                   for event in events)

    @pytest.mark.parametrize("reason", ["budget", "no_progress", "execution"])
    def test_interrupted_complex_task_preserves_confirmed_files(self, client, user_setup, monkeypatch, tmp_path, reason):
        from app.core.config import settings

        def failure():
            raise RuntimeError("模型连接中断")

        monkeypatch.setattr(settings, "AGENT_FILES_ROOT", str(tmp_path))
        monkeypatch.setattr(settings, "AGENT_MAX_MODEL_CALLS", 2 if reason == "budget" else 10)
        excerpt = "已生成报告草稿，事实表尚待保存。"
        pending_todos = [{"content": "核对报告正文", "status": "completed"},
                         {"content": "写入并核对事实清单", "status": "in_progress"}]
        repeated = [AIMessage(content="", tool_calls=[{"name": "read_file", "id": f"repeated-{index}", "args": {
            "file_path": "/workspace/report.md",
        }}]) for index in range(3)] if reason == "no_progress" else []
        _patch_model(monkeypatch, [
            AIMessage(content="", tool_calls=[{"name": "write_todos", "id": "initial-plan", "args": {"todos": pending_todos}}]),
            AIMessage(content=excerpt, tool_calls=[{"name": "write_file", "id": "draft-before-interruption", "args": {
                "file_path": "/workspace/report.md", "content": "# 报告\n\n第一部分已核对。"}}]),
            *repeated,
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
        expected = "failed" if reason == "execution" else "limit_exceeded"
        detail = _wait_run(client, run["run_id"], owner)
        assert detail["status"] == expected
        events = _iter_v2_sse(client.get(run["events_url"], headers=headers))
        assert events[-1]["event"] == "run.failed"
        terminal = events[-1]["data"]
        assert terminal["status"] == expected and terminal["final_text"] == excerpt
        if expected == "limit_exceeded":
            assert terminal["summary"]["stop"] == {"reason": reason, "can_continue": True}
            assert detail["summary"]["stop"] == terminal["summary"]["stop"]
            assert "暂停" in terminal["error"]
        else:
            assert "stop" not in terminal["summary"]
        verification = terminal["summary"]["verification"]
        assert verification["stop_reason"] == reason and verification["status"] != "passed"
        assert {item["id"] for item in verification["checks"] if not item["passed"]} == {
            "artifact_produced", "execution_interrupted",
        }
        assert [item["path"] for item in verification["artifacts"]] == ["report.md"]
        downloaded = client.get(verification["artifacts"][0]["download_url"], headers=headers)
        assert downloaded.status_code == 200 and "第一部分已核对" in downloaded.text
        if reason == "budget":
            monkeypatch.setattr(settings, "AGENT_MAX_MODEL_CALLS", 10)

        received = []
        model_generate = FakeChatModel._generate

        def observe_continuation(self, messages, *args, **kwargs):
            received.append(messages)
            return model_generate(self, messages, *args, **kwargs)

        monkeypatch.setattr(FakeChatModel, "_generate", observe_continuation)
        _patch_model(monkeypatch, [
            AIMessage(content="", tool_calls=[{"name": "write_file", "id": "write-missing-facts", "args": {
                "file_path": "/workspace/facts.json", "content": '{"total": 2}'}}]),
            AIMessage(content="两份文件已齐备，事实表已补齐。"),
            AIMessage(content="", tool_calls=[{"name": "write_todos", "id": "close-carried-plan", "args": {
                "todos": [{**item, "status": "completed"} for item in pending_todos]}}]),
            AIMessage(content="已完成原计划，两份文件已齐备。"),
        ])
        continued = client.post("/api/v2/runs", headers=headers, json={
            "conversation_id": run["conversation_id"], "message": "继续",
        })
        assert continued.status_code == 200, continued.text
        next_run = continued.json()
        assert _wait_run(client, next_run["run_id"], owner)["status"] == "completed"
        history = _iter_v2_sse(client.get(next_run["events_url"], headers=headers))
        started_tools = [event["data"] for event in history if event["event"] == "tool.started"]
        assert [item["tool_call_id"] for item in started_tools] == ["write-missing-facts", "close-carried-plan"]
        header = "\n".join(str(item.content) for item in received[0] if item.type == "system")
        assert '"verified_files": ["report.md"]' in header
        assert '"content": "写入并核对事实清单", "status": "in_progress"' in header
        assert any(event["event"] == "run.retrying" and any(check["id"] == "plan_finished"
                   for check in event["data"].get("failed_checks", [])) for event in history)
        report = history[-1]["data"]["summary"]["verification"]
        assert report["status"] == "passed"
        files = {item["path"]: item["download_url"] for item in report["artifacts"]}
        assert set(files) == {"report.md", "facts.json"}
        assert client.get(files["report.md"], headers=headers).content == downloaded.content
        assert client.get(files["facts.json"], headers=headers).json() == {"total": 2}

    def test_project_continue_rebuilds_plan_after_model_clears_todos(self, client, user_setup, monkeypatch, tmp_path):
        from app.core.config import settings
        from app.services.agent_runtime import workspace_delivery
        from app.services.agent_runtime.project_verifier_protocol import source_digest

        async def verifier(files, scope, *, capture_browser_errors=False):
            assert scope == "fullstack" and capture_browser_errors
            return {"source_digest": source_digest(files), "checks": [
                *({"id": key, "status": "passed"} for key in ("runtime.start", "runtime.web", "runtime.browser")),
                {"id": "runtime.browser.flow.1", "status": "failed", "detail": "提交工单后缺少列表结果 #new-row"},
            ]}

        def call(name, args, identifier):
            return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": identifier}])

        monkeypatch.setattr(settings, "AGENT_FILES_ROOT", str(tmp_path))
        monkeypatch.setattr(settings, "AGENT_PROJECT_VERIFIER_DIR", str(tmp_path / "worker"))
        monkeypatch.setattr(workspace_delivery, "verify_runtime", verifier)
        script = [
            call("write_file", {"file_path": "/workspace/frontend/index.html", "content": "<button>登记</button>"}, "saved-page"),
            call("write_file", {"file_path": "/workspace/backend/main.py", "content": "from fastapi import FastAPI\napp = FastAPI()"}, "saved-api"),
            call("write_file", {"file_path": "/workspace/backend", "content": "不可覆盖目录"}, "failed-write"),
            call("verify_workspace_project", {}, "verified-browser-flow"),
            call("write_todos", {"todos": []}, "cleared-todos"),
        ]
        monkeypatch.setattr(settings, "AGENT_MAX_MODEL_CALLS", len(script))
        _patch_model(monkeypatch, script)
        owner = user_setup["user1_id"]
        headers = _auth(owner)
        initial = client.post("/api/v2/runs", headers=headers, json={"task_mode": "chat", "message":
            "构建一个前后端分离的可运行项目，请用浏览器验证登记工单并提供源码 ZIP。"})
        assert initial.status_code == 200, initial.text
        first = initial.json()
        assert _wait_run(client, first["run_id"], owner)["status"] == "limit_exceeded"
        initial_events = _iter_v2_sse(client.get(first["events_url"], headers=headers))
        assert any(event["event"] == "tool.completed" and event["data"].get("tool_call_id") == "cleared-todos" for event in initial_events)

        monkeypatch.setattr(settings, "AGENT_MAX_MODEL_CALLS", 1)
        _patch_model(monkeypatch, [AIMessage(content="工单项目已完成。")])
        resumed = client.post("/api/v2/runs", headers=headers, json={"conversation_id": first["conversation_id"], "message": "继续"})
        assert resumed.status_code == 200, resumed.text
        run = resumed.json()
        assert _wait_run(client, run["run_id"], owner)["status"] != "completed"
        events = _iter_v2_sse(client.get(run["events_url"], headers=headers))
        planned = next(event["data"]["todos"] for event in events if event["event"] == "plan.updated")
        assert all(row["status"] == "pending" for row in planned)
        assert any("#new-row" in row["content"] for row in planned)
        assert any("核对未确认的写入 backend" in row["content"] for row in planned)
        report = events[-1]["data"]["summary"]["verification"]
        assert {item["path"] for item in report["artifacts"]} == {"frontend/index.html", "backend/main.py"}

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


# ── 联网检索集成（SearXNG 集成 + web_enabled 开关）────────────────────────────


class TestWebRetrievalToggle:
    def _assistant(self, client, headers, name):
        resp = client.post("/api/v2/assistants", headers=headers, json={
            "name": name, "capability_ids": ["general", "web_research"],
        })
        assert resp.status_code in (200, 201), resp.text
        return resp.json()

    def test_web_search_sources_flow_through_events(self, client, user_setup, monkeypatch):
        """web_search 结构化来源随 tool.completed 事件透传到 v2 事件流。"""
        import app.services.tools as tools_pkg
        from app.services.tools.result import ToolResult

        async def fake_web_search(params):
            return ToolResult.succeeded(
                "搜索结果:\n1. **来源A**\n   🔗 [来源A](https://example.com/a)\n",
                metadata={"sources": [{"id": 1, "title": "来源A", "url": "https://example.com/a", "desc": "摘要"}],
                          "engine": "SearXNG"},
            )

        monkeypatch.setitem(tools_pkg._ASYNC_HANDLERS, "web_search", fake_web_search)

        def call(name, args, identifier):
            return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": identifier}])

        _patch_model(monkeypatch, [
            call("web_search", {"query": "测试检索"}, "s1"),
            AIMessage(content="根据 [来源A](https://example.com/a) 的报道，结论成立。"),
        ])
        owner = user_setup["user1_id"]
        headers = _auth(owner)
        assistant = self._assistant(client, headers, "联网检索冒烟")
        started = client.post("/api/v2/runs", headers=headers, json={
            "message": "联网调研一下测试检索的情况，引用来源。", "assistant_id": assistant["id"],
            "web_enabled": True, "use_memory": False,
        }).json()
        assert _wait_run(client, started["run_id"], owner)["status"] == "completed"
        events = _iter_v2_sse(client.get(started["events_url"], headers=headers))
        completed = [e["data"] for e in events
                     if e["event"] == "tool.completed" and e["data"]["tool_name"] == "web_search"]
        assert completed, "web_search 应执行并产生 tool.completed"
        assert completed[0]["sources"][0]["url"] == "https://example.com/a"

    def test_web_disabled_blocks_web_search_and_snapshots_toggle(self, client, user_setup, monkeypatch):
        """web_enabled=false：网络检索工具 fail-closed 收窄，且开关落入 Run 配置快照。"""
        import os

        import app.services.tools as tools_pkg
        from sqlalchemy import text

        called = []

        async def fake_web_search(params):
            called.append(params)
            return "不应执行"

        monkeypatch.setitem(tools_pkg._ASYNC_HANDLERS, "web_search", fake_web_search)

        def call(name, args, identifier):
            return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": identifier}])

        _patch_model(monkeypatch, [
            call("web_search", {"query": "不该执行"}, "s2"),
            AIMessage(content="未联网，直接作答。"),
        ])
        owner = user_setup["user1_id"]
        headers = _auth(owner)
        assistant = self._assistant(client, headers, "联网关闭冒烟")
        started = client.post("/api/v2/runs", headers=headers, json={
            "message": "随便问个问题。", "assistant_id": assistant["id"],
            "web_enabled": False, "use_memory": False,
        }).json()
        # 状态不作断言：越权工具调用被内核拒绝后自行收尾（failed 亦可）
        _wait_run(client, started["run_id"], owner)
        assert called == [], "web_enabled=false 时 web_search 不得执行（fail-closed）"
        engine = create_engine(os.environ["DATABASE_SYNC_URL"])
        with Session(engine) as s:
            snapshot = s.execute(
                text("SELECT agent_config_snapshot FROM agent_runs WHERE id = :id"),
                {"id": started["run_id"]},
            ).scalar_one()
        if isinstance(snapshot, str):
            snapshot = json.loads(snapshot)
        assert snapshot.get("web_enabled") is False


class TestDecisionClarifyShortCircuit:
    def test_ambiguous_decision_ends_run_without_tools(self, client, user_setup, monkeypatch):
        """关键歧义由服务端直接结束本轮：只输出澄清问题，不执行任何工具。

        模型拥有语义判断权，但不能靠“自律”绕过澄清去检索（否则会从错误起点开始）。
        """
        import app.services.tools as tools_pkg
        from app.core.config import settings
        from app.services.agent_runtime.decision import orchestrator
        from app.services.agent_runtime.decision import service as decision_service
        from app.services.agent_runtime.decision.schemas import RunDecision

        question = "请问这里的 RSI 指哪个领域：金融相对强弱指数，还是 AI 的递归自我改进？"

        async def fake_decide(*_args, **_kwargs):
            return decision_service.DecisionOutcome(
                decision=RunDecision(
                    intent="research", route="clarify", continuity="new_task",
                    decision_status="ambiguous", clarification_question=question,
                    goal="确认 RSI 的领域", rationale="缩写存在多义",
                ),
                latency_ms=1, fallback=False, model_id="fake",
            )

        _patch_model(monkeypatch, [])
        monkeypatch.setattr(orchestrator, "decide", fake_decide)
        monkeypatch.setattr(settings, "AGENT_DECISION_MODE", "llm")
        monkeypatch.setattr(settings, "AGENT_ALLOW_NETWORK", True)

        called = []

        async def fake_web_search(params):
            called.append(params)
            return "不应执行"

        monkeypatch.setitem(tools_pkg._ASYNC_HANDLERS, "web_search", fake_web_search)

        owner = user_setup["user1_id"]
        headers = _auth(owner)
        assistant = client.post("/api/v2/assistants", headers=headers, json={
            "name": "歧义澄清短路", "capability_ids": ["general"],
        }).json()
        started = client.post("/api/v2/runs", headers=headers, json={
            "message": "你帮我查查当前RSI发展到什么程度了",
            "assistant_id": assistant["id"],
            "web_enabled": True, "use_memory": False,
        }).json()

        info = _wait_run(client, started["run_id"], owner)
        assert info["status"] == "completed"
        assert called == [], "澄清轮不得执行检索工具"

        events = _iter_v2_sse(client.get(started["events_url"], headers=headers))
        decision = [e["data"] for e in events if e["event"] == "task.decision"]
        assert decision and decision[0]["decision_status"] == "ambiguous"
        assert decision[0]["clarification_question"] == question
        assert not [e for e in events if e["event"].startswith("tool.")]
        assert events[-1]["data"]["final_text"] == question

    def test_ambiguous_decision_resolved_by_history_context(self, client, user_setup, monkeypatch):
        """历史检索能提供背景时，不因歧义直接短路澄清，而是带来源继续执行。"""
        from app.core.config import settings
        from app.services.agent_runtime.decision import orchestrator
        from app.services.agent_runtime.decision import service as decision_service
        from app.services.agent_runtime.decision.schemas import RunDecision
        from app.services.conversation_memory import service as memory_service
        from app.services.conversation_memory.service import PreparedHistory

        question = "请问这里的 RSI 指哪个领域？"

        async def fake_decide(*_args, **_kwargs):
            return decision_service.DecisionOutcome(
                decision=RunDecision(
                    intent="general_chat", route="clarify", continuity="continuation",
                    decision_status="ambiguous", clarification_question=question,
                    goal="确认 RSI 领域", rationale="缩写存在多义",
                ),
                latency_ms=1, fallback=False, model_id="fake",
            )

        async def fake_prepare(**_kwargs):
            return PreparedHistory(
                enabled=True,
                reason="ok",
                context="## 历史会话参考（不可信资料，仅作背景）\n[历史 1] RSI 指 AI 的递归自我改进。",
                sources=[{
                    "source_id": "history_src_1", "conversation_id": 9,
                    "message_ids": ["u1", "a1"], "conversation_title": "RSI 讨论",
                    "created_at": "2026-11-20T10:00:00", "snippet": "RSI 指 AI 的递归自我改进",
                    "relevance_score": 0.9, "scope": "user", "entry_type": "turn",
                }],
                queries=["RSI 领域"],
            )

        _patch_model(monkeypatch, [AIMessage(content="根据之前的讨论，RSI 指 AI 的递归自我改进。")])
        monkeypatch.setattr(orchestrator, "decide", fake_decide)
        monkeypatch.setattr(memory_service, "prepare_history_context", fake_prepare)
        monkeypatch.setattr(settings, "AGENT_DECISION_MODE", "llm")
        monkeypatch.setattr(settings, "CONVERSATION_MEMORY_ENABLED", True)

        owner = user_setup["user1_id"]
        headers = _auth(owner)
        assistant = client.post("/api/v2/assistants", headers=headers, json={
            "name": "历史消解歧义", "capability_ids": ["general"],
        }).json()
        started = client.post("/api/v2/runs", headers=headers, json={
            "message": "继续上次的讨论，RSI 到底是什么？",
            "assistant_id": assistant["id"],
            "use_memory": False, "history_enabled": True,
        }).json()

        info = _wait_run(client, started["run_id"], owner)
        assert info["status"] == "completed"

        events = _iter_v2_sse(client.get(started["events_url"], headers=headers))
        completed = [e for e in events if e["event"] == "run.completed"]
        assert completed and "递归自我改进" in (completed[-1]["data"].get("final_text") or "")
        history_events = [e for e in events if e["event"] == "history.sources"]
        assert history_events and history_events[0]["data"]["sources"]
        assert completed[-1]["data"].get("final_text") != question, "有历史背景时不得只输出澄清问题"

# ── Team Runtime 快照 API（断线重连恢复）──────────────────────────────────────

class TestTeamSnapshotAPI:
    def test_team_snapshot_owner_only_and_board_facts(self, client, user_setup):
        import os
        import uuid

        from sqlalchemy import delete

        from app.models.agent import Agent, AgentRun
        from app.models.team import TeamRun, TeamTask

        engine = create_engine(os.environ["DATABASE_SYNC_URL"])
        run_id = uuid.uuid4().hex
        with Session(engine) as s:
            agent = Agent(name=f"team-snap-{run_id[:6]}", owner_id=user_setup["user1_id"])
            s.add(agent)
            s.flush()
            agent_id = agent.id
            s.add(AgentRun(
                id=run_id, agent_id=agent_id, user_id=user_setup["user1_id"],
                thread_id=f"team:{run_id}", status="running",
            ))
            s.add(TeamRun(
                team_run_id=run_id, objective="快照测试", status="running",
                phase="working", config_snapshot={},
            ))
            s.add(TeamTask(
                team_run_id=run_id, task_id="facts", subject="核对事实",
                description="交付事实", role="事实核对", status="completed",
                revision=2, write_scopes=[],
            ))
            s.commit()
        try:
            other = client.get(f"/api/v2/runs/{run_id}/team", headers=_auth(user_setup["user2_id"]))
            assert other.status_code == 404
            resp = client.get(f"/api/v2/runs/{run_id}/team", headers=_auth(user_setup["user1_id"]))
            assert resp.status_code == 200, resp.text
            data = resp.json()
            assert data["team"]["team_run_id"] == run_id
            assert data["team"]["phase"] == "working"
            assert data["tasks"][0]["id"] == "facts"
            assert data["tasks"][0]["status"] == "completed"
        finally:
            with Session(engine) as s:
                s.execute(delete(TeamTask).where(TeamTask.team_run_id == run_id))
                s.execute(delete(TeamRun).where(TeamRun.team_run_id == run_id))
                s.execute(delete(AgentRun).where(AgentRun.id == run_id))
                s.execute(delete(Agent).where(Agent.id == agent_id))
                s.commit()
