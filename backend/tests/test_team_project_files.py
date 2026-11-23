"""验证团队工程只发布真实、隔离、可核查的源文件。"""

import json
import os
import shutil
from pathlib import Path
from zipfile import ZipFile

import pytest

from app.api.v2.artifacts import parse_artifact_id
from app.core.config import settings
from app.services.agent_runtime.team_project_files import (
    ProjectWorkspace,
    ProjectWorkspaceError,
    parse_project_source,
    source_repair_problem,
    source_shape_problem,
    validate_project_path,
    validate_project_paths,
    vite_vue_entry_repairs,
)
from app.services.agent_runtime.team_project_verifier import merge_runtime_report


_BACKEND = '''from http.server import BaseHTTPRequestHandler, HTTPServer

class Server(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/api/tasks":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'[]')
        else:
            self.send_error(404)

if __name__ == "__main__":
    HTTPServer(("127.0.0.1", 8000), Server).serve_forever()
'''
_FILES = {
    "README.md": "# 团队任务演示\n\n在项目根目录运行 `python backend/server.py`。\n",
    "backend/server.py": _BACKEND,
    "frontend/index.html": '<main><button id="refresh">刷新</button><div id="tasks"></div></main><script type="module" src="./app.js"></script>',
    "frontend/app.js": ('document.querySelector("#refresh").addEventListener("click", async () => {\n'
                        '  const response = await fetch("/api/tasks");\n'
                        '  document.querySelector("#tasks").textContent = JSON.stringify(await response.json());\n});'),
    "openapi.json": json.dumps({"openapi": "3.1.0", "paths": {"/api/tasks": {"get": {"responses": {"200": {"description": "任务"}}}}}}),
    "tests/test_api.py": 'from urllib.request import urlopen\n\ndef test_tasks():\n    assert urlopen("http://127.0.0.1:8000/api/tasks").status == 200\n',
}


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "AGENT_FILES_ROOT", str(tmp_path))
    return ProjectWorkspace(11, 12, 13, "run-1"), tmp_path


def _archive(root: Path, delivery: dict) -> Path:
    session, path = parse_artifact_id(delivery["download_url"].rsplit("/", 1)[-1])
    assert session == 13
    return root / "11" / "12" / "13" / path


def test_real_files_and_repeatable_zip(project):
    workspace, root = project
    evidence = workspace.write_files(_FILES, allowed=_FILES)
    assert len(evidence) == len(_FILES)
    assert all(item["size"] > 0 and len(item["sha256"]) == 64 for item in evidence)
    assert workspace.inspect() == _FILES
    assert workspace.read_file("backend/server.py") == _BACKEND

    report = workspace.verify(expected=_FILES)
    assert report["passed"] and report["scope"] == "static"
    assert {check["id"]: check["status"] for check in report["checks"]}["runtime"] == "skipped"
    delivery = workspace.publish("示例项目", report)
    assert delivery["state"] == "passed" and delivery["file_count"] == len(_FILES)
    path = _archive(root, delivery)
    previous = path.read_bytes()
    assert delivery["size"] == len(previous)
    with ZipFile(path) as archive:
        assert set(archive.namelist()) == {*_FILES, "verification.json"}
        assert archive.read("frontend/app.js").decode() == _FILES["frontend/app.js"]
        assert json.loads(archive.read("verification.json"))["source_digest"] == report["source_digest"]
        assert all(not ((item.external_attr >> 16) & 0o111) for item in archive.infolist())
    assert workspace.publish("示例项目", report) == delivery
    assert path.read_bytes() == previous
    assert not ProjectWorkspace(11, 12, 13, "run-2").inspect()
    assert not ProjectWorkspace(14, 12, 13, "run-1").inspect()


def test_missing_project_without_source_has_no_download(project):
    workspace, _ = project
    report = workspace.verify(expected=("README.md",))
    assert not report["passed"]
    assert not workspace.publish("空项目", report)["download_url"]


