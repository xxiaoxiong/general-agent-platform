"""主智能体续做需要可核对的文件和前一轮执行状态。"""

import hashlib
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from app.core.config import settings
from app.services.assistant.verification import CheckResult
from app.services.agent_runtime import completion
from app.services.agent_runtime.context import RunScope, RuntimeContext
from app.services.agent_runtime.events import TOOL_COMPLETED, TOOL_FAILED, TOOL_STARTED
from app.services.agent_runtime.tool_batches import _repeating_tool_rounds


def test_repeated_tool_turns_pause_only_with_unchanged_read_or_failure_evidence():
    def round_output(index, tool, path, output, *, status="success"):
        call_id = f"call-{index}"
        return [
            AIMessage(content="", tool_calls=[{"name": tool, "id": call_id, "args": {"file_path": path}}]),
            ToolMessage(content=output, tool_call_id=call_id, status=status),
        ]

    stalled = [HumanMessage(content="检查文件并解决冲突")]
    for index in range(0, 6, 2):
        stalled.extend(round_output(index, "read_file", "/workspace/report.md", "原内容"))
        stalled.extend(round_output(index + 1, "edit_file", "/workspace/report.md", "匹配位置不唯一", status="error"))

    assert _repeating_tool_rounds(stalled)
    assert not _repeating_tool_rounds(stalled[:-1])
    assert not _repeating_tool_rounds(stalled[:7] + [HumanMessage(content="恢复后继续检查")] + stalled[7:])
    assert not _repeating_tool_rounds(stalled[:-1] + [
        ToolMessage(content="已修改", tool_call_id="call-5"),
    ])
    assert not _repeating_tool_rounds(stalled[:-1] + [
        ToolMessage(content="新匹配位置", tool_call_id="call-5", status="error"),
    ])


@pytest.mark.asyncio
async def test_continuation_only_carries_current_files_with_matching_digests(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "AGENT_FILES_ROOT", str(tmp_path))
    ctx = RuntimeContext(RunScope(1), "next-run", "thread", 7, session_id=5)
    workspace = tmp_path / "1" / "7" / "5"
    workspace.mkdir(parents=True)
    original = b"original file\n"
    digest = hashlib.sha256(original).hexdigest()
    (workspace / "confirmed.md").write_bytes(original)
    (workspace / "changed.md").write_bytes(b"changed since checkpoint\n")

    await completion.prepare_completion_continuation(ctx, {
        "artifacts": dict.fromkeys(["confirmed.md", "changed.md", "missing.md"], digest),
        "todos": [{"content": "补齐文档", "status": "in_progress"}],
    })

    assert ctx.carried_artifacts == {"confirmed.md": digest}
    assert ctx.completion_continuation["changed_or_missing_files"] == ["changed.md", "missing.md"]
    assert ctx.completion_continuation["verified_files"] == ["confirmed.md"]
    assert "无需逐个列出、读取或重建" in completion.continuation_prompt(ctx)


@pytest.mark.asyncio
async def test_continuation_does_not_read_another_runs_checkpoint_todos(monkeypatch):
    valid = [{"content": "原任务", "status": "in_progress"}]
    foreign = [{"content": "其它轮的计划", "status": "completed"}]
    record = SimpleNamespace(metadata={"run_id": "next-run", "user_id": 1},
                             checkpoint={"channel_values": {"todos": foreign}}, pending_writes=[])
    saver = SimpleNamespace(aget_tuple=AsyncMock(return_value=record))
    factory = SimpleNamespace(get_checkpointer=AsyncMock(return_value=saver))
    monkeypatch.setattr(completion.CheckpointerFactory, "get", lambda: factory)
    db = SimpleNamespace(scalar=AsyncMock(return_value={"todos": valid}))

    assert await completion._read_continuation_todos(db, "thread", "previous-run", 1) == valid
    db.scalar.assert_awaited_once()
    record.metadata["run_id"] = "previous-run"
    record.checkpoint["channel_values"]["todos"] = []
    db.scalar.reset_mock()
    assert await completion._read_continuation_todos(db, "thread", "previous-run", 1) == []
    db.scalar.assert_not_awaited()
    record.pending_writes.append(("planning-task", "todos", valid))
    assert await completion._read_continuation_todos(db, "thread", "previous-run", 1) == valid


@pytest.mark.asyncio
async def test_continued_project_receives_actual_preflight_findings_before_any_model_turn(monkeypatch):
    contract = completion.build_task_contract("构建一个可运行的前后端分离项目，执行生产构建并交付源码 ZIP。")
    ctx = RuntimeContext(RunScope(1), "fresh-run", "thread", 7, session_id=5, task_contract=contract)
    ctx.completion_continuation = {"todos": [{"content": "验证工程", "status": "in_progress"}]}
    ctx.carried_artifacts = {"frontend/package.json": "recorded-hash"}
    saved = [{"path": "frontend/package.json"}]
    read_evidence = AsyncMock(return_value=([], saved, False))
    monkeypatch.setattr(completion, "_collect_evidence", read_evidence)
    middleware = completion.CompletionMiddleware(ctx, contract)
    problem = completion.CheckResult("runtime_build", False, "frontend/package.json 需要的 TypeScript 版本超出离线环境范围")
    verify = AsyncMock(return_value=completion.ProjectReview("digest", [problem], []))
    monkeypatch.setattr(middleware, "_verify_project", verify)

    first = await middleware.abefore_agent({}, None)
    assert first["todos"] == ctx.completion_continuation["todos"]
    assert problem.detail in first["messages"][0].content
    assert await middleware.abefore_agent({"completion_run_id": ctx.run_id}, None) is None
    verify.assert_awaited_once_with(saved)


