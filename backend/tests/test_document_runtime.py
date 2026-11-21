"""任务路线与等待输入的 Run、API、Worker 和 Checkpoint 验收。"""
from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, TypedDict

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.deps import get_current_user
from app.api.v2 import assistants, conversations, runs
from app.core.config import settings
from app.core.database import Base, get_db
from app.models.agent import Agent, AgentRun, AgentSession
from app.models.database_connection import DatabaseConnection
from app.models.document import RunInputRequest
from app.models.knowledge import KnowledgeBase
from app.models.user import User
from app.services.agent_runtime import document_graph, document_retry, run_input, service, worker
from app.services.agent_runtime.tools import adapter
from app.services.agent_runtime.context import RunScope, RuntimeContext
from app.services.agent_runtime.lifecycle import RunHandle, RunRegistry
from app.services.agent_runtime.persistence import CheckpointerFactory
from app.services.agent_runtime.document_turn import document_turn
from app.services.agent_runtime.task_router import resolve_attachment_refs, route_task
from app.services.agent_runtime.tools.policy import ToolPolicy
from app.services.documents.errors import StructuredDocumentError
from app.services.mcp.registry import register_mcp_tool_risks


class _State(TypedDict, total=False):
    messages: list[dict[str, Any]]
    execution_context: dict[str, Any]
    owner: str
    final_text: str


def _input(ordinal: int, key: str, **field_schema) -> dict[str, Any]:
    return {
        "kind": "document_input", "ordinal": ordinal, "required_fields": [key],
        "prompt": "请补充项目信息",
        "input_schema": {"type": "object", "properties": {key: field_schema}},
    }


def _graph(checkpointer, context):
    async def owner(_state):
        await context.step("collect_owner")
        values = interrupt(_input(1, "owner", type="string", minLength=1))
        return {"owner": values["owner"]}

    async def finish(state):
        await context.step("collect_date")
        values = interrupt(_input(2, "due", type="string", format="date"))
        return {"final_text": f"{state['owner']}，截止 {values['due']}"}

    graph = StateGraph(_State)
    graph.add_node("owner", owner)
    graph.add_node("finish", finish)
    graph.add_edge(START, "owner")
    graph.add_edge("owner", "finish")
    graph.add_edge("finish", END)
    return graph.compile(checkpointer=checkpointer)


@pytest_asyncio.fixture
async def platform(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'runs.db'}")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    for module in (service, worker, run_input, document_graph, document_retry, adapter):
        monkeypatch.setattr(module, "AsyncSessionLocal", sessions)
    import app.core.database as database

    # EventBus 和持久化回放在运行时从数据库模块获取 session factory。
    monkeypatch.setattr(database, "AsyncSessionLocal", sessions)
    monkeypatch.setattr(settings, "AGENT_CHECKPOINT_DSN", f"sqlite:///{tmp_path / 'checkpoints.db'}")
    monkeypatch.setattr(settings, "AGENT_FILES_ROOT", str(tmp_path / "files"))
    monkeypatch.setitem(
        sys.modules, "app.services.documents.graphs", SimpleNamespace(build_document_graph=_graph)
    )
    monkeypatch.setattr(document_graph, "build_chat_model", lambda *args, **kwargs: None)

    async def load_model(*args):
        return {}, {"name": "document-test", "supports_function_call": True}

    monkeypatch.setattr(service.AgentRuntimeService, "_load_model", load_model)
    async with sessions() as db:
        db.add_all([
            User(id=11, username="owner", email="owner@test.local", hashed_password="x"),
            User(id=12, username="other", email="other@test.local", hashed_password="x"),
            Agent(id=21, name="文档任务助手", owner_id=11, is_active=True),
            AgentSession(id=31, user_id=11, agent_id=21, messages=[]),
        ])
        await db.commit()

    app = FastAPI()
    app.include_router(runs.router, prefix="/api/v2")
    app.include_router(conversations.router, prefix="/api/v2")
    app.include_router(assistants.router, prefix="/api/v2")
    active_user = SimpleNamespace(id=11, is_superuser=False)

    async def owner():
        return active_user

    async def get_session():
        async with sessions() as db:
            yield db

    app.dependency_overrides[get_current_user] = owner
    app.dependency_overrides[get_db] = get_session

    runtimes = []

    def new_service():
        runtime = service.AgentRuntimeService()
        runtime.registry = RunRegistry()
        runtime.worker.poll_interval = 0.03
        runtimes.append(runtime)
        monkeypatch.setattr(service, "_service", runtime)
        return runtime

    # 测试单独管理持久化连接，模拟进程退出并从同一个 SQLite 文件恢复。
    monkeypatch.setattr(CheckpointerFactory, "_instance", CheckpointerFactory())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        try:
            yield client, new_service, active_user, sessions
        finally:
            for runtime in runtimes:
                await runtime.worker.stop()
            await CheckpointerFactory.get().aclose()
            await engine.dispose()