def test_zip_keeps_failed_runtime_evidence_bound_to_the_source_snapshot(project):
    workspace, root = project
    workspace.write_files(_FILES, allowed=_FILES)
    report = workspace.verify(expected=_FILES)
    receipt = {"source_digest": report["source_digest"], "checks": [
        {"id": "runtime.start", "status": "passed", "detail": "已监听端口 8000"},
        {"id": "runtime.tests.1", "status": "failed", "detail": "首页交互测试未通过"},
    ], "issues": [{"path": "frontend/index.html", "message": "首页交互测试未通过"},
                  {"path": "outside.py", "message": "无法归入交付源码"}]}

    verification = merge_runtime_report(report, receipt, _FILES)
    delivery = workspace.publish("离线验收草稿", verification)
    assert delivery["state"] == "failed" and delivery["verification_scope"] == "sandbox"
    assert delivery["issues"] == receipt["issues"][:1]
    with ZipFile(_archive(root, delivery)) as archive:
        published = json.loads(archive.read("verification.json"))
        assert published["checks"] == delivery["checks"]
        assert published["source_digest"] == receipt["source_digest"]
    workspace.write_files({"README.md": _FILES["README.md"] + "\n附加说明。\n"}, allowed=_FILES)
    with pytest.raises(ProjectWorkspaceError, match="重新验收"):
        workspace.publish("陈旧的离线验收报告", verification)


def test_partial_project_is_draft_and_requires_current_verified_snapshot(project):
    workspace, root = project
    files = {"README.md": _FILES["README.md"], "backend/server.py": "def broken(\n"}
    workspace.write_files(files, allowed=_FILES)
    report = workspace.verify(expected=_FILES)
    assert not report["passed"]
    assert {item["path"] for item in report["issues"]} >= {"backend/server.py", "openapi.json"}
    delivery = workspace.publish("未通过的项目", {**report, "passed": True})
    assert delivery["state"] == "failed"
    with ZipFile(_archive(root, delivery)) as archive:
        assert not json.loads(archive.read("verification.json"))["passed"]
    workspace.write_files({"backend/server.py": _BACKEND}, allowed=_FILES)
    with pytest.raises(ProjectWorkspaceError, match="重新验收"):
        workspace.publish("陈旧验收", report)


def test_route_methods_missing_imports_and_vacuous_assertions_fail(project):
    workspace, _ = project
    files = {
        **_FILES,
        "frontend/app.js": 'import "./missing.js";\n' + _FILES["frontend/app.js"],
        "openapi.json": json.dumps({"openapi": "3.1.0", "paths": {"/api/tasks": {"post": {}}}}),
        "tests/test_api.py": "def test_tasks():\n    assert 1 == 1\n",
    }
    workspace.write_files(files, allowed=_FILES)
    report = workspace.verify(expected=_FILES)
    results = {item["id"]: item for item in report["checks"]}
    assert not report["passed"]
    assert "missing.js" in results["local_imports"]["detail"]
    assert "POST" in results["api_contract"]["detail"]
    assert results["test_sources"]["status"] == "failed"


def test_json_in_an_inline_event_attribute_is_reported_as_broken_interaction(project):
    workspace, _ = project
    path = "frontend/app.js"
    source = _FILES[path] + '\nconst row = task => `<button onclick="editTask(${JSON.stringify(task)})">编辑</button>`;\n'
    workspace.write_files({**_FILES, path: source}, allowed=_FILES)
    verification = workspace.verify(expected=_FILES)
    assert {check["id"]: check["status"] for check in verification["checks"]}["frontend_implementation"] == "failed"
    assert any(row["path"] == path and "引号会截断事件" in row["message"] for row in verification["issues"])
    for old, replacement in verification["line_repairs"][path].items():
        source = source.replace(old, replacement)
    assert 'onclick="editTask(JSON.parse(decodeURIComponent(this.dataset.eventPayload)))"' in source
    assert 'data-event-payload="${encodeURIComponent(JSON.stringify(task))}"' in source
    workspace.write_files({path: source}, allowed=_FILES)
    assert workspace.verify(expected=_FILES)["passed"]


