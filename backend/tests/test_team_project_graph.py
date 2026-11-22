"""核对工程团队的文件交接、返工、验收与可恢复写入。"""

import asyncio
import json
from collections import Counter

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from app.services.agent_runtime.project_browser_contract import BROWSER_CHECKS_PATH
from app.services.agent_runtime.repositories.errors import PersistenceError
from app.services.agent_runtime.team_project_files import ProjectWorkspaceError
from app.services.agent_runtime.team_project_graph import (
    ProjectFileTruncated,
    ProjectPlanTruncated,
    TeamProjectContext,
    _repair_file_text,
    build_project_graph,
    drive_project_graph,
)
from app.services.agent_runtime.tools.policy import BudgetExceededError

_CONFIG = {"configurable": {"thread_id": "team:project-test"}}
_INPUT = {"objective": "构建前后端分离的任务管理平台", "project_scope": "fullstack"}
_TASKS = [
    {"id": "api", "role": "后端工程师", "title": "开发任务服务", "brief": "实现任务接口",
     "depends_on": [], "files": ["backend/app.py"]},
    {"id": "web", "role": "界面工程师", "title": "开发任务界面", "brief": "实现任务交互",
     "depends_on": [], "files": ["frontend/index.html", "frontend/app.js"]},
    {"id": "qa", "role": "验收工程师", "title": "验收交付", "brief": "覆盖关键用户操作",
     "depends_on": ["api", "web"], "files": ["openapi.json", "tests/test_api.py", "README.md"]},
]
_PLAN = {"name": "任务平台", "summary": "维护复杂任务的状态", "features": ["创建任务", "更新状态", "查询"],
         "api_contract": "GET /api/tasks 返回任务列表", "tasks": _TASKS}


def _example_file(path):
    if path.endswith(".html"):
        return '<main><button id="load">读取</button></main><script src="app.js"></script>'
    if path.endswith(".js"):
        return "document.querySelector('#load').addEventListener('click', () => fetch('/api/tasks'));"
    if path.endswith(".mjs"):
        return "export const state = { tasks: [] };"
    if path.endswith(".py"):
        return f"# 测试文件 {path}\npass"
    if path.endswith(".json"):
        return "{}"
    return f"有效内容 {path}"


def test_project_patch_rejects_changed_or_overlapping_anchors():
    original = "one\ntwo\nthree\n"
    for anchors in [("missing",), ("one\ntwo", "two\nthree"), ("one", "one")]:
        content = "\n".join(f"<<<<<<< SEARCH\n{old}\n=======\nfixed\n>>>>>>> REPLACE" for old in anchors)
        with pytest.raises(ProjectWorkspaceError, match="锚点"):
            _repair_file_text("script.py", content, original)


def test_localized_html_patch_with_a_model_preamble_preserves_the_full_page():
    original = '<!doctype html>\n<html><body>\n<h1>原始页面</h1>\n<script src="app.js"></script>\n</body></html>\n'
    patch = ('已定位首页标题，下面进行局部修复。\n\n<<<<<<< SEARCH\n<h1>原始页面</h1>\n=======\n'
             '<h1>完整的工作台</h1>\n>>>>>>> REPLACE')
    result = _repair_file_text("frontend/index.html", patch, original)
    assert result == original.replace("原始页面", "完整的工作台")
    with pytest.raises(ProjectWorkspaceError, match="锚点"):
        _repair_file_text("frontend/index.html", patch + "\n<<<<<<< SEARCH\n未闭合", original)