async def _events(client, url, after_seq=None):
    headers = {"Last-Event-ID": str(after_seq)} if after_seq is not None else {}
    async with asyncio.timeout(6):
        response = await client.get(f"{url}/events", headers=headers)
    assert response.status_code == 200, response.text
    return [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]


@pytest.mark.asyncio
async def test_input_pause_reload_restart_and_resume_same_run(platform):
    client, new_service, user, sessions = platform
    first = new_service()
    body = {"message": "生成正式项目报告", "conversation_id": 31, "task_mode": "document_generate"}
    created = await client.post("/api/v2/runs", json=body, headers={"Idempotency-Key": "report"})
    assert created.status_code == 200, created.text
    run_id = created.json()["run_id"]
    url = f"/api/v2/runs/{run_id}"
    first_events = await _events(client, url)
    assert [event["event"] for event in first_events[:2]] == ["run.started", "task.routed"]
    assert first_events[-1]["data"]["status"] == "waiting_input"
    old_sequence = first_events[-1]["sequence"]
    waiting = (await client.get(url)).json()
    first_request = waiting["pending_input"]
    assert first_events[-2]["data"] == first_request
    assert (await client.get("/api/v2/conversations/31")).json()["active_run_id"] == run_id
    assert (await client.get("/api/v2/conversations/31/messages")).json()["items"][0]["document_run_id"] == run_id
    assert (await client.post(url + "/resume", json={})).status_code == 409
    assert (await client.post("/api/v2/runs", json={"message": "测试会话占用", "conversation_id": 31})).status_code == 409

    reused = await client.post("/api/v2/runs", json=body, headers={"Idempotency-Key": "report"})
    assert reused.json()["idempotent_reuse"] and reused.json()["run_id"] == run_id
    changed = await client.post(
        "/api/v2/runs", json={**body, "task_mode": "document_analyze"},
        headers={"Idempotency-Key": "report"},
    )
    assert changed.status_code == 409

    await first.worker.stop()
    await CheckpointerFactory.get().aclose()
    CheckpointerFactory._instance = CheckpointerFactory()
    second = new_service()
    first_input = {"request_id": first_request["request_id"], "values": {"owner": "陈经理"}}
    user.id = 12
    assert (await client.post(url + "/input", json=first_input)).status_code == 404
    user.id = 11
    invalid = {**first_input, "values": {"owner": 7}}
    assert (await client.post(url + "/input", json=invalid)).status_code == 422
    assert (await client.get(url)).json()["status"] == "waiting_input"

    accepted = await client.post(url + "/input", json=first_input)
    assert accepted.status_code == 200 and accepted.json()["status"] == "running"
    assert (await client.post(url + "/input", json=first_input)).json()["idempotent"]
    assert (await client.post(url + "/input", json={**first_input, "values": {"owner": "其他人"}})).status_code == 409
    # 无 Worker 时提交已持久化，启动新的 Worker 后在原 Checkpoint 续跑。
    async with sessions() as db:
        stored = await db.get(RunInputRequest, first_request["request_id"])
        assert stored.status == "submitted" and stored.response == first_input["values"]
    await second.worker.start()
    second_events = await _events(client, url, old_sequence)
    assert second_events[0]["event"] == "input.received"
    assert second_events[-1]["data"]["status"] == "waiting_input"
    assert second_events[-1]["sequence"] > old_sequence
    second_request = second_events[-2]["data"]["request_id"]
    assert second_request != first_request["request_id"]
    assert (await client.post(
        url + "/input", json={"request_id": second_request, "values": {"due": "2026-02-30"}},
    )).status_code == 422
    assert (await client.post(
        url + "/input", json={"request_id": second_request, "values": {"due": "2026-11-30"}},
    )).status_code == 200

    final_events = await _events(client, url)
    assert final_events[-1]["event"] == "run.completed"
    assert final_events[-1]["data"]["final_text"] == "陈经理，截止 2026-11-30"
    assert [event["sequence"] for event in final_events] == sorted(
        {event["sequence"] for event in final_events}
    )
    paused = [event["data"] for event in final_events if event["data"].get("status") == "waiting_input"]
    assert len(paused) == 2 and all(data["historical"] for data in paused)
    finished = (await client.get(url)).json()
    assert finished["status"] == "completed" and finished["thread_id"] == waiting["thread_id"]
    async with sessions() as db:
        saved = await db.get(AgentRun, run_id)
        assert saved.attempt == 3 and saved.summary["input_request_count"] == 2