def test_html_repair_requires_applied_edits_and_preserves_the_page_entry():
    original = '<!doctype html><html><main><h1>项目</h1></main><script src="app.js"></script></html>'
    partial = '正在改动标题\n<<<<<<< SEARCH\n<h1>新的标题</h1>\n=======\n<h1>项目</h1>\n>>>>>>> REPLACE'
    assert "未应用" in source_shape_problem("frontend/index.html", partial)
    assert "完整页面入口" in source_repair_problem("frontend/index.html", original, "<h1>项目</h1>")
    assert "交互脚本" in source_repair_problem("frontend/index.html", original, "<!doctype html><html><main>项目</main></html>")


def test_html_preflight_routes_unwrapped_script_to_the_page_entry(project):
    workspace, _ = project
    path = "frontend/index.html"
    page = '<!doctype html>\n<div id="app"></div>\n<script type="module" src="/src/main.ts"></script>\n'
    invalid = page + 'const view = `<input id="duplicate">`\n</script>\n'
    workspace.write_files({path: invalid}, allowed=[path])
    result = workspace.verify(expected=[path], scope="frontend")
    assert not result["passed"]
    assert {"path": path, "message": f"{path} 第 5 行的 </script> 没有对应的开始标签，请检查前面的脚本是否落在标签外"} in result["issues"]
    assert "第 4 行的 script 标签缺少 </script>" in source_shape_problem(path, page + "<script>const value = 1")
    assert not source_shape_problem(path, page + '<!-- </script> --><script type="application/json">{"x":1}</script>')


def test_typescript_entry_rejects_misrouted_raw_html_and_preserves_legitimate_template_strings():
    path = "frontend/src/main.ts"
    assert "误写成了 HTML" in source_shape_problem(path, '// 页面入口\n<div id="app"></div>')
    assert "组件片段" in source_shape_problem(path, '<template><div id="app"></div></template>')
    assert not source_shape_problem(path, 'const layout = `<div id="app"></div>`')
    assert not source_shape_problem("frontend/src/App.tsx", '<div id="app"></div>')


def test_repair_rejects_new_imports_to_files_outside_the_project_plan():
    path = "frontend/src/main.ts"
    entry = "import App from './App.vue'\n"
    paths = [path, "frontend/src/App.vue"]
    updated = entry + "import './style.css'\n"
    kwargs = {"known_paths": paths}
    assert "新增了工程中不存在的引用 ./style.css" in source_repair_problem(path, entry, updated, **kwargs)
    assert not source_repair_problem(path, updated, entry, **kwargs)
    assert not source_repair_problem(path, entry, updated, known_paths=[*paths, "frontend/src/style.css"])


_VITE_MANIFEST = {"scripts": {"build": "vue-tsc --noEmit && vite build"},
                  "dependencies": {"vue": "^3.5.0"}, "devDependencies": {"vite": "^7.0.0"}}
_VUE_CDN = "https://unpkg.com/vue@3/dist/vue.esm-browser.prod.js"
_VUE_CDN_IMPORT = f"  import {{ createApp }} from '{_VUE_CDN}';"


def test_vite_html_repair_bundles_declared_vue_and_preserves_template_compiler(project):
    workspace, _ = project
    path = "frontend/index.html"
    source = (f'<main id="app"></main>\r\n<script type="module">\r\n{_VUE_CDN_IMPORT}\r\n'
              "  createApp({template: '<button @click=\"count++\">{{ count }}</button>', data: () => ({count: 0})}).mount('#app');\r\n"
              "</script>\r\n")
    files = {**_FILES, path: source, "frontend/package.json": json.dumps(_VITE_MANIFEST)}
    workspace.write_files(files, allowed=files)
    report = workspace.verify(expected=files)
    assert not report["passed"]
    assert any(row["path"] == path and "隔离浏览器无法访问" in row["message"] for row in report["issues"])
    edits = report["line_repairs"][path]
    assert edits == {_VUE_CDN_IMPORT: "  import { createApp } from 'vue/dist/vue.esm-bundler.js';"}
    corrected = source.replace(_VUE_CDN_IMPORT, edits[_VUE_CDN_IMPORT])
    workspace.write_files({path: corrected}, allowed=files)
    assert workspace.verify(expected=files)["passed"]


