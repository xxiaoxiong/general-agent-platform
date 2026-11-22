"""核对运行验收的任务交接和结果归属。"""

import asyncio
import json
import os
import time

import pytest

from app.core.config import settings
from app.services.agent_runtime import project_verifier_worker as worker
from app.services.agent_runtime.project_browser_contract import (
    BROWSER_CHECKS_PATH, browser_entry_source, browser_observed_source, browser_repair_problem, parse_browser_checks,
)
from app.services.agent_runtime.project_node_toolchain import dependency_ranges
from app.services.agent_runtime.project_verifier_protocol import MAX_JOB_BYTES, openapi_source, read_json, source_digest, write_json
from app.services.agent_runtime.team_project_verifier import merge_runtime_report, verify_runtime


async def _next_request(queue):
    async def wait():
        while not (requests := list(queue.glob("job-*/request.json"))):
            await asyncio.sleep(0.01)
        return requests[0]

    return await asyncio.wait_for(wait(), timeout=3)


@pytest.fixture
def queue(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "AGENT_PROJECT_VERIFIER_DIR", str(tmp_path))
    (tmp_path / "heartbeat").touch()
    return tmp_path


@pytest.mark.asyncio
@pytest.mark.parametrize("correct_digest", [True, False])
async def test_runtime_queue_binds_the_receipt_to_the_exact_sources(queue, correct_digest):
    sources = {"backend/app.py": "print('运行验收')\n", "README.md": "python backend/app.py\n"}
    digest = source_digest(sources)
    result = asyncio.create_task(verify_runtime(sources, "backend"))
    request = await _next_request(queue)
    payload = read_json(request, MAX_JOB_BYTES)
    assert payload["files"] == sources and payload["scope"] == "backend"
    assert payload["source_digest"] == digest and payload["deadline"] > time.time()
    response = {"source_digest": digest if correct_digest else "old", "checks": [
        {"id": "runtime.start", "status": "passed"},
    ], "issues": []}
    write_json(request.with_name("result.json"), response)
    report = await asyncio.wait_for(result, timeout=3)
    if correct_digest:
        assert report == response
    else:
        assert report["checks"][0]["id"] == "runtime.environment"
        assert report["checks"][0]["status"] == "failed"
    assert not list(queue.glob("job-*"))


@pytest.mark.asyncio
async def test_runtime_queue_bounds_worker_failure_and_cancellation(queue):
    result = asyncio.create_task(verify_runtime({"app.py": "print('待验收')"}, "backend"))
    await _next_request(queue)
    result.cancel()
    with pytest.raises(asyncio.CancelledError):
        await result
    assert not list(queue.glob("job-*"))

    expired = time.time() - 120
    os.utime(queue / "heartbeat", (expired, expired))
    report = await verify_runtime({"app.py": "print('待验收')"}, "backend")
    assert report["checks"][0]["status"] == "failed" and report["issues"] == []
    assert not list(queue.glob("job-*"))


def test_unsupported_dependency_is_reported_without_replacing_the_requested_stack(monkeypatch):
    files = {"backend/app.py": "from flask import Flask", "backend/requirements.txt": "flask==2.0.0\nrequests>=2\n"}
    monkeypatch.setattr(worker, "version", {"flask": "3.1.2", "requests": "2.32.5"}.__getitem__)
    entry = worker._entrypoint(files, "在项目根目录执行 `python backend/app.py`。")
    assert entry and entry.path == "backend/app.py"
    assert worker._unsupported_dependencies(entry, files) == ["flask==2.0.0（环境为 3.1.2）"]


