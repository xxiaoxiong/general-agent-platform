"""WorkPlanner：JSON 解析、超时/非法回退、一次修复、提示边界。"""
from __future__ import annotations

import json

import pytest

from app.services.work_orchestrator import planner
from app.services.work_orchestrator.planner import (
    PlannerParseError, parse_plan, plan_work, render_planner_prompt,
)
from app.services.work_orchestrator.registry import ExecutorRegistry


def _plan_payload() -> dict:
    return {
        "objective": "调研并生成 Word 报告",
        "constraints": ["必须引用来源"],
        "steps": [
            {
                "id": "research", "title": "资料检索", "executor_profile": "research",
                "objective": "收集可核验来源", "acceptance_criteria": ["有来源"],
                "deliverables": ["research_sources"],
            },
            {
                "id": "document", "title": "生成报告", "executor_profile": "document_docx",
                "objective": "整理为 Word", "depends_on": ["research"],
                "input_refs": ["step:research"], "acceptance_criteria": ["文件可下载"],
            },
        ],
    }


def _registry() -> ExecutorRegistry:
    return ExecutorRegistry(
        tool_names=["web_search", "write_file", "knowledge_search"],
        capability_ids=["web_research", "doc_processing"],
    )


def test_parse_plan_accepts_fenced_json_and_rejects_invalid():
    raw = json.dumps(_plan_payload(), ensure_ascii=False)
    plan = parse_plan(f"```json\n{raw}\n```")
    assert [step.id for step in plan.steps] == ["research", "document"]
    with pytest.raises(PlannerParseError):
        parse_plan("不是 JSON")
    with pytest.raises(PlannerParseError):
        parse_plan('{"objective":"x","steps":[{"id":"a","executor_profile":"browser"}]}')


def test_prompt_only_lists_allowed_executors_and_user_constraints():
    prompt = render_planner_prompt(
        message="调研并生成 Word 报告",
        decision={"intent": "document_generate", "constraints": ["中文"], "deliverables": ["报告"]},
        history=[{"role": "user", "content": "之前的讨论"}],
        registry=_registry(),
        extra_constraints=["至少引用 3 个来源"],
    )
    assert "research（资料检索与研究）" in prompt
    assert "document_docx（Word 文档）" in prompt
    assert "software_project" in prompt  # 不可用清单明确禁止
    assert "中文" in prompt and "至少引用 3 个来源" in prompt
    assert "message_0" in prompt and "[message_0]" in prompt


def test_planner_system_prompt_keeps_three_stage_requests_separate():
    assert "研究、分析、文档三个步骤" in planner._SYSTEM
    assert "不要把分析合并进研究步骤" in planner._SYSTEM


@pytest.mark.asyncio
async def test_planner_timeout_and_invalid_output_fall_back(monkeypatch):
    async def timeout(*args, **kwargs):
        raise TimeoutError("slow")

    monkeypatch.setattr(planner, "invoke_planner_model", timeout)
    outcome = await plan_work(
        message="调研并生成报告", decision={}, history=[], artifacts=[], attachments=[],
        preview_document=None, registry=_registry(), provider={}, model_config={},
        timeout=0.01,
    )
    assert outcome.plan is None and outcome.fallback

    async def invalid(*args, **kwargs):
        return "not json", 1, 1

    monkeypatch.setattr(planner, "invoke_planner_model", invalid)
    outcome = await plan_work(
        message="调研并生成报告", decision={}, history=[], artifacts=[], attachments=[],
        preview_document=None, registry=_registry(), provider={}, model_config={},
    )
    assert outcome.plan is None and outcome.repair_attempted


@pytest.mark.asyncio
async def test_planner_allows_one_repair(monkeypatch):
    calls = {"count": 0}

    async def flaky(*args, **kwargs):
        if calls["count"] == 0:
            calls["count"] += 1
            return "not json", 2, 3
        calls["count"] += 1
        return json.dumps(_plan_payload(), ensure_ascii=False), 4, 5

    monkeypatch.setattr(planner, "invoke_planner_model", flaky)
    outcome = await plan_work(
        message="调研并生成报告", decision={}, history=[], artifacts=[], attachments=[],
        preview_document=None, registry=_registry(), provider={}, model_config={"name": "m1"},
    )
    assert outcome.plan is not None and outcome.repair_attempted
    assert outcome.plan.planner_metadata.model_id == "m1"
    assert outcome.plan.planner_metadata.output_tokens == 8