@pytest.mark.parametrize("source", [
    f'<!-- <script type="module">\n{_VUE_CDN_IMPORT}\n</script> -->',
    f'<template><script type="module">\n{_VUE_CDN_IMPORT}\n</script></template>',
    f'<script type="text/plain">\n{_VUE_CDN_IMPORT}\n</script>',
    f'<script type="module" src="app.js">\n{_VUE_CDN_IMPORT}\n</script>',
    f'<script type="module">\n/*\n{_VUE_CDN_IMPORT}\n*/\n</script>',
    f'<script type="module">\nconst sample = `\n{_VUE_CDN_IMPORT}\n`;\n</script>',
    f'<script type="module">\n{_VUE_CDN_IMPORT}\n</script>\n<!--\n{_VUE_CDN_IMPORT}\n-->',
    f'<script type="module">\n{_VUE_CDN_IMPORT.replace("vue@3/", "vue@3.2.0/")}\n</script>',
])
def test_vite_vue_repair_only_edits_unambiguous_executed_imports(source):
    assert vite_vue_entry_repairs("frontend/index.html", source, _VITE_MANIFEST) == {}


def test_vue_repair_requires_a_compatible_vite_build_and_lockfile():
    source = f'<script type="module">\n{_VUE_CDN_IMPORT}\n</script>'
    path = "frontend/index.html"
    for manifest in (None, {**_VITE_MANIFEST, "scripts": {"build": "echo vite build"}},
                     {**_VITE_MANIFEST, "dependencies": {"vue": "^2.7.0"}},
                     {**_VITE_MANIFEST, "devDependencies": {}}):
        assert vite_vue_entry_repairs(path, source, manifest) == {}
    incompatible = {"packages": {"node_modules/vue": {"version": "2.7.16"}}}
    assert vite_vue_entry_repairs(path, source, _VITE_MANIFEST, lock=incompatible) == {}


@pytest.mark.parametrize("source", [
    "抱歉，按当前文件和验收结果暂时无法完成这次组件修复。我只能提供说明，建议先修复类型错误。" * 4,
    '<!-- <template><p>注释内的组件</p></template> --><style>p { color: red }</style>',
    '<docs><template><p>文档内的片段</p></template></docs>',
    '<template></template><script setup>\n\n</script>',
    '<template><div>尚未闭合</div>',
])
def test_vue_shape_rejects_prose_and_non_component_blocks(source):
    assert "完整的 Vue 单文件组件" in source_shape_problem("frontend/src/App.vue", source)


@pytest.mark.parametrize("source", [
    "<!-- 计数器 -->\n<script>\nimport { h } from 'vue'\nexport default { render() { return h('p', '计数器') } }\n</script>",
    '<script lang="ts" src="./render.ts" />',
    '<template><main><template v-if="isOpen"><input v-model="draft" /></template></main></template>',
    '<template lang="pug">p 组件标题</template>',
])
def test_vue_shape_accepts_script_only_and_other_component_blocks(source):
    assert not source_shape_problem("frontend/src/App.vue", source)


def test_vue_repair_preserves_interaction_and_requests_and_accepts_delegation():
    path = "frontend/src/App.vue"
    controls = '<template><button @click="load">获取备件</button></template>'
    integration = '<script setup>const load = () => fetch("/api/parts")</script>'
    original = controls + integration
    assert "完整组件" in source_repair_problem(path, original, "抱歉，当前无法修复这份源码。")
    assert "用户交互" in source_repair_problem(path, original, '<template><p>备件</p></template>' + integration)
    assert "接口调用" in source_repair_problem(path, original, controls + '<script setup>\n// fetch("/api/parts")\nconst load = () => []\n</script>')
    assert not source_repair_problem(path, original, original.replace("获取备件", "刷新备件"))
    assert not source_repair_problem(path, original, controls + '<script setup>import { load } from "./parts.ts"</script>')
    delegated = '<template><PartsPanel /></template><script setup>import PartsPanel from "./PartsPanel.vue"</script>'
    assert not source_repair_problem(path, original, delegated)