def test_python_profile_is_used_only_when_it_matches_all_declared_versions(monkeypatch, tmp_path):
    profile = tmp_path / "python-versions"
    metadata = profile / "pydantic-2.8.2.dist-info"
    metadata.mkdir(parents=True)
    (metadata / "METADATA").write_text("Name: pydantic\nVersion: 2.8.2\n", encoding="utf-8")
    monkeypatch.setattr(worker, "_PYTHON_COMPATIBILITY", (profile,))
    monkeypatch.setattr(worker, "version", {"fastapi": "0.125.0", "pydantic": "2.14.4"}.__getitem__)
    job = worker.Job(tmp_path / "job", time.monotonic() + 60, tmp_path)
    files = {"backend/app.py": "app = FastAPI()", "backend/requirements.txt": "fastapi==0.125.0\npydantic==2.8.2\n"}
    entry = worker._entrypoint(files, "uvicorn app:app --port 8000")
    assert worker._prepare_python(job, entry, files) == []
    assert job.env["PYTHONPATH"].split(os.pathsep)[0] == str(profile)
    assert job.python_versions == {"pydantic": "2.8.2"}
    incompatible = {**files, "backend/requirements.txt": "fastapi==0.90.0\npydantic==2.8.2\n"}
    second_job = worker.Job(tmp_path / "job", time.monotonic() + 60, tmp_path)
    assert worker._prepare_python(second_job, entry, incompatible) == [
        "fastapi==0.90.0（环境为 0.125.0）", "pydantic==2.8.2（环境为 2.14.4）",
    ]
    assert not second_job.python_versions


def test_vite_dependencies_keep_manifest_ranges_and_direct_lock_versions():
    manifest = {"dependencies": {"vue": "^3.5.0"}, "devDependencies": {"vite": "^7.0.0"}}
    lock = {"packages": {"node_modules/vue": {"version": "3.5.38"}, "node_modules/typescript": {"version": "5.9.3"}}}
    assert dependency_ranges(manifest, lock) == {"vue": ["^3.5.0", "3.5.38"], "vite": ["^7.0.0"]}


def test_incompatible_vite_dependency_does_not_fall_back_to_raw_html(monkeypatch, tmp_path):
    files = {"frontend/index.html": '<main>需要运行的页面</main><script type="module" src="/main.ts"></script>',
             "frontend/package.json": '{"scripts":{"build":"vite build"},"devDependencies":{"vite":"^5.0.0"}}'}
    observed = []

    def provide(_cwd, manifest, lock, _group):
        observed.append(dependency_ranges(manifest, lock))
        return ["vite ^5.0.0（环境为 7.3.8）"]

    monkeypatch.setattr(worker, "provide_dependencies", provide)
    job = worker.Job(tmp_path / "job", time.monotonic() + 60, tmp_path)
    site = worker._start_site(job, files, "frontend", "README.md")
    assert observed == [{"vite": ["^5.0.0"]}]
    assert site.web_port is None and not job.issues
    assert [(check["id"], check["status"]) for check in job.checks] == [
        ("runtime.web_build", "skipped"), ("runtime.web", "skipped"),
    ]
    assert "vite ^5.0.0" in job.checks[0]["detail"]


def test_production_web_probe_checks_mime_type_at_the_built_mount(monkeypatch, tmp_path):
    root = "/inspection/"
    responses = {"/": (302, "", b"", root), root: (200, "text/html", b'<script src="./assets/app.js"></script>', ""),
                 root + "assets/app.js": (200, "text/html", b"<main>SPA fallback</main>", "")}
    monkeypatch.setattr(worker, "_http", lambda _port, path: responses[path])
    job = worker.Job(tmp_path / "job", time.monotonic() + 60, tmp_path)
    monkeypatch.setattr(job, "tick", lambda: None)
    assert not worker._probe_frontend(job, "frontend/index.html", "", 5173, built=True)
    assert job.issues == [{"path": "frontend/index.html", "message": "页面资源 /inspection/assets/app.js 实际 HTTP 200，类型 text/html"}]
    responses[root + "assets/app.js"] = (200, "text/javascript", b"function start() {}", "")
    assert worker._probe_frontend(job, "frontend/index.html", "", 5173, built=True)
    assert job.checks[-1]["status"] == "passed"


def test_vite_build_diagnostic_targets_authored_component_and_keeps_explicit_port():
    files = {"frontend/index.html": "<main></main>", "frontend/App.vue": "<template><main/></template>",
             "frontend/package.json": '{"scripts":{"build":"vue-tsc && vite build"}}', "frontend/tsconfig.json": "{}",
             "backend/app.py": "app = FastAPI()"}
    project = worker._vite_project("frontend/index.html", files)
    output = "\n> vue-tsc -p tsconfig.json && vite build\nApp.vue(12,1): error TS2345: wrong type\n"
    assert worker._build_failure(output, project, files) == ("frontend/App.vue", "App.vue(12,1): error TS2345: wrong type")
    readme = "在 backend/ 运行 `uvicorn app:app --port 8042`，然后在 frontend/ 执行 `npm run preview -- --port 4175`。"
    assert worker._entrypoint(files, readme).argv[-2:] == ("--port", "8042")
    assert worker._vite_port(project, readme, 8042) == 4175