class _Harness:
    def __init__(self, invoke, *, saver=None):
        self.invoke = invoke
        self.saver = saver or InMemorySaver()
        self.files = {}
        self.events = []
        self.checks = 0
        self.calls = Counter()
        self.crash = False

    async def model(self, stage, system, user):
        data = json.loads(user)
        self.calls[stage, data.get("path")] += 1
        return await self.invoke(stage, system, data)

    async def read_file(self, path):
        if path not in self.files:
            raise FileNotFoundError(path)
        return self.files[path]

    async def write_file(self, path, content):
        self.files[path] = content
        if self.crash and path == "frontend/index.html":
            self.crash = False
            raise PersistenceError("模拟第二个文件落盘、checkpoint 尚未提交时中断")

    async def emit(self, event, value):
        self.events.append((event, value))

    async def review(self, plan):
        self.checks += 1
        expected = [path for task in plan["tasks"] for path in task["files"]]
        issues = [{"path": path, "message": "缺少文件"} for path in expected if path not in self.files]
        if self.files.get("frontend/app.js", "").startswith("坏接口"):
            issues.append({"path": "frontend/app.js", "message": "API 路径不一致"})
        return {"passed": not issues, "issues": issues, "checks": ["文件和接口一致性"]}

    async def publish(self, plan, report):
        assert self.checks > 0
        return {"title": plan["name"], "file_count": len(self.files), "download_url": "/api/v2/artifacts/test",
                "state": "passed" if report["passed"] else "failed", "checks": report["checks"]}

    async def run(self, initial=None, *, browser_checks=False):
        context = TeamProjectContext(self.model, self.emit, self.write_file, self.review, self.publish, self.read_file,
                                     browser_checks=browser_checks)
        return await drive_project_graph(build_project_graph(self.saver, context), _CONFIG, initial or _INPUT)


@pytest.mark.asyncio
async def test_browser_scenario_uses_committed_ui_and_is_preserved_during_repair():
    scenario = json.dumps({"version": 1, "scenarios": [{"name": "交互验证", "steps": [
        {"action": "open", "path": "/"}, {"action": "click", "selector": "#load"},
        {"action": "expect_text", "selector": "#result", "value": "已加载"},
    ]}]}, ensure_ascii=False)
    problem = f"{BROWSER_CHECKS_PATH} 「交互验证」第 3 步。操作或断言未满足 expect_text #result"

    async def invoke(stage, system, data):
        if stage == "project_planning":
            return json.dumps(_PLAN)
        path = data["path"]
        if path == BROWSER_CHECKS_PATH:
            assert stage == "project_file" and "独立页面验收工程师" in system
            assert "frontend/index.html" in data["committed_files"] and "frontend/app.js" in data["committed_files"]
            return scenario
        if stage == "project_repair":
            assert path == "frontend/app.js" and data["verification_findings"] == [problem]
            assert data["committed_files"][BROWSER_CHECKS_PATH] == scenario + "\n"
            return "document.querySelector('#load').onclick = () => { document.querySelector('#result').textContent = '已加载'; };"
        return _example_file(path)

    class BrowserHarness(_Harness):
        async def review(self, plan):
            report = await super().review(plan)
            assert self.files[BROWSER_CHECKS_PATH] == scenario + "\n"
            if "已加载" not in self.files["frontend/app.js"]:
                report.update(passed=False, issues=[{"path": "frontend/app.js", "message": problem}])
            return report

    result = BrowserHarness(invoke)
    assert (await result.run(browser_checks=True))["project"]["state"] == "passed"
    assert result.calls["project_file", BROWSER_CHECKS_PATH] == 1
    assert result.calls["project_repair", BROWSER_CHECKS_PATH] == 0


@pytest.mark.asyncio
async def test_browser_rework_budget_preserves_progress_to_later_steps():
    async def invoke(stage, system, data):
        if stage == "project_planning":
            return json.dumps(_PLAN)
        if stage == "project_repair":
            assert data["path"] == "frontend/app.js"
            return data["current_file"] + "\n// 已解决下一个页面故障"
        return _example_file(data["path"])

    class JourneyHarness(_Harness):
        async def review(self, plan):
            result = await super().review(plan)
            stage = self.files["frontend/app.js"].count("已解决下一个页面故障")
            if stage < 3:
                result.update(passed=False, issues=[{"path": "frontend/app.js", "message": (
                    f"{BROWSER_CHECKS_PATH} 「保存并刷新」第 {stage + 4} 步。当前页面状态未达到预期"
                )}])
            return result

    result = JourneyHarness(invoke)
    assert (await result.run())["project"]["state"] == "passed"
    assert result.calls["project_repair", "frontend/app.js"] == 3