def test_vue_preflight_finds_only_the_unmatched_top_level_wrapper_in_a_saved_draft(project):
    workspace, _ = project
    path = "frontend/src/App.vue"
    complete = '<template><button @click="count++">增加</button></template>\n<script setup>const count = 0</script>\n'
    source = complete + '</vue>\n'
    workspace.write_files({path: source}, allowed=[path])
    report = workspace.verify(expected=[path], scope="frontend")
    assert not report["passed"] and report["line_repairs"][path] == {"</vue>": ""}
    assert not source_shape_problem(path, complete + '<vue>自定义块</vue>\n')
    assert "缺少完整" in source_shape_problem(path, source)


def test_project_repairs_explicit_port_conflict_and_unassociated_submit_control(project):
    workspace, _ = project
    app, page, script = "backend/server.py", "frontend/index.html", "frontend/app.js"
    files = {
        **_FILES,
        "README.md": "# 启动\n\npython backend/server.py\npython -m http.server 5000 --directory frontend\n",
        app: ("import os\nfrom flask import Flask\napp = Flask(__name__)\n"
              "@app.get('/api/tasks')\ndef tasks():\n    return []\n"
              "if __name__ == '__main__':\n    app.run(port=5000)\n"),
        page: ('<main><form id="taskForm"><input required></form>\n'
               '<button type="submit">保存</button></main><script src="app.js"></script>\n'),
        script: 'document.getElementById("taskForm").addEventListener("submit", () => fetch("/api/tasks"));\n',
    }
    workspace.write_files(files, allowed=files)
    verdict = workspace.verify(expected=files)
    assert not verdict["passed"] and set(verdict["line_repairs"]) == {app, page, "README.md"}
    for path, edits in verdict["line_repairs"].items():
        for old, updated in edits.items():
            files[path] = files[path].replace(old, updated)
    assert 'form="taskForm"' in files[page]
    assert 'app.send_static_file("index.html")' in files[app]
    assert "python -m http.server 5000" not in files["README.md"]
    workspace.write_files(files, allowed=files)
    assert workspace.verify(expected=files)["passed"]


def test_project_repairs_a_hidden_dialog_with_no_open_style(project):
    workspace, _ = project
    page, script = "frontend/index.html", "frontend/app.js"
    files = {
        **_FILES,
        page: ('<style>\n.panel-overlay { display: none; align-items: center; }\n</style>\n'
               '<main><button id="refresh">打开</button><div id="panel" class="panel-overlay"></div></main>'
               '<script src="app.js"></script>'),
        script: ('const ui = { panel: document.getElementById("panel") };\n'
                 'document.getElementById("refresh").addEventListener("click", () => ui.panel.classList.add("open"));\n'
                 'fetch("/api/tasks");\n'),
    }
    workspace.write_files(files, allowed=files)
    verdict = workspace.verify(expected=files)
    assert any("缺少对应的可见样式" in row["message"] for row in verdict["issues"])
    for old, replacement in verdict["line_repairs"][page].items():
        files[page] = files[page].replace(old, replacement)
    assert ".panel-overlay.open { display: flex; }" in files[page]
    workspace.write_files({page: files[page]}, allowed=files)
    assert workspace.verify(expected=files)["passed"]


@pytest.mark.skipif(not (shutil.which("node") or shutil.which("nodejs")), reason="本地未安装 JavaScript 解析器")
def test_project_javascript_syntax_is_verified_without_running_code(project):
    workspace, _ = project
    files = {**_FILES, "frontend/app.js": _FILES["frontend/app.js"] + "\nconst broken = ;\n"}
    workspace.write_files(files, allowed=files)
    report = workspace.verify(expected=files)
    assert not report["passed"]
    javascript = next(check for check in report["checks"] if check["id"] == "javascript_syntax")
    assert javascript["status"] == "failed" and "frontend/app.js" in javascript["detail"]
    assert "第 5 行" in javascript["detail"]
    assert parse_project_source("frontend/app.mjs", 'throw Error("这个文件只能解析");')