@pytest.mark.asyncio
async def test_failed_document_continues_from_saved_chapter_in_same_run(platform, monkeypatch):
    client, new_service, user, sessions = platform
    calls = {"saved": 0, "failed": 0}

    def graph_factory(checkpointer, context):
        async def first_chapter(_state):
            values = interrupt(_input(1, "owner", type="string"))
            calls["saved"] += 1
            await context.emit("document.section.completed", {"title": "第一章", "completed": 1, "total": 2})
            return {"owner": values["owner"]}

        async def second_chapter(state):
            calls["failed"] += 1
            if calls["failed"] == 1:
                raise StructuredDocumentError("invalid", stage="第二章")
            return {"final_text": f"{state['owner']}的两章报告已完成"}

        graph = StateGraph(_State)
        graph.add_node("first_chapter", first_chapter)
        graph.add_node("second_chapter", second_chapter)
        graph.add_edge(START, "first_chapter")
        graph.add_edge("first_chapter", "second_chapter")
        graph.add_edge("second_chapter", END)
        return graph.compile(checkpointer=checkpointer)

    monkeypatch.setattr(sys.modules["app.services.documents.graphs"], "build_document_graph", graph_factory)
    first = new_service()
    await first.worker.start()
    created = await client.post("/api/v2/runs", json={"message": "生成项目报告", "conversation_id": 31})
    run_id = created.json()["run_id"]
    url = f"/api/v2/runs/{run_id}"
    waiting = (await _events(client, url))[-1]
    request = (await client.get(url)).json()["pending_input"]
    received = await client.post(url + "/input", json={"request_id": request["request_id"], "values": {"owner": "陈经理"}})
    assert received.status_code == 200, received.text
    failed = (await _events(client, url, waiting["sequence"]))[-1]
    assert failed["event"] == "run.failed" and failed["data"]["document_retryable"]
    status = (await client.get(url)).json()
    assert status["document_retryable"] is True
    old_history = (await client.get("/api/v2/conversations/31/messages")).json()["items"]
    old_reply = next(item for item in old_history if item["role"] == "assistant")
    async with sessions() as db:
        old_deadline = (await db.get(AgentRun, run_id)).budget_deadline_at

    await first.worker.stop()
    await CheckpointerFactory.get().aclose()
    CheckpointerFactory._instance = CheckpointerFactory()
    second = new_service()
    user.id = 12
    assert (await client.post(url + "/retry")).status_code == 404
    user.id = 11
    assert (await client.post(url + "/retry", headers={"X-Document-Failure-Seq": "1"})).status_code == 409
    header = {"X-Document-Failure-Seq": str(failed["sequence"])}
    resumed = await client.post(url + "/retry", headers=header)
    assert resumed.status_code == 200, resumed.text
    assert resumed.json()["run_id"] == run_id and resumed.json()["after_seq"] == failed["sequence"]
    assert (await client.post(url + "/retry", headers=header)).json()["after_seq"] == failed["sequence"]
    await second.worker.start()
    events = await _events(client, url, failed["sequence"])
    assert events[0]["event"] == "run.retrying" and events[0]["data"]["manual"]
    assert events[-1]["event"] == "run.completed"
    assert calls == {"saved": 1, "failed": 2}

    history = await _events(client, url)
    assert len([event for event in history if event["event"] == "document.section.completed"]) == 1
    assert next(event for event in history if event["event"] == "run.failed")["data"]["historical"]
    async with sessions() as db:
        row = await db.get(AgentRun, run_id)
        assert row.thread_id == status["thread_id"] and row.budget_deadline_at > old_deadline
    saved_history = (await client.get("/api/v2/conversations/31/messages")).json()["items"]
    replies = [item for item in saved_history if item["role"] == "assistant"]
    assert len(replies) == 1 and replies[0]["message_id"] == old_reply["message_id"]
    assert replies[0]["content"] == "陈经理的两章报告已完成"