@pytest.mark.asyncio
async def test_project_parallel_writes_shared_contract_and_repairs_only_bad_file():
    in_flight = set()
    parallel = asyncio.Event()

    async def invoke(stage, system, data):
        if stage == "project_planning":
            assert "项目立项" in system and data["project_scope"] == "fullstack"
            return "<think>规划</think>\n```json\n" + json.dumps(_PLAN, ensure_ascii=False) + "\n```"
        assert data["project"]["api_contract"] == _PLAN["api_contract"]
        path = data["path"]
        if path in {"backend/app.py", "frontend/index.html"}:
            in_flight.add(path)
            if len(in_flight) == 2:
                parallel.set()
            await asyncio.wait_for(parallel.wait(), timeout=3)
        if path == "README.md":
            assert "backend/app.py" in data["committed_files"]
        if stage == "project_repair":
            assert "项目返工" in system and path == "frontend/app.js"
            assert data["verification_findings"] == ["API 路径不一致"]
            assert data["current_file"].strip() == "坏接口"
            return "fetch('/api/tasks');"
        assert "项目文件" in system
        return "坏接口" if path == "frontend/app.js" else _example_file(path)

    harness = _Harness(invoke)
    result = await harness.run()
    assert result["project"]["state"] == "passed"
    assert harness.files["frontend/app.js"] == "fetch('/api/tasks');\n"
    assert harness.checks == 2
    assert harness.calls["project_repair", "frontend/app.js"] == 1
    assert not result.get("failure")
    assert result["team"]["phase"] == "completed"
    assert "已交付" in result["final_text"]
    assert all(task["status"] == "completed" for task in result["team"]["tasks"])
    assert any(event == "project.delivery" for event, _ in harness.events)
    assert {data["phase"] for event, data in harness.events if event == "project.progress"} == {
        "planning", "building", "checking", "repairing", "packaging",
    }


@pytest.mark.asyncio
async def test_unfixed_project_preserves_the_draft_without_ready_completion():
    async def invoke(stage, system, data):
        if stage == "project_planning":
            return json.dumps(_PLAN)
        return "坏接口" if data["path"] == "frontend/app.js" else _example_file(data["path"])

    harness = _Harness(invoke)
    result = await harness.run()
    assert not result.get("final_text")
    assert "未通过" in result["failure"]
    assert result["project"]["state"] == "failed"
    assert result["team"]["phase"] == "failed"
    assert next(task for task in result["team"]["tasks"] if task["id"] == "web")["status"] == "failed"
    assert harness.calls["project_repair", "frontend/app.js"] == 2
    assert harness.checks == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("verified_command", [True, False])
async def test_readme_repair_preserves_existing_launch_instructions(verified_command):
    original = "# 启动\npython backend/app.py\n\n## 测试\npytest test_api.py -v\n"
    repaired = original.replace("pytest test_api.py -v", "python test_api.py")
    seen = []

    async def invoke(stage, system, data):
        if stage == "project_planning":
            return json.dumps(_PLAN)
        if data["path"] != "README.md":
            return _example_file(data["path"])
        if stage == "project_file":
            return original
        assert data["current_file"] == original
        seen.append(data.get("correction"))
        return ("当前工作路径：/home/user\n文件总数：7\n" if len(seen) == 1 else
                "<<<<<<< SEARCH\npytest test_api.py -v\n=======\npython test_api.py\n>>>>>>> REPLACE")

    class ReadmeHarness(_Harness):
        async def review(self, plan):
            result = await super().review(plan)
            if self.files["README.md"] == repaired:
                return result
            result.update(passed=False, issues=[{"path": "README.md", "message": "执行独立脚本须使用 Python"}])
            if verified_command:
                result["runtime_result"] = {"readme_edits": {"pytest test_api.py -v": "python test_api.py"}}
            return result

    harness = ReadmeHarness(invoke)
    assert (await harness.run())["project"]["state"] == "passed"
    assert harness.files["README.md"] == repaired
    assert len(seen) == (0 if verified_command else 2)
    if seen:
        assert "丢失了已有的启动方式" in seen[-1]