def test_project_reports_missing_frontend_routes_origin_and_test_database(project):
    workspace, _ = project
    files = {
        **_FILES,
        "README.md": "运行 python backend/server.py，另用 python -m http.server 3000 启动前端。",
        "backend/server.py": (
            "import os\nfrom flask import Flask\nfrom flask_sqlalchemy import SQLAlchemy\n"
            "app = Flask(__name__)\n"
            "app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get('DATABASE_URL', 'sqlite:///tasks.db')\n"
            "db = SQLAlchemy(app)\n@app.get('/api/tasks')\ndef tasks():\n    return []\n"
        ),
        "frontend/app.js": (
            "const base = 'http://localhost:8000';\n"
            "function request(path) { return fetch(`${base}${path}`); }\n"
            "document.querySelector('#refresh').addEventListener('click', () => request('/api/tasks'));\n"
            "fetch(`${base}/api/auth/me`);\n"
            "const external = 'https://example.org'; fetch(`${external}/api/auth/check`);\n"
        ),
        "tests/test_api.py": (
            "from server import app, db\n"
            "def test_tasks():\n"
            "    app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///:memory:'\n"
            "    assert app.test_client().get('/api/tasks').status_code == 200\n"
        ),
    }
    workspace.write_files(files, allowed=files)
    report = workspace.verify(expected=files)
    problems = {item["path"]: item["message"] for item in report["issues"]}
    assert "/api/auth/me" in problems["frontend/app.js"]
    assert all("/api/auth/check" not in item["message"] for item in report["issues"])
    assert "跨源" in problems["backend/server.py"]
    assert "DATABASE_URL" in problems["tests/test_api.py"] and "导入应用前" in problems["tests/test_api.py"]


def test_verified_flask_database_repairs_keep_startup_defaults_and_guard_fixture_cleanup(project):
    workspace, _ = project
    app, unit, e2e = "backend/server.py", "tests/test_api.py", "tests/test_flow.py"
    source = (
        "import os\nfrom flask import Flask\nfrom flask_sqlalchemy import SQLAlchemy\n"
        "app = Flask(__name__)\napp.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///tasks.db'\ndb = SQLAlchemy(app)\n"
        "@app.get('/api/tasks')\ndef tasks():\n    return []\n"
    )
    fixture = (
        "import pytest\nfrom server import app, db\n\n@pytest.fixture\ndef client():\n"
        "    app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///test.db'\n"
        "    with app.app_context():\n        db.create_all()\n    return app.test_client()\n\n"
        "def test_tasks(client):\n    assert client.get('/api/tasks').status_code == 200\n"
    )
    files = {**_FILES, app: source, unit: fixture, e2e: "env = {}\nenv['DEMO_DB_PATH'] = '/tmp/qa.db'\n"}
    workspace.write_files(files, allowed=files)
    report = workspace.verify(expected=files)
    assert set(report["line_repairs"]) == {app}
    for old, replacement in report["line_repairs"][app].items():
        source = source.replace(old, replacement)
    assert "DEMO_DB_PATH" in source and "sqlite:///tasks.db" in source
    workspace.write_files({app: source}, allowed=files)
    edits = workspace.verify(expected=files)["line_repairs"][unit]
    for old, replacement in edits.items():
        fixture = fixture.replace(old, replacement)
    assert 'database_config.delenv("DEMO_DB_PATH", raising=False)' in fixture
    assert 'setenv("DATABASE_URL", "sqlite:///:memory:")' in fixture
    assert fixture.index('verification_url.database not in (None, ":memory:")') < fixture.index('db.drop_all()')
    assert "assert client.get('/api/tasks').status_code == 200" in fixture
    workspace.write_files({unit: fixture}, allowed=files)
    assert workspace.verify(expected=files)["passed"]


def test_project_rework_catches_missing_dependency_in_install_manifest(project):
    workspace, _ = project
    manifest = "backend/requirements.txt"
    files = {**_FILES, manifest: "Flask==3.1.0\n",
             "backend/server.py": "from flask_cors import CORS\n" + _BACKEND}
    workspace.write_files(files, allowed=files)
    report = workspace.verify(expected=files)
    issue = next(item for item in report["issues"] if "flask-cors" in item["message"])
    assert issue["path"] == manifest
    workspace.write_files({manifest: files[manifest] + "Flask_Cors>=5\n"}, allowed=files)
    assert workspace.verify(expected=files)["passed"]


