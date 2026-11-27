"""WorkPlan Schema / Validator / Complexity / Registry 单元测试。"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.services.work_orchestrator.complexity import is_composite
from app.services.work_orchestrator.registry import ExecutorRegistry
from app.services.work_orchestrator.schemas import (
    WorkPlan, WorkPlanRecord, WorkStep, new_plan_record,
)
from app.services.work_orchestrator.validator import validate_plan


def _step(step_id: str = "research", profile: str = "research", **overrides) -> dict:
    payload = {
        "id": step_id,
        "title": "资料检索",
        "objective": "收集可核验来源",
        "executor_profile": profile,
        "depends_on": [],
        "input_refs": [],
        "required_capabilities": [],
        "deliverables": ["research_sources"],
        "acceptance_criteria": ["关键结论有可核验来源"],
    }
    payload.update(overrides)
    return payload


def _plan(*steps: dict, **overrides) -> WorkPlan:
    payload = {"objective": "调研并生成报告", "steps": list(steps) or [_step()]}
    payload.update(overrides)
    return WorkPlan.model_validate(payload)


def _registry(**overrides) -> ExecutorRegistry:
    values = dict(
        tool_names=["web_search", "write_file", "read_file", "knowledge_search"],
        capability_ids=["web_research", "doc_processing"],
        auto_team=False,
        database_bound=False,
    )
    values.update(overrides)
    return ExecutorRegistry(**values)


def test_plan_schema_is_strict_and_normalizes_executor_aliases():
    plan = _plan(
        _step("research", "researcher"),
        _step("document", "word", depends_on=["research"], input_refs=["step:research"]),
    )
    assert [item.executor_profile for item in plan.steps] == ["research", "document_docx"]
    with pytest.raises(ValidationError):
        WorkPlan.model_validate({
            "objective": "x", "steps": [_step()], "unexpected": True,
        })
    with pytest.raises(ValidationError):
        _plan(_step("bad", "browser_agent"))
    with pytest.raises(ValidationError):
        _plan(_step("a"), _step("a"))
    with pytest.raises(ValidationError):
        _plan(*[_step(f"s{index}") for index in range(7)])


def test_plan_record_separates_runtime_state_and_ready_steps():
    plan = _plan(
        _step("research", "research"),
        _step("analysis", "general_agent", depends_on=["research"], input_refs=["step:research"]),
    )
    record = new_plan_record(plan, mode="auto")
    assert record.status == "ready"
    assert [item.id for item in record.ready_steps()] == ["research"]
    record.steps["research"].status = "completed"
    record.steps["research"].summary = "已完成检索"
    assert [item.id for item in record.ready_steps()] == ["analysis"]
    assert record.plan_summary()["steps"][0]["status"] == "completed"


def test_validator_accepts_valid_plan_and_rejects_cycles_forward_refs_and_unknown_refs():
    valid = _plan(
        _step("research", "research"),
        _step("document", "document_docx", depends_on=["research"], input_refs=["step:research"]),
    )
    result = validate_plan(
        valid, registry=_registry(), context_refs=["message_0"], user_message="调研并生成报告",
    )
    assert result.valid, result.errors

    forward = _plan(
        _step("document", "document_docx", depends_on=["research"]),
        _step("research", "research"),
    )
    result = validate_plan(forward, registry=_registry(), context_refs=[])
    assert not result.valid and any("前序步骤" in item for item in result.errors)

    cycle = _plan(
        _step("document", "document_docx", depends_on=["research"]),
        _step("research", "research", depends_on=["document"]),
    )
    result = validate_plan(cycle, registry=_registry(), context_refs=[])
    assert not result.valid
    assert any("只能依赖前序步骤" in item for item in result.errors)

    unknown_ref = _plan(_step("research", "research", input_refs=["artifact_999"]))
    result = validate_plan(unknown_ref, registry=_registry(), context_refs=["message_0"])
    assert not result.valid and any("不存在的上下文" in item for item in result.errors)

    missing_acceptance = _plan(_step("research", "research", acceptance_criteria=[]))
    result = validate_plan(missing_acceptance, registry=_registry(), context_refs=[])
    assert not result.valid


def test_validator_drops_unauthorized_capability_and_rejects_unavailable_executor():
    plan = _plan(_step(
        "research", "research",
        required_capabilities=["admin_database"],
    ))
    result = validate_plan(plan, registry=_registry(), context_refs=[])
    assert result.valid
    assert result.plan.steps[0].required_capabilities == []
    assert any("未授权能力" in item for item in result.warnings)

    # 没有联网/知识库工具时 research 执行器不可用，不能靠计划"扩权"
    result = validate_plan(
        _plan(_step("research", "research")),
        registry=_registry(tool_names=["write_file"]),
        context_refs=[],
    )
    assert not result.valid
    assert any("执行器不可用" in item for item in result.errors)


def test_validator_rejects_workspace_paths_and_new_urls_but_allows_user_urls():
    bad_path = _plan(_step("research", "research", objective="把结果写到 /workspace/report.docx"))
    assert not validate_plan(bad_path, registry=_registry(), context_refs=[]).valid

    bad_url = _plan(_step("research", "research", objective="访问 https://evil.example.com 抓取"))
    assert not validate_plan(bad_url, registry=_registry(), context_refs=[]).valid

    user_url = "参考 https://example.com/source 做调研并生成报告"
    allowed = _plan(_step("research", "research", objective="访问 https://example.com/source 获取资料"))
    assert validate_plan(
        allowed, registry=_registry(), context_refs=[], user_message=user_url,
    ).valid


def test_registry_enforces_team_database_and_business_boundaries():
    assert _registry().resolve("general_agent").allowed
    assert not _registry(auto_team=False, explicit_team=False).resolve("team").allowed
    assert _registry(auto_team=True).resolve("team").allowed
    assert _registry(auto_team=True).resolve("software_project").allowed
    assert not _registry(tool_names=["web_search"]).resolve("database").allowed
    assert _registry(tool_names=["db_query"], database_bound=True).resolve("database").allowed
    assert not _registry(tool_names=["web_search"]).resolve("business_action").allowed
    assert _registry(tool_names=["http_post"]).resolve("business_action").allowed
    allowed, denied = _registry().capability_allowed(["web_research", "admin"])
    assert allowed == ["web_research"] and denied == ["admin"]


def test_complexity_only_plans_real_composite_tasks():
    assert not is_composite(message="解释一下什么是向量数据库").composite
    assert not is_composite(
        message="把这段内容整理成 Word 文档", execution_context={"deliverable_format": "docx"},
    ).composite
    assert not is_composite(
        message="调研一下国产 Agent 平台的现状", decision={"intent": "research"},
    ).composite
    assert not is_composite(
        message="调研之后生成 Word 报告并做数据分析",
        execution_context={"team_mode": True},
    ).composite
    assert not is_composite(message="调研之后生成报告", goal_mode=True).composite

    research = is_composite(
        message="调研国产 Agent 平台，比较技术架构，最后生成 Word 报告",
        decision={"intent": "document_generate"},
        execution_context={"task_type": "document_generate", "deliverable_format": "docx"},
    )
    assert research.composite and "research" in research.domains

    trend = is_composite(
        message="先分析当前企业 Agent 平台的主要技术趋势，再据此生成一份 Word 报告",
        decision={"intent": "document_generate"},
        execution_context={"task_type": "document_generate", "deliverable_format": "docx"},
    )
    assert trend.composite and "research" in trend.domains

    database = is_composite(
        message="查询数据库并生成一份经营分析报告",
        decision={"intent": "database_query", "deliverable_format": "docx"},
        execution_context={"deliverable_format": "docx"},
    )
    assert database.composite and "data_analysis" in database.domains or "research" in database.domains

    two_domains = is_composite(
        message="先分析这份销售数据，再根据结论写一份 Word 报告",
        execution_context={"deliverable_format": "docx"},
    )
    assert two_domains.composite