@pytest.mark.asyncio
async def test_invalid_plan_cannot_publish_duplicate_path_or_cyclic_owners():
    plan = {**_PLAN, "tasks": [dict(task) for task in _TASKS]}
    plan["tasks"][1]["files"] = ["BACKEND/app.py"]

    async def invoke(stage, system, data):
        assert stage == "project_planning"
        return json.dumps(plan)

    harness = _Harness(invoke)
    result = await harness.run()
    assert "计划" in result["failure"]
    assert harness.calls == {("project_planning", None): 2}
    assert not harness.files and not harness.checks


@pytest.mark.asyncio
async def test_resume_after_disk_write_reuses_durable_drafts_without_repeat_model_calls():
    async def invoke(stage, system, data):
        return json.dumps(_PLAN) if stage == "project_planning" else _example_file(data["path"])

    harness = _Harness(invoke)
    harness.crash = True
    with pytest.raises(PersistenceError, match="模拟"):
        await harness.run()
    assert sorted(harness.files) == ["backend/app.py", "frontend/index.html"]
    result = await harness.run()
    assert result["project"]["state"] == "passed"
    assert all(count == 1 for count in harness.calls.values())
    events = list(harness.events)
    assert await harness.run() == result
    assert harness.events == events


@pytest.mark.asyncio
async def test_model_budget_exhaustion_keeps_committed_files_as_failed_draft():
    async def invoke(stage, system, data):
        if stage == "project_planning":
            return json.dumps(_PLAN)
        if data["path"] == "frontend/app.js":
            raise BudgetExceededError("已达到模型上限")
        return _example_file(data["path"])

    harness = _Harness(invoke)
    result = await harness.run()
    assert sorted(harness.files) == ["backend/app.py", "frontend/index.html"]
    assert result["project"]["state"] == "failed"
    assert "模型预算" in result["failure"]
    assert harness.calls["project_file", "frontend/app.js"] == 1
    assert harness.checks == 1
    assert not any(stage == "project_repair" for stage, _ in harness.calls)


@pytest.mark.asyncio
async def test_truncated_planning_response_gets_compact_retry_and_delivers():
    async def invoke(stage, system, data):
        if stage == "project_planning":
            if not data["correction"]:
                raise ProjectPlanTruncated
            assert "生成长度上限" in data["correction"]
            return "以下是可交付的计划：\n```json\n" + json.dumps(_PLAN) + "\n```\n按此计划执行。"
        return _example_file(data["path"])

    harness = _Harness(invoke)
    result = await harness.run()
    assert result["project"]["state"] == "passed"
    assert harness.calls["project_planning", None] == 2
    assert any(data.get("issue") == "response_truncated" for _, data in harness.events)
    assert not any(data.get("planning_status") == "fallback" for _, data in harness.events)


@pytest.mark.asyncio
@pytest.mark.parametrize("provider_available", [True, False])
async def test_delegated_stack_fallback_only_when_model_returns_invalid_planning(provider_available):
    async def invoke(stage, system, data):
        if stage == "project_planning":
            if not provider_available:
                raise ConnectionError("测试模型暂不可用")
            return "无效工程计划"
        assert "随意发挥" in data["objective"]
        return _example_file(data["path"])

    harness = _Harness(invoke)
    result = await harness.run({**_INPUT, "objective": "帮我构建一个足够复杂的前后端分离项目，你随意发挥"})
    if provider_available:
        assert result["project"]["state"] == "passed"
        assert {"README.md", "openapi.json", "backend/app.py", "frontend/index.html"} <= harness.files.keys()
        assert any(data.get("planning_status") == "fallback" for _, data in harness.events)
        assert len(harness.files) == result["project"]["file_count"]
    else:
        assert not harness.files
        assert "模型服务" in result["failure"]
        assert not any(data.get("planning_status") == "fallback" for _, data in harness.events)