def test_document_turn_uses_phm_objective_and_exports_the_existing_draft():
    original = "根据这份文档内容以这个作为模板帮我生成针对PHM软件的用户手册文档"
    template = {"path": "uploads/工业软件用户手册.docx"}
    draft = "# PHM 软件用户手册\n\n## 健康数据管理\n\n" + "使用健康数据管理功能可以查看设备状态。" * 16
    draft += "\n\n## 报警\n\n|等级|含义|\n|---|---|\n|高|需要检查|"
    history = [
        {"role": "user", "content": original, "attachments": [template], "document_run_id": "source-run"},
        {"role": "assistant", "content": "请补充资料。"},
        {"role": "user", "content": "设备接入支持 OPC UA，运行环境是 Linux"},
        {"role": "user", "content": "这些信息本次你自己发挥，我主要是用来测试"},
        {"role": "assistant", "content": "以下是手册草稿。\n\n" + draft},
        {"role": "user", "content": "在界面中给到我能直接下载的文档"},
        {"role": "assistant", "content": "已生成下载文档。"},
    ]
    for message in ("继续", "说中文", "这些信息本次你自己发挥，我主要是用来测试"):
        route, refs, run = document_turn(message, history, route_task(message))
        assert route["task_type"] == "document_generate"
        assert route["document_context"]["example_mode"] is True
        assert route["document_context"]["export_existing"] is False
        assert original in route["document_context"]["request"] and refs == [template] and run == "source-run"
        assert "设备接入支持 OPC UA，运行环境是 Linux" in route["document_context"]["request"]

    for message in ("在界面中给到我能直接下载的文档", "把之前的 PHM 用户手册直接导出为 Word",
                    "我是说将你之前生成的文档内容或者构思好的PHM内容写到word文档中，我要下载"):
        route, _, _ = document_turn(message, history, route_task(message))
        assert route["document_context"]["draft"] == draft
        assert route["document_context"]["export_existing"] is True

    new_document = "生成一份新的考勤报告"
    route, refs, run = document_turn(new_document, history, route_task(new_document))
    assert route["document_context"]["request"] == new_document and refs == [] and run is None
    new_topic = [*history, {"role": "user", "content": "你用的什么模型？"}]
    assert document_turn("继续", new_topic, route_task("继续"))[0]["task_type"] == "general_chat"
    assert document_turn("继续", history, route_task("继续", task_mode="chat"))[0]["task_type"] == "general_chat"