@pytest.mark.asyncio
@pytest.mark.parametrize("objective,passed,should_continue", [
    ("构建一个可运行的前后端分离项目，运行生产构建、可执行测试，并用浏览器验证业务结果，交付 ZIP。", [], True),
    ("构建一个可运行的前后端分离项目，运行生产构建、可执行测试，并用浏览器验证业务结果，交付 ZIP。",
     ["project_errors", "runtime_start", "runtime_build", "runtime_tests", "runtime_browser", "archive"], False),
    ("写一份报告并保存为 report.md。", [], False),
])
async def test_completed_run_continue_requires_unverified_project_gates(objective, passed, should_continue, monkeypatch):
    monkeypatch.setattr(settings, "AGENT_VERIFICATION_ENABLED", True)
    record = SimpleNamespace(
        id="old-run", status="completed", thread_id="thread",
        input_payload={"messages": [{"role": "user", "content": objective}], "execution_context": {"task_type": "general_chat"}},
        summary={"verification": {"version": 2, "checks": [{"id": key, "passed": True}
                                                        for key in ["answer_present", "artifact_produced", *passed]]}},
    )
    plan = [{"content": "完成运行测试", "status": "in_progress"}]
    monkeypatch.setattr(completion, "_read_continuation_todos", AsyncMock(return_value=plan))
    monkeypatch.setattr(completion, "_continuation_file_actions", AsyncMock(return_value=[]))
    db = SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(one_or_none=lambda: record)),
                         scalars=AsyncMock(return_value=SimpleNamespace(all=lambda: [])))
    resume = await completion.resolve_completion_resume(db, 1, 2, "继续", [{"role": "assistant", "source_run_id": record.id}])

    if should_continue:
        assert resume["run_id"] == record.id and resume["todos"] == plan
        assert {item["id"] for item in resume["failed_checks"]} == {
            "project_errors", "runtime_start", "runtime_build", "runtime_tests", "runtime_browser", "archive",
        }
    else:
        assert resume is None


@pytest.mark.asyncio
async def test_continuation_records_latest_unconfirmed_write_without_repeating_fixed_paths():
    def start(call_id, path):
        return TOOL_STARTED, {"internal": True, "tool": "write_file", "tool_call_id": call_id,
                              "input": {"file_path": f"/workspace/{path}"}}

    def result(kind, call_id, error=""):
        return kind, {"internal": True, "tool_call_id": call_id, "error": error}

    rows = [start("old-fixed", "fixed.md"), result(TOOL_FAILED, "old-fixed", "已经存在"),
            start("new-fixed", "fixed.md"), result(TOOL_COMPLETED, "new-fixed"),
            start("old-uncertain", "blocked.md"), start("new-uncertain", "blocked.md"),
            result(TOOL_COMPLETED, "old-uncertain"), result(TOOL_FAILED, "new-uncertain", "文件路径被目录占用"),
            start("pending-at-stop", "pending.md")]
    db = SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(all=lambda: rows)))

    assert await completion._continuation_file_actions(db, "previous-run") == [
        {"path": "blocked.md", "tool": "write_file", "detail": "文件路径被目录占用"},
        {"path": "pending.md", "tool": "write_file", "detail": "写入结果尚未确认"},
    ]


def test_project_continuation_rebuilds_empty_plan_from_current_proof_and_unconfirmed_writes():
    history = [{"content": "核对项目结构", "status": "completed"}]
    ctx = {
        "todos": history,
        "pending_files": [{"path": "frontend/index.html", "detail": "目标文件已存在"}],
        "failed_checks": [{"id": "runtime_browser", "detail": "上一轮报告浏览器失败"}],
    }
    result = completion._continuation_project_todos(ctx, [
        CheckResult("runtime_browser", True, ""),
        CheckResult("project_errors", False, "backend/app.py 缺少启动入口"),
    ])
    assert result == [*history,
                      {"content": "核对未确认的写入 frontend/index.html · 目标文件已存在", "status": "pending"},
                      {"content": "修复验收发现的工程问题 · backend/app.py 缺少启动入口", "status": "pending"}]
    assert completion._continuation_project_todos({**ctx, "pending_files": [], "todos": []}, None) == [
        {"content": "待验收 执行真实浏览器操作并验证业务结果", "status": "pending"},
    ]
    assert completion._continuation_project_todos({**ctx, "todos": result}, []) == result
