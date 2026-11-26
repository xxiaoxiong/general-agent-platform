"""Decision Architecture：Schema / Context / Validator / Apply / 编排 / 回归场景。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.services.agent_runtime.decision.apply import apply_decision
from app.services.agent_runtime.decision.context import (
    SYSTEM_PROMPT, build_decision_context, render_decision_prompt,
)
from app.services.agent_runtime.decision.orchestrator import (
    resolve_run_decision, should_defer_semantic,
)
from app.services.agent_runtime.decision.schemas import RunDecision
from app.services.agent_runtime.decision.service import (
    DecisionOutcome, DecisionParseError, parse_decision,
)
from app.services.agent_runtime.decision.validator import validate_decision
from app.services.agent_runtime.document_turn import document_turn
from app.services.agent_runtime.task_router import (
    detect_deliverable_format, route_task, user_grants_autonomy,
)
from app.services.assistant.task_contract import TASK_QA, TASK_RESEARCH, build_task_contract


def _decision(**overrides) -> RunDecision:
    payload = {
        "intent": "general_chat",
        "route": "deep_agent",
        "continuity": "new_task",
        "goal": "",
        "deliverable_format": "none",
        "target": "none",
        "rationale": "测试",
    }
    payload.update(overrides)
    return RunDecision.model_validate(payload)


def _context(**overrides):
    values = dict(
        message="这一轮用户消息",
        history=[],
        artifacts=[],
        preview_document=None,
        attachments=[],
        capabilities=[],
        tools=[],
        explicit={},
    )
    values.update(overrides)
    return build_decision_context(**values)


# ── Schema 与解析 ────────────────────────────────────────────────────────────

def test_run_decision_schema_is_strict_and_clips_lists():
    decision = _decision(context_refs=["artifact_1", "artifact_1", "x" * 300])
    assert decision.context_refs == ["artifact_1", "x" * 200]
    with pytest.raises(Exception):
        _decision(intent="banana")
    with pytest.raises(Exception):
        RunDecision.model_validate({"intent": "general_chat", "route": "deep_agent", "extra": 1})


def test_run_decision_normalizes_common_model_aliases():
    decision = RunDecision.model_validate({
        "intent": "doc_generate",
        "route": "document",
        "continuity": "continue",
        "deliverable_format": "word",
        "target": "current_document",
        "edit_scope": "content_and_style",
        "project_scope": "Full-Stack",
    })
    assert decision.intent == "document_generate"
    assert decision.route == "document_graph"
    assert decision.continuity == "continue_existing"
    assert decision.deliverable_format == "docx"
    assert decision.target == "preview"
    assert decision.edit_scope == "both"
    assert decision.project_scope == "fullstack"

    relaxed = RunDecision.model_validate({
        "intent": "general_chat", "route": "deep_agent",
        "deliverable_format": "exe", "project_scope": "unknown-scope",
    })
    assert relaxed.deliverable_format == "none"
    assert relaxed.project_scope is None


def test_parse_decision_accepts_fenced_json_and_rejects_invalid():
    raw = json.dumps({
        "intent": "document_generate", "route": "document_graph",
        "deliverable_format": "docx", "goal": "生成报告",
    })
    parsed = parse_decision(f"```json\n{raw}\n```")
    assert parsed.intent == "document_generate"
    with pytest.raises(DecisionParseError):
        parse_decision("不是 JSON")
    with pytest.raises(DecisionParseError):
        parse_decision('{"intent":"unknown_intent","route":"deep_agent"}')


# ── 上下文 ───────────────────────────────────────────────────────────────────

def test_decision_context_exposes_candidate_refs_and_untrusted_warning():
    context = _context(
        history=[
            {"role": "user", "content": "生成一份调研报告"},
            {"role": "assistant", "content": "# 报告\n正文", "document_run_id": "run-1"},
        ],
        artifacts=[{"document_id": 7, "title": "调研报告", "version_no": 2}],
        recent_runs=[{"run_id": "run-1", "status": "failed", "created_at": "2026-11-26T00:00:00"}],
        preview_document={"document_id": 7, "title": "调研报告", "version_no": 2},
        attachments=[{"name": "notes.md", "path": "uploads/notes.md"}],
        capabilities=["doc_processing"],
        tools=["read_file", "write_file"],
    )
    assert {"message_0", "message_1", "artifact_7", "preview_document", "attachment_0"} == context.candidate_refs()
    prompt = render_decision_prompt(context)
    assert "候选引用 message_i" in prompt and "artifact_7" in prompt
    assert "最近运行" in prompt and "failed" in prompt
    assert "doc_processing" in prompt and "write_file" in prompt
    assert "不可信" in SYSTEM_PROMPT


def test_decision_context_does_not_treat_history_documents_as_current_instruction():
    context = _context(
        history=[{"role": "assistant", "content": "上一轮文档正文", "document_run_id": "run-1"}],
        artifacts=[],
    )
    prompt = render_decision_prompt(context)
    assert "该轮产生了文档产物" in prompt
    assert "artifact_" not in prompt


# ── Validator ────────────────────────────────────────────────────────────────

def test_validator_corrects_route_and_drops_unauthorized_refs_and_capabilities():
    context = _context(
        artifacts=[{"document_id": 7, "title": "报告", "version_no": 1}],
        capabilities=["doc_processing"],
    )
    result = validate_decision(
        _decision(
            intent="document_edit", route="deep_agent",
            context_refs=["artifact_7", "artifact_999"],
            capability_suggestions=["doc_processing", "admin_database"],
            deliverable_format="docx", target="latest_draft",
        ),
        context=context,
    )
    assert result.route == "document_graph"
    assert result.decision.context_refs == ["artifact_7"]
    assert result.decision.capability_suggestions == ["doc_processing"]
    assert any("route" in note for note in result.notes)


def test_validator_degrades_edit_without_target_to_chat():
    result = validate_decision(_decision(intent="document_edit", route="document_graph"), context=_context())
    assert result.intent == "general_chat"
    assert result.route == "deep_agent"
    assert result.degraded


def test_validator_promotes_word_delivery_to_document_generate():
    result = validate_decision(
        _decision(intent="research", route="deep_agent", deliverable_format="docx"),
        context=_context(), user_format="docx",
    )
    assert result.intent == "document_generate"
    assert result.route == "document_graph"


def test_validator_does_not_inherit_file_format_for_new_task():
    result = validate_decision(
        _decision(intent="general_chat", deliverable_format="md"),
        context=_context(), user_format=None,
    )
    assert result.decision.deliverable_format == "none"

    explicit = validate_decision(
        _decision(intent="general_chat", deliverable_format="md"),
        context=_context(), user_format="md",
    )
    assert explicit.decision.deliverable_format == "md"


def test_validator_does_not_default_to_word_without_user_format_or_target():
    result = validate_decision(
        _decision(intent="document_generate", route="document_graph",
                  deliverable_format="docx"),
        context=_context(),
    )
    assert result.intent == "general_chat"
    assert result.route == "deep_agent"

    continued = validate_decision(
        _decision(intent="document_generate", route="document_graph",
                  deliverable_format="docx", continuity="continue_existing"),
        context=_context(artifacts=[{"document_id": 7, "title": "报告", "version_no": 1}]),
    )
    assert continued.intent == "document_generate"
    assert continued.route == "document_graph"


def test_validator_defaults_missing_format_to_docx_for_word_generation():
    result = validate_decision(
        _decision(intent="document_generate", route="document_graph"),
        context=_context(), user_format="docx",
    )
    assert result.intent == "document_generate"
    assert result.route == "document_graph"
    assert result.decision.deliverable_format == "docx"


def test_validator_markdown_generation_uses_general_capability():
    result = validate_decision(
        _decision(intent="document_generate", route="document_graph", deliverable_format="md"),
        context=_context(),
    )
    assert result.intent == "general_chat"
    assert result.route == "deep_agent"


def test_validator_user_named_format_overrides_model_output():
    result = validate_decision(
        _decision(intent="general_chat", deliverable_format="none"),
        context=_context(), user_format="docx",
    )
    assert result.intent == "document_generate"
    assert result.route == "document_graph"
    assert result.decision.deliverable_format == "docx"

    markdown = validate_decision(
        _decision(intent="document_generate", route="document_graph",
                  deliverable_format="docx"),
        context=_context(), user_format="md",
    )
    assert markdown.intent == "general_chat"
    assert markdown.decision.deliverable_format == "md"


def test_validator_does_not_block_clear_word_generation_for_missing_details():
    result = validate_decision(
        _decision(intent="document_generate", route="document_graph",
                  decision_status="ambiguous", deliverable_format="docx"),
        context=_context(), user_format="docx",
    )
    assert result.route == "document_graph"
    assert result.intent == "document_generate"
    assert result.decision.decision_status == "clear"


def test_validator_analyze_with_existing_document_does_not_clarify():
    context = _context(artifacts=[{"document_id": 7, "title": "报告", "version_no": 1}])
    result = validate_decision(
        _decision(intent="document_analyze", route="deep_agent",
                  decision_status="ambiguous"),
        context=context,
    )
    assert result.route == "deep_agent"
    assert result.intent == "document_analyze"
    assert result.decision.decision_status == "clear"


def test_validator_edit_with_explicit_target_does_not_clarify():
    context = _context(artifacts=[{"document_id": 7, "title": "报告", "version_no": 1}])
    result = validate_decision(
        _decision(intent="document_edit", route="document_graph",
                  decision_status="ambiguous", target="latest_draft",
                  continuity="modify_existing", deliverable_format="docx"),
        context=context,
    )
    assert result.route == "document_graph"
    assert result.intent == "document_edit"


def test_validator_ambiguous_requires_clarification_route():
    result = validate_decision(
        _decision(intent="general_chat", decision_status="ambiguous"),
        context=_context(),
    )
    assert result.route == "clarify"
    assert result.clarification_question
    assert result.intent == "general_chat"


def test_validator_software_project_respects_auto_team_flag():
    decision = _decision(
        intent="software_project", route="team_project_graph",
        project_scope="fullstack",
    )
    off = validate_decision(decision, context=_context(), auto_team=False)
    assert off.route == "deep_agent"
    on = validate_decision(decision, context=_context(), auto_team=True)
    assert on.route == "team_project_graph"


# ── Apply ────────────────────────────────────────────────────────────────────

def test_apply_chat_releases_stale_document_binding():
    rules = route_task("帮我解释一下这个方案的核心取舍", document_id=7, has_document_context=True)
    assert rules["task_type"] == "document_analyze"
    route = apply_decision(
        validate_decision(_decision(), context=_context(artifacts=[{
            "document_id": 7, "title": "方案", "version_no": 1,
        }])),
        rules, context=_context(artifacts=[{"document_id": 7, "title": "方案", "version_no": 1}]),
    )
    assert route["task_type"] == "general_chat"
    assert route["document_id"] is None
    assert "document_context" not in route
    assert route["intent_source"] == "llm"


def test_apply_markdown_delivery_uses_general_capability():
    context = _context(message="把上面的调研结果整理成 md 文件给我下载")
    validated = validate_decision(
        _decision(intent="document_generate", route="document_graph",
                  deliverable_format="md", target="latest_draft"),
        context=context,
    )
    route = apply_decision(validated, route_task(context.message), context=context)
    assert route["task_type"] == "general_chat"
    assert route["deliverable"] == {"format": "md"}


def test_apply_edit_binds_preview_document_and_style_scope():
    context = _context(preview_document={"document_id": 9, "title": "报告", "version_no": 3})
    validated = validate_decision(
        _decision(intent="document_edit", route="document_graph", deliverable_format="docx",
                  target="preview", edit_scope="style", continuity="modify_existing"),
        context=context,
    )
    route = apply_decision(validated, route_task("把字体调大"), context=context)
    assert route["task_type"] == "document_edit"
    assert route["document_id"] == 9
    assert route["document_version"] == 3
    assert route["style_edit"] is True


def test_apply_software_project_without_team_stays_general_chat():
    context = _context(message="帮我构建一个设备管理平台并交付源码")
    validated = validate_decision(
        _decision(intent="software_project", route="deep_agent", project_scope="fullstack"),
        context=context, auto_team=False,
    )
    route = apply_decision(validated, route_task(context.message), context=context)
    assert route["task_type"] == "general_chat"
    assert route["wants_software_project"] is True


def test_apply_software_project_with_team_enters_team_route():
    context = _context(message="帮我构建一个设备管理平台并交付源码")
    validated = validate_decision(
        _decision(intent="software_project", route="team_project_graph",
                  project_scope="fullstack"),
        context=context, auto_team=True,
    )
    route = apply_decision(validated, route_task(context.message), context=context)
    assert route["task_type"] == "software_project"
    assert route["team_mode"] is True
    assert route["project_scope"] == "fullstack"


# ── 任务书 §9 回归场景 ──────────────────────────────────────────────────────

def test_regression_consulting_vs_software_project():
    consult = _context(message="怎么开发一个设备管理平台？")
    chat = validate_decision(_decision(), context=consult)
    assert apply_decision(chat, route_task(consult.message), context=consult)["task_type"] == "general_chat"

    build = _context(message="帮我构建一个设备管理平台并交付源码。")
    project = validate_decision(
        _decision(intent="software_project", route="deep_agent", project_scope="fullstack"),
        context=build, auto_team=False,
    )
    assert apply_decision(project, route_task(build.message), context=build)["task_type"] == "general_chat"


def test_regression_compound_research_then_word_keeps_both_requirements():
    context = _context(message="先调研行业，再把结果整理成 Word。")
    validated = validate_decision(
        _decision(
            intent="document_generate", route="document_graph", deliverable_format="docx",
            constraints=["先完成行业调研并核实来源"], goal="行业调研报告",
            acceptance_criteria=["包含来源清单"],
        ),
        context=context, user_format="docx",
    )
    route = apply_decision(validated, route_task(context.message), context=context)
    assert route["task_type"] == "document_generate"
    assert route["decision_constraints"] == ["先完成行业调研并核实来源"]
    assert route["decision_acceptance"] == ["包含来源清单"]


def test_regression_supplement_and_reexport_edits_latest_document():
    context = _context(
        message="把刚才的研究报告补充数据后重新导出。",
        artifacts=[{"document_id": 12, "title": "研究报告", "version_no": 4}],
    )
    validated = validate_decision(
        _decision(intent="document_edit", route="document_graph", deliverable_format="docx",
                  continuity="modify_existing", target="latest_draft", edit_scope="content"),
        context=context,
    )
    route = apply_decision(validated, route_task(context.message), context=context)
    assert route["task_type"] == "document_edit"
    assert route["document_id"] == 12
    assert route["document_version"] == 4


def test_regression_translate_does_not_mechanically_create_new_document():
    context = _context(
        message="把刚才的内容翻译成英文。",
        artifacts=[{"document_id": 12, "title": "研究报告", "version_no": 4}],
    )
    validated = validate_decision(_decision(), context=context)
    route = apply_decision(validated, route_task(context.message), context=context)
    assert route["task_type"] == "general_chat"
    assert route["document_id"] is None


def test_regression_bare_continue_requires_confirmable_context():
    context = _context(message="继续。")
    validated = validate_decision(_decision(), context=context)
    route = apply_decision(validated, route_task("继续。"), context=context)
    assert route["task_type"] == "general_chat"


def test_regression_uploaded_word_check_chapter_uses_analyze_with_attachment():
    context = _context(
        message="检查第三章。",
        attachments=[{"name": "报告.docx", "path": "uploads/报告.docx"}],
    )
    validated = validate_decision(
        _decision(intent="document_analyze", route="deep_agent",
                  target="attachment", continuity="new_task"),
        context=context,
    )
    route = apply_decision(validated, route_task(context.message), context=context)
    assert route["task_type"] == "document_analyze"
    assert route["document_id"] is None


def test_regression_conflicting_resource_requests_are_rejected():
    context = _context(capabilities=["doc_processing"])
    result = validate_decision(
        _decision(
            intent="document_edit", route="team_project_graph",
            capability_suggestions=["shell_admin"], context_refs=["artifact_404"],
            target="preview", deliverable_format="docx",
        ),
        context=context,
    )
    assert result.route == "deep_agent"
    assert result.decision.capability_suggestions == []
    assert result.decision.context_refs == []
    assert result.decision.target == "none"


# ── 编排：应用 / Shadow / 兜底 / 恢复复用 ──────────────────────────────────

def _base_context(**overrides):
    payload = {
        "task_type": "general_chat",
        "task_mode": "auto",
        "attachment_refs": [],
        "decision_pending": True,
        "decision_input": {
            "message": "这一轮用户消息",
            "artifacts": [],
            "preview_document": None,
            "attachments": [],
            "explicit": {"task_mode": "auto"},
            "is_superuser": False,
            "message_id": "m1",
            "document_id": None,
            "document_profile_id": None,
            "task_options": {},
        },
    }
    payload.update(overrides)
    return payload


def _messages():
    return [
        {"role": "user", "content": "生成一份报告"},
        {"role": "assistant", "content": "已生成报告正文"},
        {"role": "user", "content": "放到 md 文档中给到我下载"},
    ]


@pytest.mark.asyncio
async def test_orchestrator_applies_decision_and_persists_snapshot(monkeypatch):
    from app.services.agent_runtime.decision import orchestrator

    async def fake_decide(*_args, **_kwargs):
        return DecisionOutcome(
            decision=_decision(deliverable_format="md", rationale="交付 md 文件"),
            latency_ms=18, fallback=False, model_id="fake",
            input_tokens=120, output_tokens=30,
        )

    persisted = {}

    async def fake_persist(run_id, context):
        persisted["context"] = context

    async def fake_mark(*_args):
        persisted["marked"] = True

    monkeypatch.setattr(orchestrator, "decide", fake_decide)
    monkeypatch.setattr(orchestrator, "persist_execution_context", fake_persist)
    monkeypatch.setattr(orchestrator, "mark_message_document_run", fake_mark)

    resolved, event = await resolve_run_decision(
        run_id="run-1", session_id=None, user_id=1,
        execution_context=_base_context(deliverable_format="md"), messages=_messages(),
        agent_config={"id": 11, "capability_ids": []},
        provider={}, model_config={"name": "fake"},
        mode="llm",
    )
    assert resolved["task_type"] == "general_chat"
    assert resolved["deliverable"] == {"format": "md"}
    assert "decision_pending" not in resolved and "decision_input" not in resolved
    assert resolved["decision"]["applied"] is True
    assert resolved["decision"]["decision_source"] == "llm"
    assert event["intent"] == "general_chat"
    assert resolved["decision"]["input_tokens"] == 120
    assert resolved["decision"]["output_tokens"] == 30
    assert persisted["context"]["decision"]["latency_ms"] == 18
    assert "marked" not in persisted


@pytest.mark.asyncio
async def test_orchestrator_shadow_mode_keeps_legacy_route(monkeypatch):
    from app.services.agent_runtime.decision import orchestrator

    async def fake_decide(*_args, **_kwargs):
        return DecisionOutcome(
            decision=_decision(intent="document_edit", route="document_graph",
                               deliverable_format="docx", target="latest_draft",
                               edit_scope="content", continuity="modify_existing"),
            latency_ms=9, fallback=False, model_id="fake",
        )

    async def fake_persist(*_args):
        return None

    monkeypatch.setattr(orchestrator, "decide", fake_decide)
    monkeypatch.setattr(orchestrator, "persist_execution_context", fake_persist)

    shadow_context = _base_context()
    shadow_context["decision_input"]["artifacts"] = [
        {"document_id": 7, "title": "报告", "version_no": 1}
    ]
    resolved, event = await resolve_run_decision(
        run_id="run-2", session_id=None, user_id=1,
        execution_context=shadow_context, messages=_messages(),
        agent_config={"id": 11, "capability_ids": []},
        provider={}, model_config={"name": "fake"},
        mode="shadow",
    )
    assert resolved["task_type"] == "general_chat"
    assert resolved["decision"]["applied"] is False
    assert resolved["decision"]["route"] == "document_graph"
    assert event["mode"] == "shadow"


@pytest.mark.asyncio
async def test_orchestrator_falls_back_to_rules_when_model_unavailable(monkeypatch):
    from app.services.agent_runtime.decision import orchestrator

    async def fake_decide(*_args, **_kwargs):
        return DecisionOutcome(decision=None, latency_ms=4, fallback=True, error="超时")

    async def fake_persist(*_args):
        return None

    monkeypatch.setattr(orchestrator, "decide", fake_decide)
    monkeypatch.setattr(orchestrator, "persist_execution_context", fake_persist)

    resolved, _event = await resolve_run_decision(
        run_id="run-3", session_id=None, user_id=1,
        execution_context=_base_context(), messages=_messages(),
        agent_config={"id": 11, "capability_ids": []},
        provider={}, model_config={"name": "fake"},
        mode="llm",
    )
    assert resolved["task_type"] == "general_chat"
    assert resolved["decision"]["fallback"] is True
    assert resolved["decision"]["decision_source"] == "rules"


@pytest.mark.asyncio
async def test_orchestrator_reuses_persisted_decision_after_restart(monkeypatch):
    from app.services.agent_runtime.decision import orchestrator

    calls = {"decide": 0}

    async def fake_decide(*_args, **_kwargs):
        calls["decide"] += 1
        raise AssertionError("恢复时不得重复调用决策模型")

    monkeypatch.setattr(orchestrator, "decide", fake_decide)
    context = _base_context(decision_pending=False, decision={"applied": True, "intent": "general_chat"})
    resolved, event = await resolve_run_decision(
        run_id="run-4", session_id=None, user_id=1,
        execution_context=context, messages=None,
        agent_config={"id": 11}, provider={}, model_config={"name": "fake"},
        mode="llm",
    )
    assert calls["decide"] == 0
    assert event is None
    assert resolved is context


def test_should_defer_semantic_respects_explicit_controls():
    assert should_defer_semantic("llm", task_mode="auto", has_context=True) is True
    assert should_defer_semantic("shadow", task_mode="auto", has_context=True) is True
    assert should_defer_semantic("off", task_mode="auto", has_context=True) is False
    assert should_defer_semantic("llm", task_mode="auto", has_context=False) is False
    assert should_defer_semantic("llm", task_mode="document_generate", has_context=True) is False
    assert should_defer_semantic("llm", task_mode="auto", document_id=7, has_context=True) is False
    assert should_defer_semantic("llm", task_mode="auto", team_mode=True, has_context=True) is False


# ── TaskContract 与决策解耦 ─────────────────────────────────────────────────

def test_task_contract_prefers_decision_intent_over_keywords():
    contract = build_task_contract(
        "帮我看看最新进展，并给出判断", decision_intent="general_chat",
    )
    assert contract.task_type == TASK_QA
    assert contract.source == "decision"

    research = build_task_contract("帮我看看最新进展", decision_intent="research")
    assert research.task_type == TASK_RESEARCH
    assert "citations_present" in research.completion_checks


def test_task_contract_accepts_decision_project_scope():
    contract = build_task_contract(
        "做一个管理后台", decision_intent="software_project",
        project_scope_override="frontend",
    )
    assert contract.task_type == "code"
    assert contract.project_scope == "frontend"


# ── 既有规则快速通道保持可用 ────────────────────────────────────────────────

def test_format_detection_and_non_word_routing_remain_available():
    assert detect_deliverable_format("放到md文档中给到我下载") == "md"
    route = route_task("放到md文档中给到我下载", has_document_context=True)
    assert route["task_type"] == "general_chat"
    assert route["deliverable_format"] == "md"


def test_evaluation_dataset_is_large_and_schema_valid():
    """任务书 §8 Phase 0：至少 200 条可离线评测的标注样例。"""
    path = Path(__file__).parent / "evaluation" / "decision_cases.jsonl"
    cases = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(cases) >= 200
    intents = {
        "general_chat", "research", "document_analyze", "document_generate",
        "document_edit", "profile_extract", "software_project", "data_analysis",
        "database_query", "coding", "business_action", "unknown",
    }
    routes = {"deep_agent", "document_graph", "team_graph", "team_project_graph", "clarify"}
    for case in cases:
        assert case["id"] and case["category"] and case["message"]
        assert case["expected"]["intent"] in intents
        assert case["expected"]["route"] in routes
        assert case["expected"].get("continuity", "new_task") in {
            "new_task", "continue_existing", "modify_existing", "resume_existing", "unrelated",
        }
        for key in ("history", "artifacts", "attachments"):
            assert isinstance(case.get(key) or [], list)


def test_autonomy_and_upgrade_guards_remain_available():
    assert user_grants_autonomy("不需要再向我确认，缺少的用通用假设")
    message = "写一份《AI Agent 评测入门》Word 文档给我，不需要再向我确认，缺少的用通用假设"
    route = document_turn(message, [], route_task(message))[0]
    assert route["document_context"]["autonomous"] is True

    report = "# 调研报告\n\n## 一、背景\n\n" + "背景说明。" * 80
    history = [
        {"role": "user", "content": "帮我调研 agent 框架"},
        {"role": "assistant", "content": report, "document_run_id": "run-1"},
    ]
    guarded = document_turn("把上面的内容整理成几条要点", history,
                            route_task("把上面的内容整理成几条要点"), allow_upgrade=False)[0]
    assert guarded["task_type"] == "general_chat"
