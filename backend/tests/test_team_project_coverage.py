"""核对需求复核与实际测试、源码快照的对应关系。"""

import json

from app.services.agent_runtime.events import RuntimeEvent, event_storage_payload
from app.services.agent_runtime.project_browser_contract import BROWSER_CHECKS_PATH
from app.services.agent_runtime.project_verifier_protocol import source_digest
from app.services.agent_runtime.team_project_coverage import acceptance_criteria, coverage_input, coverage_report, merge_coverage_report
from app.services.agent_runtime.team_project_graph import _repair_jobs


def test_mixed_project_creation_and_business_requirements_need_business_evidence():
    rows = acceptance_criteria("帮我构建备件系统，刷新后可以看到明细。需要交付源码、启动说明与测试。前端必须使用 Vue 3。", [])
    assert [row["kind"] for row in rows] == ["behavior", "delivery", "delivery"]
    files = {"README.md": "启动项目。"}
    checks = [{"id": "runtime.start", "status": "passed"}]
    report = _verify(rows[0], files, checks, "README.md", "启动项目。")
    assert report["criteria"][0]["status"] == "unverified"


def test_build_request_with_business_behavior_cannot_pass_as_delivery():
    rows = acceptance_criteria(
        "构建一个可在刷新后显示领用明细的备件系统。"
        "构建并交付可直接运行的前后端分离项目「设备保养与备件工作台」，文件必须可以从对话下载。"
        "帮我构建一个足够复杂的前后端分离项目，你随意发挥。"
        "交付源码 ZIP、启动与测试说明、OpenAPI 和可执行测试。"
        "提供源码，同时支持按备件筛选。",
        [],
    )
    assert [row["kind"] for row in rows] == ["behavior", "delivery", "delivery", "delivery", "behavior"]
    report = _verify(rows[0], {"README.md": "备件系统"}, [{"id": "runtime.start", "status": "passed"}],
                     "README.md", "备件系统")
    assert report["criteria"][0]["status"] == "unverified"


def _verify(criterion, files, checks, path, excerpt):
    row = {"id": criterion["id"], "status": "verified", "detail": "验证结果",
           "evidence": [{"path": path, "excerpt": excerpt}], "checks": [checks[0]["id"]], "files": []}
    return coverage_report(json.dumps({"criteria": [row]}), [criterion], files, checks, source_digest(files))


def test_browser_evidence_accepts_the_real_assertion_and_rejects_an_unrelated_element():
    criterion = {"id": "u1", "text": "通过浏览器验证删除工单", "kind": "browser"}
    assertion = {"action": "expect_text", "selector": "#result", "value": "已删除"}
    steps = [{"action": "open", "path": "/"}, {"action": "click", "selector": "#delete"},
             {"action": "expect_visible", "selector": "#home"}, assertion]
    files = {BROWSER_CHECKS_PATH: json.dumps({"version": 1, "scenarios": [{"name": "删除工单", "steps": steps}]}, ensure_ascii=False)}
    checks = [{"id": "runtime.browser.flow.1", "status": "passed"}]
    assert _verify(criterion, files, checks, BROWSER_CHECKS_PATH, "#home")["criteria"][0]["status"] == "unverified"
    assert _verify(criterion, files, checks, BROWSER_CHECKS_PATH, json.dumps(assertion, ensure_ascii=False))[
        "criteria"][0]["status"] == "verified"


def test_python_evidence_is_limited_to_test_assertions_in_the_executed_suite():
    criterion = {"id": "u1", "text": "备件可以领取", "kind": "behavior"}
    path = "tests/test_orders.py"
    files = {path: (
        "def helper():\n    assert picked == 3\n\n"
        "class Helpers:\n    def test_pickup_requires_permission(self):\n        assert response.status_code == 403\n\n"
        "def test_pickup():\n    if False:\n        assert response.status_code == 404\n"
        "    assert response.status_code == 200\n"
    )}
    checks = [{"id": "runtime.tests.1", "status": "passed", "detail": "1 passed", "files": [path],
               "test_cases": [f"{path}::test_pickup"]}]
    assert _verify(criterion, files, checks, path, "assert picked == 3")["criteria"][0]["status"] == "unverified"
    assert _verify(criterion, files, checks, path, "assert response.status_code == 403")["criteria"][0]["status"] == "unverified"
    assert _verify(criterion, files, checks, path, "assert response.status_code == 404")["criteria"][0]["status"] == "unverified"
    report = _verify(criterion, files, checks, path, "assert response.status_code == 200")
    assert report["criteria"][0]["status"] == "verified"
    current = {"source_digest": source_digest(files), "passed": True, "issues": [],
               "checks": [{**checks[0], "detail": "1 passed, 1 skipped", "test_cases": []}]}
    assert not merge_coverage_report(current, report, files)["passed"]


def test_missing_requirements_and_new_source_cannot_reuse_a_previous_green_verdict():
    path = "backend/main.py"
    files = {path: "app = FastAPI()"}
    criteria = acceptance_criteria("后端使用 FastAPI。执行登记后可以查询备件。", [])
    checks = [{"id": "runtime.start", "status": "passed"}]
    raw = json.dumps({"criteria": [{"id": "u1", "status": "verified"}]})
    report = coverage_report(raw, criteria, files, checks, source_digest(files))
    assert not report["received"] and all(item["status"] == "unverified" for item in report["criteria"])
    current = {"source_digest": source_digest(files), "passed": True, "issues": [], "checks": checks}
    assert not merge_coverage_report(current, report, {path: "app = Flask()"})["passed"]