@pytest.mark.asyncio
async def test_cancel_waiting_input_after_worker_exit(platform):
    client, new_service, _user, sessions = platform
    runtime = new_service()
    created = await client.post("/api/v2/runs", json={
        "message": "生成项目报告", "conversation_id": 31, "task_mode": "document_generate",
    })
    run_id = created.json()["run_id"]
    url = f"/api/v2/runs/{run_id}"
    events = await _events(client, url)
    request_id = events[-2]["data"]["request_id"]
    await runtime.worker.stop()
    new_service()

    cancelled = await client.post(url + "/cancel", json={})
    assert cancelled.json()["status"] == "cancelled"
    result = await client.post(url + "/input", json={"request_id": request_id, "values": {"owner": "无效"}})
    assert result.status_code == 409
    assert (await _events(client, url))[-1]["event"] == "run.cancelled"
    async with sessions() as db:
        assert (await db.get(RunInputRequest, request_id)).status == "cancelled"
        assert (await db.get(AgentSession, 31)).active_run_id is None


@pytest.mark.asyncio
async def test_v2_run_reads_only_owned_external_database(platform, tmp_path, monkeypatch):
    client, new_service, _user, sessions = platform
    new_service()
    monkeypatch.setattr(settings, "AGENT_FILES_ROOT", str(tmp_path))
    owner_root = tmp_path / "11"
    owner_root.mkdir()
    path = owner_root / "external.db"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE evidence (value TEXT)")
        db.execute("INSERT INTO evidence VALUES ('approved-external-evidence')")

    async with sessions() as db:
        assistant = await db.get(Agent, 21)
        assistant.database_connection_id = 41
        assistant.tools = [{"name": "db_query", "enabled": True}]
        db.add_all([
            DatabaseConnection(id=41, name="外部项目库", db_type="sqlite", database=str(path), owner_id=11),
            DatabaseConnection(id=42, name="他人数据库", db_type="sqlite", database=str(path), owner_id=12),
            KnowledgeBase(id=43, name="他人知识库", owner_id=12),
        ])
        await db.commit()

    async def collect(_, context):
        await context.step("read_bound_source")
        output = await context.invoke_tool(
            "db_query", {"sql": "SELECT value FROM evidence"}, "fetch-evidence"
        )
        return {"final_text": output}

    def build(checkpointer, context):
        async def node(state):
            return await collect(state, context)

        graph = StateGraph(_State)
        graph.add_node("collect", node)
        graph.add_edge(START, "collect")
        graph.add_edge("collect", END)
        return graph.compile(checkpointer=checkpointer)

    monkeypatch.setitem(
        sys.modules, "app.services.documents.graphs", SimpleNamespace(build_document_graph=build)
    )
    for key, other in (("database_connection_id", 42), ("knowledge_base_id", 43)):
        response = await client.post("/api/v2/assistants", json={"name": "无权限的绑定", key: other})
        assert response.status_code == 404
        # 模拟升级前已存入的助手配置，Run 启动同样再次校验。
        async with sessions() as db:
            db.add(Agent(id=other, name="旧助手", owner_id=11, **{key: other}))
            await db.commit()
        denied = await client.post("/api/v2/runs", json={
            "message": "生成报告", "assistant_id": other, "task_mode": "document_generate",
        })
        assert denied.status_code == 404

    response = await client.post("/api/v2/runs", json={
        "message": "生成报告", "conversation_id": 31, "task_mode": "document_generate",
    })
    assert response.status_code == 200, response.text
    events = await _events(client, f"/api/v2/runs/{response.json()['run_id']}")
    assert events[-1]["event"] == "run.completed", events
    assert "approved-external-evidence" in events[-1]["data"]["final_text"]


@pytest.mark.parametrize(("message", "attachments", "task"), [
    ("帮我看一下附件并总结内容", [{"path": "uploads/report.docx"}], "document_analyze"),
    ("这是上季度的文件", [{"path": "uploads/report.docx"}], "general_chat"),
    ("根据这份样例提炼可复用的项目总结规范", [{"path": "uploads/report.docx"}], "profile_extract"),
    ("按照项目总结规范生成本季度报告", [], "document_generate"),
    ("修改上次报告的第三章", [], "document_edit"),
    ("把上次报告第三章的成果描述重新整理一下，其他章节不要动。", [], "document_edit"),
    ("如何生成一份 Word 报告", [], "general_chat"),
    ("什么是函数调用", [], "general_chat"),
])
def test_task_routes(message, attachments, task):
    assert route_task(message, attachment_refs=attachments)["task_type"] == task
    assert route_task(message, task_mode="chat", attachment_refs=attachments)["task_type"] == "general_chat"


