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


def test_internal_read_events_complete_citation_evidence():
    """内置 read_file 不写工具账本：事件证据需补齐，且按调用 ID 去重。"""
    events = [
        (5, TOOL_STARTED, {"tool": "read_file", "tool_call_id": "call-1", "internal": True,
                           "input": {"file_path": "/workspace/uploads/report.docx.extracted.txt"}}),
        (6, TOOL_COMPLETED, {"tool": "read_file", "tool_call_id": "call-1", "internal": True,
                             "output": "报告正文"}),
        # 重试会重复发同一调用的完成事件
        (7, TOOL_COMPLETED, {"tool": "read_file", "tool_call_id": "call-1", "internal": True,
                             "output": "报告正文"}),
        # 非读取工具、非 internal 事件不进入读取证据
        (8, TOOL_COMPLETED, {"tool": "write_file", "tool_call_id": "call-2", "internal": True, "output": "ok"}),
        (9, TOOL_COMPLETED, {"tool": "read_file", "tool_call_id": "call-3", "output": "外部"}),
        (10, TOOL_FAILED, {"tool": "read_file", "tool_call_id": "call-4", "internal": True, "error": "not found"}),
    ]
    rows = completion._event_read_evidence(events, set())
    assert [(row.tool_name, row.status) for row in rows] == [("read_file", "succeeded")]
    assert rows[0].output == "报告正文"
    # 账本已有记录的调用不重复补
    assert completion._event_read_evidence(events, {"call-1"}) == []


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


def test_delivery_path_checks_reject_scratch_paths_and_unbacked_workspace_writes():
    """交付声明必须落在用户可见工作区，且有真实写入记录。"""
    checks = completion.delivery_path_checks(
        "调研文件 `/research/report.md` 已完成所有提纲项", [], [])
    assert checks and checks[0].id == "delivery_path" and not checks[0].passed
    assert "/research/report.md" in checks[0].detail

    evidence = [completion.ToolEvidence("write_file", "succeeded", path="report.md")]
    assert completion.delivery_path_checks("已保存到 /workspace/report.md", evidence, []) == []

    checks = completion.delivery_path_checks("已保存到 /workspace/report.md", [], [])
    assert checks and not checks[0].passed
    assert "没有对应写入记录" in checks[0].detail


def test_delivery_path_checks_allow_incidental_scratch_path_in_substantive_answer():
    """正文已给出完整内容时，临时区路径只是过程说明，不按虚假交付处理。"""
    text = "## 调研结论\n\n" + "完整分析正文。" * 120 + "\n\n过程笔记保存在 `/research/notes.md`。"
    assert completion.delivery_path_checks(text, [], []) == []


def test_delivery_path_checks_ignore_urls_and_plain_mentions():
    assert completion.delivery_path_checks(
        "参考 https://example.com/guide/report.md 中的说明", [], []) == []
    assert completion.delivery_path_checks("调研结论如下：……", [], []) == []


def test_completion_prompt_requires_full_answer_and_workspace_delivery():
    from app.services.assistant.task_contract import build_task_contract

    contract = build_task_contract("请联网调研 X 并至少 3 个来源", capability_ids=["web_research"])
    prompt = completion.completion_prompt(contract)
    assert "最终回答正文直接给出完整调研内容" in prompt
    assert "/workspace/" in prompt
    assert "至少 3 个" in prompt


def _answer(text: str, finish_reason: str | None = None) -> AIMessage:
    metadata = {"finish_reason": finish_reason} if finish_reason else {}
    return AIMessage(content=text, response_metadata=metadata, id="answer-1")


def _research_middleware(monkeypatch):
    from app.services.assistant.task_contract import build_task_contract

    contract = build_task_contract("请联网深度调研 X 并至少 3 个来源", capability_ids=["web_research"])
    ctx = RuntimeContext(RunScope(1), "run-cont", "thread", 7, session_id=5, task_contract=contract)
    monkeypatch.setattr(completion, "_collect_evidence", AsyncMock(return_value=([], [], False)))
    return completion.CompletionMiddleware(ctx, contract), ctx, contract