def test_observed_text_control_repairs_value_assertion_without_weakening_it():
    contract = {"version": 1, "scenarios": [{"name": "检查发布状态", "steps": [
        {"action": "open", "path": "/"}, {"action": "click", "selector": "#publish"},
        {"action": "expect_value", "selector": "#status", "value": "已发布"},
        {"action": "expect_value", "selector": "#date", "value": "2030-01-01"},
    ]}]}
    finding = "tests/browser-checks.json 「检查发布状态」第 3 步。匹配到文本元素，请使用 expect_text"
    steps = parse_browser_checks(browser_observed_source(json.dumps(contract), [finding]))[0]["steps"]
    assert steps[2] == {"action": "expect_text", "selector": "#status", "value": "已发布"}
    assert steps[3] == contract["scenarios"][0]["steps"][3]


def test_only_the_vite_reference_proven_missing_by_typescript_can_be_removed():
    path = "frontend/tsconfig.json"
    source = '{\n  "compilerOptions": {"strict": true},\n  "references": [{"path":"./tsconfig.node.json"}, {"path":"./tsconfig.shared.json"}]\n}'
    files = {path: source, "frontend/vite.config.ts": "export default {};", "frontend/tsconfig.shared.json": "{}"}
    digest = source_digest(files)
    verification = {"source_digest": digest, "passed": True, "checks": [], "issues": []}
    detail = "frontend/tsconfig.json 构建退出码 2 · tsconfig.json(3,18): error TS6053: File 'frontend/tsconfig.node.json' not found."
    receipt = {"source_digest": digest, "checks": [{"id": "runtime.web_build", "status": "failed"}],
               "issues": [{"path": path, "message": detail}]}
    report = merge_runtime_report(verification, receipt, files)
    edits = report["line_repairs"][path]
    previous, repaired = next(iter(edits.items()))
    assert json.loads(source.replace(previous, repaired)) == {
        "compilerOptions": {"strict": True}, "references": [{"path": "./tsconfig.shared.json"}],
    }
    receipt["issues"][0]["message"] = detail.replace("tsconfig.node.json", "tsconfig.shared.json")
    assert not merge_runtime_report(verification, receipt, files)["line_repairs"]


def test_pytest_scope_failure_targets_the_fixture_without_changing_business_assertions():
    path = "tests/test_e2e.py"
    source = ('import pytest\n\n@pytest.fixture(scope="module")\ndef client(tmp_path):\n'
              '    return tmp_path\n\ndef test_record(client):\n    assert client.is_dir()\n')
    files = {path: source, "backend/main.py": "app = FastAPI()"}
    suite = worker.Invocation(path, "tests", ("python", "-m", "pytest", "test_e2e.py"))
    error = ('ScopeMismatch: You tried to access the function scoped fixture tmp_path with a module scoped request object. '
             'Requesting fixture stack:\ntest_e2e.py:3:  def client(tmp_path)\nRequested fixture:\n')
    issues = worker._fixture_scope_failures(error * 11, suite, files)
    assert len(issues) == 1 and issues[0]["path"] == path
    digest = source_digest(files)
    verification = {"source_digest": digest, "passed": True, "checks": [], "issues": []}
    receipt = {"source_digest": digest, "checks": [{"id": "runtime.tests.1", "status": "failed"}], "issues": issues}
    repairs = merge_runtime_report(verification, receipt, files)["line_repairs"]
    line, replacement = next(iter(repairs[path].items()))
    assert source.replace(line, replacement) == source.replace('scope="module"', 'scope="function"')