def test_partial_reviews_keep_grounded_claims_and_recheck_old_proof_on_retry():
    front, back, readme = "frontend/app.js", "backend/main.py", "README.md"
    files = {front: "const parts = await api(`/orders/${id}/parts`)", back: "app = FastAPI()", readme: "启动说明"}
    criteria, prompt = coverage_input("可以查询备件。后端使用 FastAPI。", {"features": [], "tasks": [
        {"role": "前端工程师", "files": [front]}, {"role": "后端工程师", "files": [back]},
    ]}, files, [])
    assert json.loads(prompt)["source_owners"] == {front: "前端工程师", back: "后端工程师"}
    digest = source_digest(files)
    checks = [{"id": "runtime.start", "status": "passed"}]
    gap = {"id": "u1", "status": "gap", "detail": "备件请求缺少对应接口。",
           "evidence": [{"path": front, "excerpt": files[front]}], "files": [readme]}
    passed = {"id": "u2", "status": "verified", "detail": "后端按要求启动。",
              "evidence": [{"path": back, "excerpt": files[back]}], "checks": ["runtime.start"]}

    def report(rows, checks=checks, previous=None):
        return coverage_report(json.dumps({"criteria": rows}), criteria, files, checks, digest, previous=previous)

    first = report([passed, gap, gap, {"id": "unexpected", "status": "verified", "detail": "无关条目。"}])
    assert not first["received"] and first["reviewed_ids"] == ["u2"]
    assert [row["status"] for row in first["criteria"]] == ["unverified", "verified"]
    retry = report([gap], previous=first)
    assert retry["received"]
    assert retry["criteria"][0]["files"] == [front]
    assert retry["criteria"][1]["status"] == "verified"
    assert report([gap], checks=[], previous=first)["criteria"][1]["status"] == "unverified"
    assert report([gap], previous={**first, "source_digest": "other"})["criteria"][1]["status"] == "unverified"
    revised = {**passed, "status": "unverified", "detail": "启动证据不足。"}
    assert report([revised], previous=first)["criteria"][1]["detail"] == "启动证据不足。"


def test_browser_gap_routes_to_an_anchored_scenario_only_after_page_load():
    path = BROWSER_CHECKS_PATH
    files = {path: '{"version":1,"scenarios":[]}'}
    criterion = {"id": "u1", "kind": "browser", "text": "通过浏览器验证备件领取"}
    unverified = {"id": "u1", "status": "unverified", "detail": "场景缺少领取操作后的断言。",
                  "evidence": [{"path": path, "excerpt": '"scenarios":[]'}]}
    raw = json.dumps({"criteria": [unverified]})
    digest = source_digest(files)
    loaded = {"id": "runtime.browser", "status": "passed", "detail": "首页已经加载。"}
    skipped = {**loaded, "status": "skipped", "detail": "页面依赖无法离线访问的资源。"}
    report = coverage_report(raw, [criterion], files, [loaded], digest)
    verification = {"source_digest": digest, "passed": True, "issues": [], "checks": [loaded]}
    merged = merge_coverage_report(verification, report, files)
    assert not merged["passed"] and merged["issues"][0]["path"] == path
    assert not merge_coverage_report({**verification, "checks": [skipped]}, report, files)["issues"]
    rejected = coverage_report("", [criterion], files, [skipped], digest)["criteria"][0]
    assert rejected["status"] == "unverified" and skipped["detail"] in rejected["detail"]


def test_new_requirement_can_repair_after_technical_attempts_and_precedes_downstream_scenarios():
    frontend, qa, browser = "frontend/app.js", "tests/test_api.py", BROWSER_CHECKS_PATH
    state = {"written": {frontend: "", qa: "", browser: ""}, "repair_attempts": {frontend: 2}, "plan": {"tasks": [
        {"id": "web", "depends_on": [], "files": [frontend]},
        {"id": "qa", "depends_on": ["web"], "files": [qa]},
        {"id": "browser", "depends_on": ["qa"], "files": [browser]},
    ]}}
    issues = [{"path": path, "criterion": "u4", "message": "删除功能及其执行断言缺失"} for path in (frontend, browser)]
    jobs = _repair_jobs(state, issues, {}, {}, {})
    assert [job["path"] for job in jobs] == [frontend]
    assert jobs[0]["requirement_keys"] == ["u4"]


def test_project_delivery_replay_preserves_requirements_beyond_regular_event_limit():
    checks = [{"id": f"coverage.u{index}", "status": "passed"} for index in range(26)]
    checks[-1].update(status="failed", detail="第五个核心流程尚未核验。" * 70)
    payload = event_storage_payload(RuntimeEvent(type="project.delivery", run_id="test", data={"checks": checks}))
    assert len(payload["checks"]) == len(checks)
    assert payload["checks"][-1]["status"] == "failed"
    assert payload["checks"][-1]["detail"] == checks[-1]["detail"]