@pytest.mark.asyncio
async def test_wrong_file_type_or_truncated_reply_is_retried_before_writing():
    calls = Counter()

    async def invoke(stage, system, data):
        if stage == "project_planning":
            return json.dumps(_PLAN)
        path = data["path"]
        calls[path] += 1
        if path == "frontend/index.html":
            if calls[path] == 1:
                return "我是 kimi-k3.1-pro。"
            assert "自我介绍" in data["correction"]
        if path == "frontend/app.js":
            if calls[path] == 1:
                raise ProjectFileTruncated
            assert "截断" in data["correction"]
            return "document.querySelector('#load').addEventListener('click', () => fetch('/api/tasks'));"
        if path == "backend/app.py":
            if calls[path] == 1:
                return "def incomplete(:"
            assert "无法解析" in data["correction"]
        return _example_file(path)

    harness = _Harness(invoke)
    result = await harness.run()
    assert result["project"]["state"] == "passed"
    assert "kimi" not in harness.files["frontend/index.html"]
    assert calls["frontend/app.js"] == calls["frontend/index.html"] == 2
    assert calls["backend/app.py"] == 2
    assert not any(stage == "project_repair" for stage, _ in harness.calls)


@pytest.mark.asyncio
async def test_resume_seed_repairs_only_bad_files_and_uses_the_current_page():
    page = '<!doctype html><main><button id="taskCreate">创建任务</button></main><script src="app.js"></script>'
    displaced = '<!doctype html><!--' + '原页面样式' * 1500 + '-->' + page

    async def invoke(stage, system, data):
        assert stage == "project_repair"
        if data["path"] == "frontend/index.html":
            assert "taskCreate" in data["committed_files"]["frontend/app.js"]
            return page
        assert data["path"] == "frontend/app.js"
        assert data["committed_files"]["frontend/index.html"].strip() == page
        return "document.querySelector('#taskCreate').addEventListener('click', () => fetch('/api/tasks'));"

    class SeedHarness(_Harness):
        async def review(self, plan):
            result = await super().review(plan)
            for path, wrong in (('frontend/index.html', '我是 kimi-k3.1-pro。'), ('frontend/app.js', displaced)):
                if self.files[path] == wrong:
                    result['issues'].append({'path': path, 'message': f'{path} 内容类型错误'})
            result['passed'] = not result['issues']
            return result

    harness = SeedHarness(invoke)
    original = {path: _example_file(path) for task in _TASKS for path in task["files"]}
    original.update({"frontend/index.html": "我是 kimi-k3.1-pro。", "frontend/app.js": displaced})
    harness.files = dict(original)
    result = await harness.run({**_INPUT, "plan": {**_PLAN, "scope": "fullstack"}, "restored_paths": list(original)})
    assert result["project"]["state"] == "passed"
    assert harness.calls == {("project_repair", "frontend/index.html"): 1, ("project_repair", "frontend/app.js"): 1}
    assert {path for path in original if original[path] != harness.files[path]} == {"frontend/index.html", "frontend/app.js"}


@pytest.mark.asyncio
async def test_project_repair_accepts_a_small_patch_and_preserves_the_other_code():
    original = "// 保留已有的功能\n坏接口('/api/old');\nconst ready = true;\n"

    async def invoke(stage, system, data):
        assert stage == "project_repair" and data["path"] == "frontend/app.js"
        assert "<<<<<<< SEARCH" in system and data["current_file"] == original
        return "<<<<<<< SEARCH\n坏接口('/api/old');\n=======\nfetch('/api/tasks');\n>>>>>>> REPLACE"

    class PatchHarness(_Harness):
        async def review(self, plan):
            result = await super().review(plan)
            if "/api/old" in self.files["frontend/app.js"]:
                result["issues"].append({"path": "frontend/app.js", "message": "请求了缺失的 API 路径"})
                result["passed"] = False
            return result

    harness = PatchHarness(invoke)
    harness.files = {path: _example_file(path) for task in _TASKS for path in task["files"]}
    harness.files["frontend/app.js"] = original
    report = await harness.run({**_INPUT, "plan": {**_PLAN, "scope": "fullstack"},
                                "restored_paths": list(harness.files)})
    assert report["project"]["state"] == "passed"
    assert harness.files["frontend/app.js"] == original.replace("坏接口('/api/old');", "fetch('/api/tasks');")
    assert harness.calls == {("project_repair", "frontend/app.js"): 1}