@pytest.mark.asyncio
async def test_length_truncation_continues_before_other_rework(monkeypatch):
    """长度截断优先补全余下正文：其他缺口（如来源未附）等正文完整后再验收。"""
    middleware, ctx, _ = _research_middleware(monkeypatch)
    text = "## 调研正文\n" + "结论正文。" * 200
    result = await middleware.aafter_model({"messages": [_answer(text, "length")], "todos": []}, None)
    review = result["completion_review"]
    assert result["jump_to"] == "model"
    assert review["continuing"] is True and review["prefix"] == text
    assert review["fix_rounds"] == 0 and review["continuations"] == 1
    assert "紧接其末尾" in result["messages"][0].content


@pytest.mark.asyncio
async def test_truncation_after_continuation_limit_stops_as_length(monkeypatch):
    middleware, ctx, _ = _research_middleware(monkeypatch)
    text = "正文。" * 500
    previous = {"run_id": ctx.run_id, "answer": text, "prefix": text, "signature": "old", "report": {},
                "fix_rounds": 0, "continuations": completion._MAX_ANSWER_CONTINUATIONS}
    result = await middleware.aafter_model(
        {"messages": [_answer("续写尾巴", "length")], "todos": [], "completion_review": previous}, None)
    assert result.get("jump_to") is None
    assert result["completion_review"]["stop_reason"] == "length"


@pytest.mark.asyncio
async def test_json_answer_truncation_requires_full_rewrite(monkeypatch):
    """JSON 无法拼接，截断后仍要求模型重新输出完整对象。"""
    from app.services.assistant.task_contract import build_task_contract

    contract = build_task_contract("请只输出 JSON 格式的结果", capability_ids=[])
    ctx = RuntimeContext(RunScope(1), "run-json", "thread", 7, session_id=5, task_contract=contract)
    monkeypatch.setattr(completion, "_collect_evidence", AsyncMock(return_value=([], [], False)))
    middleware = completion.CompletionMiddleware(ctx, contract)
    result = await middleware.aafter_model({"messages": [_answer('{"a": 1', "length")], "todos": []}, None)
    review = result["completion_review"]
    assert "prefix" not in review and review["continuing"] is True
    assert review["fix_rounds"] == 1 and review["continuations"] == 0


@pytest.mark.asyncio
async def test_plan_only_rework_keeps_delivered_answer(monkeypatch):
    """只差计划项未关闭时，短状态说明不能覆盖已经交付的完整正文。"""
    from app.services.assistant.task_contract import build_task_contract

    contract = build_task_contract("写一份项目总结", capability_ids=[])
    ctx = RuntimeContext(RunScope(1), "run-plan", "thread", 7, session_id=5, task_contract=contract)
    monkeypatch.setattr(completion, "_collect_evidence", AsyncMock(return_value=([], [], False)))
    middleware = completion.CompletionMiddleware(ctx, contract)
    delivered = "## 项目总结\n" + "完整正文。" * 400
    previous = {"run_id": ctx.run_id, "answer": delivered, "signature": "s", "report": {},
                "fix_rounds": 0, "continuations": 0, "stop_reason": ""}
    result = await middleware.aafter_model({
        "messages": [_answer("已更新计划状态")],
        "todos": [{"content": "撰写总结", "status": "in_progress"}],
        "completion_review": previous,
    }, None)
    review = result["completion_review"]
    assert review["preserve_answer"] == delivered
    assert review["answer"] == delivered
    assert "不要重写" in result["messages"][0].content


def test_continuation_join_dedupes_seam_and_replaces_full_restatement():
    prefix = "调研正文内容。" * 200
    assert completion._continuation_join(prefix, prefix[-120:] + "续写部分") == prefix + "续写部分"
    assert completion._continuation_join(prefix, prefix + "新增") == prefix + "新增"
    assert completion._continuation_join(prefix, "全新段落") == prefix + "\n全新段落"
    assert completion._continuation_join("", "文本") == "文本"