def test_api_probes_require_success_for_public_routes_and_auth_for_protected_routes():
    files = {"openapi.json": '''{"paths": {
      "/api/public": {"get": {"responses": {"200": {"content": {"application/json": {}}}, "404": {}}}},
      "/api/user": {"get": {"security": [{"bearer": []}], "responses": {"200": {}, "401": {}}}},
      "/api/search": {"parameters": [{"required": true}], "get": {"responses": {"200": {}}}}
    }}'''}
    assert worker._api_probes(files) == [("/api/public", {200}, {200}), ("/api/user", {401}, set())]


@pytest.mark.parametrize("media,valid", [("application/json", True), ("text/html", False)])
def test_export_openapi_reads_the_actual_backend_and_requires_json(media, valid, monkeypatch, tmp_path):
    spec = {"openapi": "3.1.0", "info": {"title": "设备台账", "version": "1.2.0"},
            "paths": {"/api/assets": {"get": {"responses": {"200": {"description": "设备记录"}}}}}}
    files = {"README.md": "uvicorn app:app --port 8042", "backend/app.py": "app = FastAPI()"}
    job = worker.Job(tmp_path / "job", time.monotonic() + 60, tmp_path)
    monkeypatch.setattr(job, "tick", lambda: None)

    def start(_job, submitted, scope, readme):
        assert submitted == files and scope == "backend" and readme == "README.md"
        return worker.Site(None, 8042, None, None, False)

    def http(port, route):
        assert (port, route) == (8042, "/openapi.json")
        return 200, media, json.dumps(spec).encode(), ""

    monkeypatch.setattr(worker, "_start_site", start)
    monkeypatch.setattr(worker, "_http", http)
    result = worker._capture_openapi(job, files)
    assert (json.loads(openapi_source(result)) if result else None) == (spec if valid else None)
    assert openapi_source({**spec, "paths": {}}) is None


def test_standalone_and_collected_tests_are_distinguished_and_failures_point_to_application_files():
    files = {"backend/test_app.py": "def test_health():\n    assert True\n",
             "e2e/test_flow.py": 'if __name__ == "__main__":\n    main()\n'}
    tests, scripts = worker._test_commands(files)
    assert len(tests) == 2 and scripts == ["e2e/test_flow.py"]
    suite, standalone = tests
    assert (suite.cwd, suite.path) == ("backend", "backend/test_app.py")
    assert standalone.argv[-1] == "test_flow.py"
    backend = worker.Invocation("backend/app.py", "backend", ("python", "app.py"))
    assert worker._test_failure_path("FAIL: 前端应展示记账首页", standalone, backend, "frontend/index.html") == "frontend/index.html"
    assert worker._test_failure_path("FAIL: 创建交易返回 500", standalone, backend, "frontend/index.html") == "backend/app.py"
    assert worker._test_failure_path("ImportError: test support missing", standalone, backend, "frontend/index.html") == standalone.path


def test_known_test_command_updates_only_the_line_and_retains_pytest_for_collected_cases():
    files = {"backend/test_app.py": "def test_http():\n    assert True\n",
             "e2e/test_flow.py": 'if __name__ == "__main__":\n    main()\n'}
    _, scripts = worker._test_commands(files)
    readme = "pytest test_app.py -v\r\npython3 -m pytest -q e2e/test_flow.py\r\n不能使用 pytest test_flow.py。"
    assert worker._standalone_test_edits(readme, files, scripts) == {
        "python3 -m pytest -q e2e/test_flow.py": "python3 e2e/test_flow.py",
    }
    ambiguous = {**files, "another/test_flow.py": files["backend/test_app.py"]}
    assert worker._standalone_test_edits("pytest test_flow.py", ambiguous, scripts) == {}


def test_failed_test_evidence_includes_the_assertion_and_skipped_suites_do_not_pass():
    detail = "FAIL: 首屏应包含应用名称 · 1 个检查未通过"
    source = 'import requests\ncheck("example" in response.text, "首屏应包含应用名称")\n'
    assert worker._assertion_location(detail, source).startswith('第 2 行 check("example" in response.text')
    pytest_run = worker.Invocation("test_app.py", ".", ("python", "-m", "pytest", "test_app.py"))
    assert worker._run_test_status(pytest_run, 0, "1 skipped in 0.01s") == "skipped"
    assert worker._run_test_status(pytest_run, 0, "1 passed, 1 skipped in 0.01s") == "passed"


