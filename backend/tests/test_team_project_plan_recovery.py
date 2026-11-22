"""确认模型输出恢复不会改变软件范围、冲突分工或用户约束。"""

import json

import pytest
from pydantic import ValidationError

from app.services.agent_runtime.team_project_graph import _ProjectPlan
from app.services.agent_runtime.team_project_plan_recovery import fallback_project_plan, recover_project_plan


def test_normalizes_one_wrapped_plan_with_preamble_and_unambiguous_dependency_aliases():
    model_plan = {
        "projectPlan": {
            "title": "工单管理", "description": "实现工单管理", "features": ["登记、查询和更新工单"],
            "scope": "full-stack", "apiContract": {"GET /api/tickets": "列出工单"},
            "assignments": [
                {"taskId": "接口设计", "roleName": "架构师", "name": "共享契约", "instructions": "定义接口",
                 "outputFiles": ["./openapi.json"]},
                {"taskId": "Backend", "roleName": "后端工程", "name": "后端实现", "instructions": "实现服务",
                 "dependsOn": ["架构师", "接口设计"], "outputFiles": [r"backend\app.py", "./backend/app.py"]},
                {"taskId": "Frontend", "roleName": "前端工程", "name": "前端实现", "instructions": "实现界面",
                 "dependsOn": "共享契约", "outputFiles": ["frontend/index.html"]},
                {"roleName": "验收", "name": "回归测试", "instructions": "编写回归测试",
                 "dependsOn": ["backend", "前端工程"], "outputFiles": ["README.md", "tests/test_api.py"]},
            ],
        },
    }
    raw = "<think>规划文件交接</think>\n以下是实现计划。\n```json\n" + json.dumps(model_plan) + "\n```\n交付会逐步推进。"
    plan = _ProjectPlan.model_validate(recover_project_plan(raw, "fullstack"))
    assert plan.name == "工单管理"
    assert json.loads(plan.api_contract) == {"GET /api/tickets": "列出工单"}
    assert [task.id for task in plan.tasks] == ["task-1", "backend", "frontend", "task-4"]
    assert plan.tasks[1].files == ["backend/app.py"]
    assert plan.tasks[1].depends_on == plan.tasks[2].depends_on == ["task-1"]
    assert plan.tasks[-1].depends_on == ["backend", "frontend"]


@pytest.mark.parametrize("reply", [
    '{"tasks": []}\n{"tasks": []}',
    '```json\n{"tasks": []}\n```\n```json\n{"plan": {"tasks": []}}\n```',
    '{"plan": {"tasks": []}, "project": {"tasks": []}}',
    '<think>这是推理中的示例 {"tasks": []}',
    '{"tasks": [], "scope": "backend"}',
])
def test_rejects_ambiguous_or_incomplete_plans_and_different_scope(reply):
    with pytest.raises(ValueError):
        recover_project_plan(reply, "fullstack")


def test_conflicting_roles_and_unsafe_paths_are_not_corrected_away():
    plan = fallback_project_plan("构建一个前后端分离的项目，你随意发挥", "fullstack")
    unsafe = plan["tasks"][0]
    unsafe["files"].append("./backend/../secrets.json")
    with pytest.raises(ValidationError):
        _ProjectPlan.model_validate(recover_project_plan(json.dumps(plan), "fullstack"))
    unsafe["files"] = ["openapi.json", "./backend/app.py"]
    with pytest.raises(ValidationError):
        _ProjectPlan.model_validate(recover_project_plan(json.dumps(plan), "fullstack"))


@pytest.mark.parametrize(("scope", "total"), [("fullstack", 10), ("backend", 6), ("frontend", 6)])
def test_fallback_has_a_valid_dependency_plan_for_each_scope(scope, total):
    raw = fallback_project_plan("帮我构建一个足够复杂的前后端分离项目，你随意发挥", scope)
    plan = _ProjectPlan.model_validate(raw)
    assert plan.name == "协作工单台"
    assert sum(len(task.files) for task in plan.tasks) == total
    assert plan.tasks[-1].depends_on == [task.id for task in plan.tasks[:-1]]
    if scope == "fullstack":
        assert plan.tasks[1].depends_on == plan.tasks[2].depends_on == ["contract"]
        assert "openapi.json" in plan.tasks[0].files


def test_stack_delegation_preserves_the_requested_product():
    plan = fallback_project_plan("实现设备巡检和仪表保养平台，技术栈自行选择。", "fullstack")
    assert plan is not None and plan["name"] != "协作工单台"
    assert "工单" not in json.dumps(plan, ensure_ascii=False)
    assert "原始请求" in plan["tasks"][0]["brief"]


@pytest.mark.parametrize("objective", [
    "帮我构建一个项目", "不要随意发挥，按实施方案执行", "帮我用Vue和Spring Boot实现项目，其他你随意发挥。",
    "用C#做个后台，你随意发挥", "实现前后端项目，沿用现有依赖，你随意发挥", "做前后端项目，Docker Compose一键部署，技术栈随意",
    "后端你随意发挥，不使用数据库", "用支付宝小程序实现，随意发挥",
])
def test_fallback_does_not_override_a_non_delegated_or_constrained_stack(objective):
    assert fallback_project_plan(objective, "fullstack") is None