def test_project_checks_missing_test_names_and_accepts_database_setup_before_import(project):
    workspace, _ = project
    path = "tests/test_api.py"
    files = {
        **_FILES,
        "backend/server.py": (
            "import os\nfrom flask import Flask\nfrom flask_sqlalchemy import SQLAlchemy\n"
            "app = Flask(__name__)\n"
            "app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get('DATABASE_URL', 'sqlite:///real.db')\n"
            "db = SQLAlchemy(app)\n@app.get('/api/tasks')\ndef tasks():\n    return []\n"
        ),
        path: (
            "import os\nos.environ['DATABASE_URL'] = 'sqlite:///:memory:'\nfrom server import app\n"
            "def test_tasks():\n"
            "    app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///:memory:'\n"
            "    scratch = Flask(__name__)\n"
            "    scratch.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///test.db'\n"
            "    assert app.test_client().get('/api/tasks').status_code == 200\n"
        ),
    }
    workspace.write_files(files, allowed=files)
    issues = workspace.verify(expected=files)["issues"]
    assert len(issues) == 1 and "未定义的 Flask" in issues[0]["message"]
    assert workspace.verify(expected=files)["import_repairs"] == {path: ["from flask import Flask"]}
    workspace.write_files({path: "from flask import Flask\n" + files[path]}, allowed=files)
    assert workspace.verify(expected=files)["passed"]


def test_project_leaves_ambiguous_imports_for_model_repair(project):
    workspace, _ = project
    test = "tests/test_api.py"
    files = {**_FILES,
             test: "def test_tasks():\n    assert Path('README.md').exists()\n",
             "backend/module_a.py": "from pathlib import Path\n",
             "backend/module_b.py": "from posixpath import realpath as Path\n"}
    workspace.write_files(files, allowed=files)
    report = workspace.verify(expected=files)
    assert report["import_repairs"] == {}
    assert any(row["path"] == test and "Path" in row["message"] for row in report["issues"])


def test_frontend_scope_does_not_require_backend_or_openapi(project):
    workspace, _ = project
    files = {"README.md": "运行 `python -m http.server 8000`。", "index.html": _FILES["frontend/index.html"],
             "app.js": _FILES["frontend/app.js"], "tests/ui.test.mjs": "expect(await page.getByRole('button').isVisible()).toBe(true)"}
    workspace.write_files(files, allowed=files)
    assert workspace.verify(expected=files, scope="frontend")["passed"]
    assert not workspace.verify(expected=files, scope="fullstack")["passed"]


def test_conversation_131_defects_point_to_each_actual_file(project):
    workspace, _ = project
    files = {
        **_FILES,
        "README.md": "安装 `pip install Flask==2.3.3`，运行 `python backend/server.py`，随后直接在浏览器打开 `frontend/index.html`。",
        "frontend/index.html": "我是 kimi-k3.1-pro。",
        "frontend/app.js": '<!DOCTYPE html><html><body><main><button>刷新</button></main><script src="app.js"></script></body></html>',
        "backend/server.py": "from flask import Flask\napp = Flask(__name__)\n"
        "@app.before_first_request\ndef initialize():\n    pass\n\n"
        "@app.get('/api/tasks')\ndef tasks():\n    return []\n",
    }
    workspace.write_files(files, allowed=files)
    report = workspace.verify(expected=files)
    assert not report["passed"]
    issues_by_path = {path: "\n".join(item["message"] for item in report["issues"] if item["path"] == path) for path in files}
    assert "自我介绍" in issues_by_path["frontend/index.html"]
    assert "OpenAPI" not in issues_by_path["frontend/index.html"]
    assert "完整 HTML" in issues_by_path["frontend/app.js"]
    assert "OpenAPI" in issues_by_path["frontend/app.js"]
    assert "before_first_request" in issues_by_path["backend/server.py"]
    assert "直接打开本地 HTML" in issues_by_path["README.md"]