def test_document_mcp_uses_current_binding_and_readonly_policy(monkeypatch):
    monkeypatch.setattr(document_graph, "build_chat_model", lambda *args, **kwargs: None)
    read = "mcp__documentQaOnly__lookup"
    confirm = "mcp__documentQaOnly__approve"
    other = "mcp__anotherDocumentQa__lookup"
    trusted = {
        "documentQaOnly": {"tools": {
            read: {"side_effect_class": "read_only", "requires_approval": False},
            confirm: {"side_effect_class": "read_only", "requires_approval": True},
        }},
        "anotherDocumentQa": {"tools": {
            other: {"side_effect_class": "read_only", "requires_approval": False},
        }},
    }
    register_mcp_tool_risks(trusted)
    run = RunHandle(run_id="mcp-qa", agent_id=1, session_id=1, user_id=1, thread_id="qa")
    ctx = RuntimeContext(scope=RunScope(1), run_id=run.run_id, thread_id=run.thread_id, agent_id=1,
                         agent_config={"mcp_runtime": {"documentQaOnly": trusted["documentQaOnly"]}})
    specs = [{"name": name, "description": "测试工具", "parameters": {}}
             for name in (read, confirm, other)]
    for network, expected in ((True, [read]), (False, [])):
        flow = document_graph.DocumentGraphContext(
            run, ctx, {}, {"supports_function_call": True},
            ToolPolicy(allowed_tools={read, confirm, other}, allow_network=network), specs, None,
        )
        assert [tool.name for tool in flow.tools] == expected


def test_attachment_fingerprint_changes_with_file_bytes(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "AGENT_FILES_ROOT", str(tmp_path))
    uploads = Path(tmp_path, "11", "21", "31", "uploads")
    uploads.mkdir(parents=True)
    file = uploads / "sample.docx"
    kwargs = {"user_id": 11, "agent_id": 21, "session_id": 31}
    attachments = [{"path": "uploads/sample.docx"}]
    file.write_text("one", encoding="utf-8")
    first = resolve_attachment_refs(attachments, **kwargs)
    file.write_text("two", encoding="utf-8")
    second = resolve_attachment_refs(attachments, **kwargs)
    assert first[0]["size"] == second[0]["size"]
    assert first[0]["sha256"] != second[0]["sha256"]
    for invalid in ("uploads/../sample.docx", str(file.resolve()), "/uploads/sample.docx"):
        with pytest.raises(ValueError, match="附件路径"):
            resolve_attachment_refs([{"path": invalid}], **kwargs)


def test_attachment_rejects_symbolic_links_to_files_and_parent_directories(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "AGENT_FILES_ROOT", str(tmp_path))
    uploads = tmp_path / "11" / "21" / "31" / "uploads"
    uploads.mkdir(parents=True)
    elsewhere = tmp_path / "other-user"
    elsewhere.mkdir()
    target = elsewhere / "sample.docx"
    target.write_bytes(b"private reference")
    link = uploads / "sample.docx"
    try:
        link.symlink_to(target)
    except OSError as exc:
        if getattr(exc, "winerror", None) == 1314:
            pytest.skip("Windows 当前账户未启用符号链接权限")
        raise
    kwargs = {"user_id": 11, "agent_id": 21, "session_id": 31}
    with pytest.raises(FileNotFoundError, match="附件不存在"):
        resolve_attachment_refs([{"path": "uploads/sample.docx"}], **kwargs)
    link.unlink()
    uploads.rmdir()
    uploads.symlink_to(elsewhere, target_is_directory=True)
    with pytest.raises(ValueError, match="附件路径越界"):
        resolve_attachment_refs([{"path": "uploads/sample.docx"}], **kwargs)