def test_http_suites_that_start_the_same_backend_are_detected_separately():
    entry = worker.Invocation("backend/app.py", "backend", ("python", "app.py"))
    suite = worker.Invocation("tests/e2e_test.py", "tests", ("python", "-m", "pytest", "e2e_test.py"))
    assert worker._suite_owns_backend(suite, entry, {suite.path: 'subprocess.Popen([sys.executable, "app.py"])'})
    assert not worker._suite_owns_backend(suite, entry, {suite.path: 'requests.get("http://localhost:5000/api/tasks")'})
    assert not worker._suite_owns_backend(suite, entry, {suite.path: 'subprocess.Popen([sys.executable, "file_helper.py"])'})


@pytest.mark.parametrize("step, problem", [
    ({"action": "open", "path": "//example.invalid"}, "应用内"),
    ({"action": "click", "selector": "#save >> text=next"}, "CSS"),
    ({"action": "execute", "source": "document.body"}, "不支持"),
    ({"action": "click", "selector": "#save", "confirm": 1}, "布尔值"),
])
def test_browser_scenarios_only_accept_typed_actions_in_the_application(step, problem):
    checks = {"version": 1, "scenarios": [{"name": "保存后可查", "steps": [
        {"action": "open", "path": "/"}, step, {"action": "expect_visible", "selector": "#result"},
    ]}]}
    with pytest.raises(ValueError, match=problem):
        parse_browser_checks(json.dumps(checks))


def test_browser_scenario_requires_a_visible_state_assertion_and_a_user_action():
    click = {"action": "click", "selector": "#save"}
    visible = {"action": "expect_text", "selector": "#result", "value": "已保存"}
    check = {"version": 1, "scenarios": [{"name": "保存后可查", "steps": [
        {"action": "open", "path": "/"}, click, visible,
    ]}]}
    assert parse_browser_checks(json.dumps(check)) == check["scenarios"]
    for changed in ([check["scenarios"][0]["steps"][0], visible, visible], [
        {"action": "open", "path": "/"}, click,
        {"action": "expect_response", "path": "/api/tasks", "method": "POST", "status": 201},
    ]):
        stub = {**check, "scenarios": [{"name": "检查", "steps": changed}]}
        with pytest.raises(ValueError, match="校验结果"):
            parse_browser_checks(json.dumps(stub))


def test_browser_failure_routes_to_real_assets_api_or_the_scenario_itself():
    files = {"frontend/index.html": '<script src="./app.js"></script>', "frontend/app.js": "", BROWSER_CHECKS_PATH: "{}"}
    app = worker.Invocation("backend/app.py", "backend", ("python", "app.py"))
    site = worker.Site(app, 8000, "frontend/index.html", 3000, False)
    assert worker._browser_issue_file(site, files, {"kind": "script", "path": "/app.js"}) == "frontend/app.js"
    assert worker._browser_issue_file(site, files, {"kind": "action", "path": "/"}) == "frontend/app.js"
    assert worker._browser_issue_file(site, files, {"kind": "api"}) == app.path
    assert worker._browser_issue_file(site, files, {"kind": "coverage"}) == BROWSER_CHECKS_PATH
    assert worker._browser_issue_file(site, files, {"kind": "scenario_input"}) == BROWSER_CHECKS_PATH
    assert worker._browser_issue_file(site, files, {"kind": "missing_control", "selector": "#wrong-id"}) == BROWSER_CHECKS_PATH


def test_browser_scenario_recovers_annotations_and_equivalent_step_fields():
    raw = {"version": 1, "scenarios": [{"name": "重开工作台", "steps": [
        {"action": "open", "path": "/", "description": "初始页面"},
        {"action": "click", "target": "#save", "value": ""},
        {"action": "reload", "path": "/", "selector": None},
        {"action": "expect_text", "selector": "#result", "text": "已保存"},
    ]}]}
    steps = parse_browser_checks(json.dumps(raw))[0]["steps"]
    assert steps == [
        {"action": "open", "path": "/"}, {"action": "click", "selector": "#save"}, {"action": "reload"},
        {"action": "expect_text", "selector": "#result", "value": "已保存"},
    ]
    raw["scenarios"][0]["steps"][2]["path"] = "/other"
    with pytest.raises(ValueError, match="reload 包含未知字段 path"):
        parse_browser_checks(json.dumps(raw))