def test_api_path_detects_a_configured_base_plus_template(project):
    workspace, _ = project
    files = {**_FILES, "frontend/app.js": 'const api = "http://localhost:8000/api";\n'
             + _FILES["frontend/app.js"].replace('"/api/tasks"', '`${api}/tasks?status=open`')}
    workspace.write_files(files, allowed=files)
    assert workspace.verify(expected=files)["passed"]


def test_api_path_detects_requests_sent_through_a_local_wrapper(project):
    workspace, _ = project
    files = {**_FILES, "frontend/app.js": '''const API_BASE = 'http://localhost:5000/api';
async function request(path, options = {}) {
  return fetch(`${API_BASE}${path}`, options);
}
document.querySelector('#refresh').addEventListener('click', async () => {
  const query = '?status=open';
  await request(`/tasks${query}`);
});'''}
    workspace.write_files(files, allowed=files)
    assert workspace.verify(expected=files)["passed"]


def test_http_launch_instructions_and_isolated_tests_are_allowed(project):
    workspace, _ = project
    files = {**_FILES, "README.md": _FILES["README.md"] + "直接在浏览器打开 `http://127.0.0.1:8000/`。不要直接在浏览器打开 `frontend/index.html`。",
             "tests/test_api.py": _FILES["tests/test_api.py"] + "\n\ndef test_database(tmp_path):\n    db = tmp_path / 'test.db'\n    assert not db.exists()\n"}
    workspace.write_files(files, allowed=files)
    assert workspace.verify(expected=files)["passed"]


@pytest.mark.parametrize("path", ["../escape.py", "backend/../../escape.py", "C:/secret.txt", "//host/file.js",
                                  "backend\\main.py", "backend/.env", "backend/.env.prod", "./main.py", "/main.py",
                                  "backend/lpt1.py", "a/abc.", "src/.git/hooks/pre-commit", "public/payload.zip",
                                  "frontend/node_modules/main.js", "verification.json", "下载.exe"])
def test_reject_unportable_private_or_binary_paths(path):
    with pytest.raises(ProjectWorkspaceError):
        validate_project_path(path)


def test_owner_scope_portable_paths_and_content_are_prevalidated(project):
    workspace, _ = project
    for files, allowed in [
        ({"main.py": "x = 1", "private.py": "x = 2"}, {"main.py"}),
        ({"Main.py": "x = 1", "main.py": "x = 2"}, {"Main.py", "main.py"}),
        ({"src/main.py": "x = 1", "src": "x = 2"}, {"src/main.py", "src"}),
        ({"config.py": "KEY = '" + "ghp_" + "a" * 36 + "'"}, {"config.py"}),
        ({"raw.txt": "\0"}, {"raw.txt"}),
    ]:
        with pytest.raises(ProjectWorkspaceError):
            workspace.write_files(files, allowed=allowed)
        assert not workspace.inspect()
    with pytest.raises(ProjectWorkspaceError):
        validate_project_paths(["Frontend/a.js", "frontend/b.js"])
    assert validate_project_paths(["frontend/app/(auth)/page.tsx", "backend/.env.example"])


def test_workspace_rejects_links_and_keeps_files_outside_project_safe(project):
    workspace, root = project
    workspace.write_files({"README.md": "# 正常文件"}, allowed={"README.md"})
    source = root / "11" / "12" / "13" / "team-projects" / "run-1" / "source"
    outside = root / "secret.txt"
    outside.write_text("原有文件", encoding="utf-8")
    linked = source / "hardlink.txt"
    os.link(outside, linked)
    with pytest.raises(ProjectWorkspaceError):
        workspace.inspect()
    linked.unlink()
    try:
        (source / "escape").symlink_to(root, target_is_directory=True)
    except OSError:
        pytest.skip("当前平台没有创建符号链接的权限")
    with pytest.raises(ProjectWorkspaceError):
        workspace.write_files({"escape/secret.txt": "越界覆盖"}, allowed={"escape/secret.txt"})
    assert outside.read_text(encoding="utf-8") == "原有文件"