@pytest.mark.asyncio
@pytest.mark.parametrize(("rejected", "correction"), [
    ("我是 kimi-k3.1-pro。", "自我介绍"),
    ("<<<<<<< SEARCH\n缺失的原文\n=======\n改动\n>>>>>>> REPLACE", "未在原文中唯一匹配"),
    (None, "截断"),
])
async def test_rejected_repair_uses_same_source_and_full_evidence_without_an_extra_verification(rejected, correction):
    path = "frontend/src/App.vue"
    tasks = [dict(task) for task in _TASKS]
    tasks[1]["files"] = ["frontend/index.html", path]
    plan = {**_PLAN, "scope": "fullstack", "tasks": tasks}
    original = '<script setup lang="ts">\nconst count: string = 0\n</script>\n<template><p>{{ count }}</p></template>\n'
    fixed = original.replace("count: string", "count: number")
    evidence = f"{path} · " + "前序诊断" * 80 + " · tests/test_api.py:16 完工后刷新应保持数量"
    replies = []

    async def invoke(stage, system, data):
        assert stage == "project_repair" and data["path"] == path
        assert data["current_file"] == original and data["verification_findings"] == [evidence]
        assert data["committed_files"]["tests/test_api.py"] == _example_file("tests/test_api.py")
        assert data["reference_context"] == "用户要求刷新后保留结果"
        replies.append(data)
        if len(replies) == 1:
            assert correction not in data.get("correction", "")
            if rejected is None:
                raise ProjectFileTruncated
            return rejected
        assert correction in data["correction"] and "可应用的编辑块" in system
        assert "assistant_context" not in data and "frontend/index.html" not in data["committed_files"]
        return "<<<<<<< SEARCH\nconst count: string = 0\n=======\nconst count: number = 0\n>>>>>>> REPLACE"

    class CompileHarness(_Harness):
        async def review(self, plan):
            result = await super().review(plan)
            if self.files[path] != fixed:
                result.update(passed=False, issues=[{"path": path, "message": evidence}])
            return result

    harness = CompileHarness(invoke)
    harness.files = {name: _example_file(name) for task in tasks for name in task["files"]}
    harness.files[path] = original
    report = await harness.run({**_INPUT, "plan": plan, "restored_paths": list(harness.files),
                                "reference_context": "用户要求刷新后保留结果"})
    assert report["project"]["state"] == "passed"
    assert harness.calls == {("project_repair", path): 2}
    assert harness.checks == 2 and harness.files[path] == fixed
    assert report["repair_attempts"][path] == 1


@pytest.mark.asyncio
async def test_focused_repair_does_not_replace_the_whole_draft_with_an_unanchored_fragment():
    path = "frontend/app.js"
    original = "坏接口;\nconst existingFeature = true;\n"

    async def invoke(stage, system, data):
        assert stage == "project_repair" and data["path"] == path
        if "回复仅由可应用的编辑块组成" in system:
            return "fetch('/api/tasks');"
        assert data["current_file"] == original
        return "我是 kimi-k3.1-pro。"

    harness = _Harness(invoke)
    harness.files = {name: _example_file(name) for task in _TASKS for name in task["files"]}
    harness.files[path] = original
    report = await harness.run({**_INPUT, "plan": {**_PLAN, "scope": "fullstack"},
                                "restored_paths": list(harness.files)})
    assert report["project"]["state"] == "failed"
    assert harness.calls == {("project_repair", path): 3}
    assert harness.files[path] == original