def test_a_scenario_repair_keeps_other_user_flows_and_expectations():
    def scenario(name, selector):
        return {"name": name, "steps": [
            {"action": "open", "path": "/"}, {"action": "click", "selector": "#choose"},
            {"action": "expect_visible", "selector": selector},
        ]}

    def source(*scenarios):
        return json.dumps({"version": 1, "scenarios": scenarios}, ensure_ascii=False)

    valid = scenario("保存后查看", "#taskList")
    original = source(scenario("无效输入", "#wrong-id"), valid)
    corrected = scenario("无效输入", "#error-notice")
    hint = ["tests/browser-checks.json 「无效输入」第 3 步。当前页面缺少该元素"]
    assert browser_repair_problem(original, source(corrected, valid), hint) == ""
    assert "保存后查看" in browser_repair_problem(original, source(corrected), hint)


def test_browser_entry_normalizes_only_a_known_mounted_page_at_scenario_start():
    source = json.dumps({"version": 1, "scenarios": [{"name": "浏览页面", "steps": [
        {"action": "open", "path": "/frontend/index.html"},
        {"action": "click", "selector": "#visit"},
        {"action": "open", "path": "/details"},
        {"action": "expect_visible", "selector": "#settings"},
    ]}]})
    assert browser_entry_source(source, ["README.md"]) == source
    normalized = parse_browser_checks(browser_entry_source(source, ["frontend/index.html"]))
    assert normalized[0]["steps"][0]["path"] == "/"
    assert normalized[0]["steps"][2]["path"] == "/details"


def test_native_validation_and_confirmation_repairs_keep_the_intended_user_outcomes():
    actions = [
        {"action": "open", "path": "/"},
        {"action": "click", "selector": "#submit"},
        {"action": "expect_visible", "selector": "#errorBanner"},
        {"action": "click", "selector": "#delete"},
        {"action": "expect_hidden", "selector": "#record"},
    ]
    source = json.dumps({"version": 1, "scenarios": [{"name": "无效输入和删除", "steps": actions}]}, ensure_ascii=False)
    hints = [
        "tests/browser-checks.json 「无效输入和删除」第 3 步。场景输入未满足表单要求 #title",
        "tests/browser-checks.json 「无效输入和删除」第 5 步。上一次点击的确认弹窗已取消",
    ]
    updated = parse_browser_checks(browser_observed_source(source, hints))[0]["steps"]
    assert updated[2] == {"action": "expect_invalid", "selector": "#title"}
    assert updated[3] == {"action": "click", "selector": "#delete", "confirm": True}
    assert updated[4] == actions[4]

    renamed = source.replace("无效输入和删除", "保存数据")
    assert browser_observed_source(renamed, [hints[0].replace("无效输入和删除", "保存数据")]) == renamed


def test_failed_restart_fixture_line_can_be_fixed_with_all_assertions_preserved():
    path = "tests/test_process.py"
    source = '''import subprocess
import pytest

def start():
    return subprocess.Popen(["python", "app.py"])

def stop(process):
    process.terminate()

@pytest.fixture(scope="module")
def api():
    child = start()
    yield
    stop(child)

def test_restart(api):
    stop(api)
    assert has_persisted_data()
'''
    files = {path: source}
    digest = source_digest(files)
    verification = {"source_digest": digest, "passed": True, "checks": [], "issues": []}
    receipt = {"source_digest": digest, "checks": [{"id": "runtime.tests.1", "status": "failed"}], "issues": [
        {"path": path, "message": "AttributeError: 'NoneType' object has no attribute 'terminate'"},
    ]}
    merged = merge_runtime_report(verification, receipt, files)
    assert merged["line_repairs"] == {path: {"    yield": "    yield child"}}
    assert not merged["passed"]
    unrelated = {path: source.replace("stop(api)", "check_health()")}
    assert not merge_runtime_report(verification, receipt, unrelated)["line_repairs"]