@pytest.mark.asyncio
async def test_project_repair_adds_verified_imports_without_model_calls():
    path = "tests/test_api.py"
    original = '"""验收测试。"""\nfrom __future__ import annotations\n\ndef test_result():\n    assert Decimal("1") == 1\n'
    statement = "from decimal import Decimal"

    async def invoke(*_args):
        pytest.fail("来源唯一的缺失导入应直接补齐")

    class ImportsHarness(_Harness):
        async def review(self, plan):
            result = await super().review(plan)
            if statement not in self.files[path]:
                result["issues"].append({"path": path, "message": "Decimal 未定义"})
                result["import_repairs"] = {path: [statement]}
                result["passed"] = False
            return result

    harness = ImportsHarness(invoke)
    harness.files = {name: _example_file(name) for task in _TASKS for name in task["files"]}
    harness.files[path] = original
    report = await harness.run({**_INPUT, "plan": {**_PLAN, "scope": "fullstack"},
                                "restored_paths": list(harness.files)})
    assert report["project"]["state"] == "passed"
    assert harness.files[path] == original.replace("import annotations\n", f"import annotations\n{statement}\n")
    assert not harness.calls


@pytest.mark.asyncio
async def test_script_advice_is_rejected_and_unambiguous_fenced_code_is_saved():
    source = "document.querySelector('#load').addEventListener('click', () => fetch('/api/tasks'));"

    async def invoke(stage, system, data):
        if stage == "project_planning":
            return json.dumps(_PLAN)
        path = data["path"]
        if path != "frontend/app.js":
            return _example_file(path)
        if "correction" not in data:
            return "你可以直接把上面的 JavaScript 复制到 `frontend/app.js` 中，保存即可。"
        assert "缺少可写入的实际源码" in data["correction"]
        return f"以下是修复后的脚本。\n\n```javascript\n{source}\n```\n\n将代码保存后刷新页面。"

    harness = _Harness(invoke)
    result = await harness.run()
    assert result["project"]["state"] == "passed"
    assert harness.files["frontend/app.js"] == source + "\n"
    assert harness.calls["project_file", "frontend/app.js"] == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("recovery", ["model", "backend", "none"])
async def test_missing_contract_does_not_trigger_readme_rework_or_spend_other_files_repair_attempts(recovery):
    contract, readme, package = "openapi.json", "README.md", "frontend/package.json"
    instructions = "# 启动\n\n进入 frontend 后运行 `npm run dev`。"
    runtime_contract = '{"openapi":"3.1.0","info":{"title":"任务服务","version":"1.0.0"},"paths":{"/api/tasks":{"get":{}}}}\n'
    plan = {**_PLAN, "tasks": [
        {"id": "contract", "role": "接口设计", "title": "建立接口约定", "brief": "提供启动说明与接口约定",
         "depends_on": [], "files": [contract, readme]},
        {**_TASKS[0], "depends_on": ["contract"]},
        {**_TASKS[1], "depends_on": ["contract", "api"], "files": [package, *_TASKS[1]["files"]]},
        {**_TASKS[2], "files": ["tests/test_api.py"]},
    ]}

    async def invoke(stage, system, data):
        if stage == "project_planning":
            return json.dumps(plan)
        path = data["path"]
        if path == contract and (stage == "project_file" or recovery != "model"):
            return "你好，我是工程助手。"
        if path == readme:
            assert stage == "project_file"
            return instructions
        if path != contract:
            assert stage == "project_file"
            if path == package and recovery != "model":
                assert any(contract in finding for finding in data["verification_findings"])
                assert contract not in data["committed_files"]
        return _example_file(path)

    class UnfinishedHarness(_Harness):
        async def review(self, plan):
            result = await super().review(plan)
            if package not in self.files and readme in self.files:
                result["issues"].append({"path": readme, "message": "README 中的 dev 命令没有在 package.json 声明"})
            if recovery == "backend" and len(result["issues"]) == 1 and result["issues"][0]["path"] == contract:
                result["verified_openapi"] = {"path": contract, "content": runtime_contract}
            return result

    harness = UnfinishedHarness(invoke)
    result = await harness.run()
    assert result["project"]["state"] == ("failed" if recovery == "none" else "passed")
    assert harness.files[readme] == instructions + "\n"
    assert set(harness.files) == {path for task in plan["tasks"] for path in task["files"]
                                  if recovery != "none" or path != contract}
    if recovery == "backend":
        assert harness.files[contract] == runtime_contract and harness.checks >= 4
    assert harness.calls["project_repair", contract] == (1 if recovery == "model" else 2)
    assert all(path == contract for stage, path in harness.calls if stage == "project_repair")
