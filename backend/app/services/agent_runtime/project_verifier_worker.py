"""在离线验收容器中启动工程并记录实际运行结果。"""

from __future__ import annotations

import ast
import http.client
import json
import math
import os
import re
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from collections import defaultdict
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, distributions, version
from pathlib import Path, PurePosixPath
from typing import BinaryIO
from urllib.parse import urljoin, urlsplit

from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import canonicalize_name

from .project_browser_contract import BROWSER_CHECKS_PATH, browser_form_scope, parse_browser_checks
from .project_node_toolchain import NPM, dependency_ranges, provide_dependencies
from .project_verifier_protocol import (
    JOB_NAME, JOB_TIMEOUT_SECONDS, MAX_JOB_BYTES, MAX_OPENAPI_BYTES, MAX_PROJECT_BYTES, MAX_PROJECT_FILES,
    MAX_PROJECT_FILE_BYTES, MAX_RESULT_BYTES, openapi_source, read_json, source_digest, valid_job_path, write_json,
)

_QUEUE = Path("/var/lib/project-verifier")
_CHILD_USER = 65534
_BROWSER_USER = 65533
_BROWSER_PROBE = Path(__file__).with_name("project_browser_probe.py")
_PYTHON = (sys.executable, "-B")
_PYTHON_COMPATIBILITY = (Path("/opt/project-python/pydantic-2.8"),)
_ENTRY = re.compile(r"\b(python3?|node)\s+((?:\./)?[\w./-]+\.(?:py|[cm]?js))(?=\s|[\x60。。，]|$)")
_UVICORN = re.compile(r"\buvicorn\s+([A-Za-z_][\w.]*:[A-Za-z_]\w*)")
_FLASK = re.compile(r"\bflask\s+--app\s+([A-Za-z_][\w.]*)(?::[A-Za-z_]\w*)?\s+run\b")
_PORT = re.compile(r"(?:localhost:|127\.0\.0\.1:|0\.0\.0\.0:|--port[= ]|http\.server\s+)(\d{2,5})")
_WEB_PORT = re.compile(r"\bhttp\.server\s+(\d{2,5})")
_COMMAND_PORT = re.compile(r"(?<!\S)--port[= \t]+(\d{2,5})\b")
_VITE_BUILD = re.compile(r"\bvite\s+build\b")
_BUILD_DIAGNOSTIC = re.compile(r"\b(?:error|failed|could not)\b|^\s*file:", re.I)
_WEB_COMPONENTS = (".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".vue")
_ROUTE = re.compile(r"/[A-Za-z0-9_./%-]{0,180}\Z")
_ASSET = re.compile(r"<(?:script|link)\b[^>]+(?:src|href)=['\"]([^'\"]+)['\"]", re.I)
_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
_PYTEST_FILE = re.compile(
    r"^(?P<indent>[ \t]*)(?:(?P<python>python3?) -m )?pytest(?:[ \t]+--?[\w-]+)*"
    r"[ \t]+(?P<path>[\w./-]+\.py)(?:[ \t]+--?[\w-]+)*[ \t]*(?=\r?$)", re.M,
)
_ASSERTION = re.compile(r"Traceback|Error|FAILED|FAIL:|\bpassed\b|未通过|^E\s+")
_PYTEST_FIXTURE_SCOPE = re.compile(
    r"(?m)^(ScopeMismatch: [^\r\n]+)\r?\n([^\r\n]+\.py):(\d+):[ \t]+(?:async )?def ([A-Za-z_]\w*)\(",
)
_PYTEST_CASE = re.compile(r"(?m)^_{2,} (.+?) _{2,}$")
_PYTEST_FRAME = re.compile(r"(?m)^([\w./@()+\[\]-]+\.py):(\d+): in ([\w.<>]+)$")
_FAILED_ASSERTION = re.compile(r"(?m)^E\s+assert ([^\r\n]+)")
_HTTP_METHODS = {"get", "post", "put", "patch", "delete", "head", "options"}
_WEB_FAILURE = re.compile(r"前端|首页|浏览器|\b(?:frontend|homepage|browser|FRONTEND_URL)\b", re.I)


class VerificationCancelled(Exception):
    """平台已撤回请求，或本次验收已超过时限。"""


@dataclass(frozen=True)
class Invocation:
    path: str
    cwd: str
    argv: tuple[str, ...]


@dataclass(frozen=True)
class Site:
    entry: Invocation | None
    backend_port: int | None
    web: str | None
    web_port: int | None
    blocked: bool


@dataclass(frozen=True)
class ViteProject:
    path: str
    manifest: dict
    scripts: dict

    @property
    def cwd(self) -> str:
        return PurePosixPath(self.path).parent.as_posix()


def _module_location(module: str, files: dict[str, str]) -> tuple[str, str] | None:
    relative = module.replace(".", "/") + ".py"
    candidates = [path for path in files if path == relative or path.endswith("/" + relative)]
    if len(candidates) == 1:
        path = candidates[0]
        return path, path.removesuffix(relative).rstrip("/") or "."
    return None


def _command_port(command: str) -> str | None:
    match = _COMMAND_PORT.search(command)
    return match[1] if match and 0 < int(match[1]) <= 65535 else None


def _entrypoint(files: dict[str, str], readme: str) -> Invocation | None:
    for executable, name in _ENTRY.findall(readme):
        name = name.removeprefix("./")
        candidates = [name] if name in files else [path for path in files if path.endswith("/" + name)]
        if len(candidates) != 1 or _is_test(candidates[0]):
            continue
        path = candidates[0]
        cwd = "." if path == name else path.removesuffix(name).rstrip("/")
        command = _PYTHON if executable.startswith("python") else ("node",)
        return Invocation(path, cwd, (*command, name))
    if match := _UVICORN.search(readme):
        target = match.group(1)
        if location := _module_location(target.split(":")[0], files):
            port = _command_port(re.split(r"[\r\n`]", readme[match.end():], maxsplit=1)[0]) or "8000"
            return Invocation(*location, (*_PYTHON, "-m", "uvicorn", target, "--host", "127.0.0.1", "--port", port))
    if match := _FLASK.search(readme):
        target = match.group(1)
        if location := _module_location(target, files):
            command = (*_PYTHON, "-m", "flask", "--app", target, "run", "--host", "127.0.0.1")
            if port := _command_port(re.split(r"[\r\n`]", readme[match.end():], maxsplit=1)[0]):
                command += ("--port", port)
            return Invocation(*location, command)
    for path, source in files.items():
        if PurePosixPath(path).name != "package.json":
            continue
        manifest = json.loads(source)
        scripts = manifest.get("scripts") if isinstance(manifest, dict) else None
        script = scripts.get("start") if isinstance(scripts, dict) else None
        if isinstance(script, str) and (command := re.fullmatch(r"\s*node\s+((?:\./)?[\w./-]+\.[cm]?js)\s*", script)):
            cwd = PurePosixPath(path).parent
            target = (cwd / command.group(1)).as_posix()
            if target in files:
                return Invocation(target, str(cwd), ("node", command.group(1)))
    return None


def _is_test(path: str) -> bool:
    parts = path.casefold().split("/")
    return (bool(set(parts[:-1]) & {"test", "tests", "spec", "__tests__", "e2e"})
            or parts[-1].startswith("test_") or ".test." in parts[-1] or ".spec." in parts[-1])


def _test_commands(files: dict[str, str]) -> tuple[list[Invocation], list[str]]:
    groups: dict[str, list[str]] = defaultdict(list)
    individual: list[Invocation] = []
    script_names = []
    for path, content in files.items():
        if not _is_test(path):
            continue
        parent = PurePosixPath(path).parent.as_posix()
        name = PurePosixPath(path).name
        if path.endswith(".py"):
            tree = ast.parse(content)
            collected = any(isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test_")
                            for node in ast.walk(tree))
            if collected:
                groups[parent].append(path)
            elif re.search(r"""(?m)^if\s+__name__\s*==\s*['"]__main__['"]\s*:""", content):
                individual.append(Invocation(path, parent, (*_PYTHON, name)))
                script_names.append(path)
        elif path.endswith((".js", ".mjs", ".cjs")) and re.search(r"\b(?:node:test|node:assert)\b", content):
            individual.append(Invocation(path, parent, ("node", "--test", name)))
    suites = [Invocation(paths[0], parent, (*_PYTHON, "-m", "pytest", "-q", "-rfEp", "--tb=short", "--color=no", "-p", "no:cacheprovider",
                                            *(PurePosixPath(path).name for path in paths)))
              for parent, paths in groups.items()]
    return [*suites, *individual], script_names


def _standalone_test_edits(readme: str, files: dict[str, str], scripts: list[str]) -> dict[str, str]:
    edits = {}
    for match in _PYTEST_FILE.finditer(readme):
        path = match["path"].removeprefix("./")
        candidates = [name for name in files if name == path or name.endswith("/" + path)]
        if len(candidates) == 1 and candidates[0] in scripts:
            edits[match[0]] = f"{match['indent']}{match['python'] or 'python'} {match['path']}"
    return edits


def _unsupported_dependencies(invocation: Invocation, files: dict[str, str], *, versions: dict[str, str] | None = None) -> list[str]:
    parent = PurePosixPath(invocation.path).parent
    if invocation.argv[0] == "node":
        manifest = next((files[(part / "package.json").as_posix()] for part in (parent, *parent.parents)
                         if (part / "package.json").as_posix() in files), "{}")
        payload = json.loads(manifest)
        dependencies = payload.get("dependencies") if isinstance(payload, dict) else None
        return sorted(dependencies) if isinstance(dependencies, dict) else []

    unsupported = []
    paths = {(part / "requirements.txt").as_posix() for part in (parent, *parent.parents)}
    for path in sorted(paths):
        for line in files.get(path, "").splitlines():
            try:
                requirement = Requirement(line.split(" #", 1)[0].strip())
            except InvalidRequirement:
                continue
            if requirement.marker and not requirement.marker.evaluate():
                continue
            try:
                installed = (versions or {}).get(canonicalize_name(requirement.name)) or version(requirement.name)
            except PackageNotFoundError:
                unsupported.append(requirement.name)
                continue
            if requirement.url or not requirement.specifier.contains(installed, prereleases=True):
                unsupported.append(f"{requirement.name}{requirement.specifier}（环境为 {installed}）")
    return list(dict.fromkeys(unsupported))


def _api_probes(files: dict[str, str]) -> list[tuple[str, set[int], set[int]]]:
    contract = next((json.loads(source) for name, source in files.items()
                     if PurePosixPath(name).name.casefold() == "openapi.json"), {})
    result = []
    paths = contract.get("paths") or {} if isinstance(contract, dict) else {}
    if not isinstance(paths, dict):
        return result
    for path, operations in paths.items():
        route = operations.get("get") if isinstance(operations, dict) and _ROUTE.fullmatch(path) else None
        if not isinstance(route, dict) or any(
            isinstance(parameter, dict) and parameter.get("required")
            for parameter in [*(operations.get("parameters") or []), *(route.get("parameters") or [])]
        ):
            continue
        responses = route.get("responses") or {}
        if not isinstance(responses, dict):
            responses = {}
        security = route.get("security", contract.get("security"))
        requires_auth = isinstance(security, list) and bool(security) and all(security)
        if requires_auth:
            codes = {int(code) for code in ("401", "403") if code in responses} or {401, 403}
        else:
            codes = {int(code) for code in responses if code.isdecimal() and 200 <= int(code) < 400} or {200}
        json_codes = {code for code in codes if isinstance(response := responses.get(str(code)), dict)
                      and "application/json" in (response.get("content") or {})}
        result.append((path, codes, json_codes))
    return result[:10]


def _limit_child() -> None:
    import resource

    resource.setrlimit(resource.RLIMIT_NOFILE, (256, 256))
    resource.setrlimit(resource.RLIMIT_FSIZE, (8 * 1024 * 1024,) * 2)
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))


def _cleanup_children() -> None:
    """同一时刻仅运行一个工程，清理验收 UID 下的所有后代。"""
    for _ in range(8):
        found = False
        for process in Path("/proc").iterdir():
            if not process.name.isdecimal():
                continue
            try:
                status = (process / "status").read_text()
                uid = re.search(r"(?m)^Uid:\s+(\d+)", status)
                if uid and int(uid.group(1)) in {_CHILD_USER, _BROWSER_USER} and not re.search(r"(?m)^State:\s+Z\b", status):
                    os.kill(int(process.name), signal.SIGKILL)
                    found = True
            except (FileNotFoundError, ProcessLookupError):
                continue
        if not found:
            return
        time.sleep(0.08)
    raise SystemExit("验收子进程无法完全回收，重启验收容器")


class Job:
    def __init__(self, request: Path, deadline: float, area: Path, *, capture_browser_errors: bool = False):
        self.request = request
        self.deadline = deadline
        self.capture_browser_errors = capture_browser_errors
        self.root = area / "source"
        self.area = area
        self.checks: list[dict] = []
        self.issues: list[dict] = []
        self.readme_edits: dict[str, str] = {}
        self.python_versions: dict[str, str] = {}
        self.processes: list[tuple[subprocess.Popen, BinaryIO]] = []
        self.heartbeat = 0.0
        self.env = {
            "PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": str(self.root), "TMPDIR": str(self.root),
            "PYTHONPATH": os.pathsep.join(str(self.root / directory) for directory in ("", "backend", "server")),
            "PYTHONIOENCODING": "utf-8", "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1", "PYTHONNOUSERSITE": "1",
            "SECRET_KEY": secrets.token_hex(32), "DATABASE_URL": f"sqlite:///{self.root}/verification.db",
            "PYTHONUNBUFFERED": "1", "PYTHONDONTWRITEBYTECODE": "1",
            "npm_config_cache": str(self.root / ".npm-cache"), "npm_config_offline": "true",
            "npm_config_update_notifier": "false", "npm_config_audit": "false",
        }

    def tick(self) -> None:
        if not self.request.is_file() or time.monotonic() >= self.deadline:
            raise VerificationCancelled
        if time.monotonic() - self.heartbeat >= 1:
            (self.request.parent.parent / "heartbeat").touch()
            self.heartbeat = time.monotonic()

    def add(self, identifier: str, label: str, status: str, detail: str, path: str = "",
            *, files: list[str] | None = None, test_cases: list[str] | None = None) -> None:
        check = {"id": f"runtime.{identifier}", "label": label, "status": status, "detail": detail[:1200]}
        if files:
            check["files"] = files
        if test_cases:
            check["test_cases"] = test_cases
        self.checks.append(check)
        if status == "failed" and path:
            self.issues.append({"path": path, "message": detail[:500]})

    def start(self, invocation: Invocation, *, browser_home: Path | None = None) -> tuple[subprocess.Popen, BinaryIO]:
        self.tick()
        log = tempfile.TemporaryFile(dir=self.area)
        if browser_home is None:
            cwd, env, user = self.root / invocation.cwd, self.env, _CHILD_USER
        else:
            cwd, user = browser_home, _BROWSER_USER
            env = {"PATH": self.env["PATH"], "HOME": str(cwd), "TMPDIR": str(cwd),
                   "PLAYWRIGHT_BROWSERS_PATH": os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "/opt/playwright")}
        try:
            process = subprocess.Popen(
                invocation.argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                stdout=log, stderr=subprocess.STDOUT, start_new_session=True, preexec_fn=_limit_child,
                user=user, group=user, extra_groups=(),
            )
        except BaseException:
            log.close()
            raise
        self.processes.append((process, log))
        return process, log

    def output(self, log: BinaryIO, *, limit: int = 8192) -> str:
        log.seek(max(0, os.fstat(log.fileno()).st_size - limit))
        text = _ANSI.sub("", log.read(limit).decode("utf-8", errors="replace")).replace(str(self.root) + "/", "")
        return text.replace(self.env["SECRET_KEY"], "[隐藏]")

    def details(self, log: BinaryIO) -> str:
        lines = [line.strip() for line in self.output(log).splitlines() if line.strip()]
        important = [line for line in lines if _ASSERTION.search(line)]
        return " · ".join((important or lines)[-6:])[-920:]

    def stop(self) -> None:
        try:
            _cleanup_children()
        finally:
            for process, log in self.processes:
                process.wait(timeout=2)
                log.close()
            self.processes.clear()


def _prepare_python(job: Job, entry: Invocation, files: dict[str, str]) -> list[str]:
    unsupported = _unsupported_dependencies(entry, files)
    if not unsupported or entry.argv[0] == "node":
        return unsupported
    for profile in _PYTHON_COMPATIBILITY:
        versions = {canonicalize_name(dist.name): dist.version for dist in distributions(path=[str(profile)])}
        if versions and not _unsupported_dependencies(entry, files, versions=versions):
            job.python_versions = versions
            job.env["PYTHONPATH"] = str(profile) + os.pathsep + job.env["PYTHONPATH"]
            return []
    return unsupported


def _http_timeout(_signal, _frame) -> None:
    raise TimeoutError("本地 HTTP 响应超时")


def _http(port: int, route: str) -> tuple[int, str, bytes, str]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=0.8)
    # 还要约束总耗时，否则缓慢持续发送内容可反复绕开单次读取超时。
    signal.setitimer(signal.ITIMER_REAL, 1.25)
    try:
        connection.request("GET", route)
        response = connection.getresponse()
        return response.status, response.getheader("Content-Type", ""), response.read(65_537), response.getheader("Location", "")
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        connection.close()


def _await_http(job: Job, process: subprocess.Popen, ports: list[int]) -> int | None:
    until = min(time.monotonic() + 8, job.deadline)
    while time.monotonic() < until:
        if process.poll() is not None:
            return None
        for port in ports:
            job.tick()
            try:
                _http(port, "/")
                return port
            except (OSError, http.client.HTTPException):
                continue
        time.sleep(0.15)
    return None


def _probe_api(job: Job, port: int, entry: Invocation, files: dict[str, str]) -> None:
    probes = _api_probes(files)
    if not probes:
        job.add("api", "接口响应", "skipped", "未找到可直接探测的 OpenAPI GET 操作")
        return
    problems = []
    for route, expected, json_codes in probes:
        job.tick()
        try:
            status, media_type, raw, _ = _http(port, route)
            if status not in expected:
                problems.append(f"{route} 实际 HTTP {status}，预期 {'、'.join(map(str, sorted(expected)))}")
            elif status in json_codes and status != 204:
                if "json" not in media_type.casefold():
                    problems.append(f"{route} 的响应类型为 {media_type or '未声明'}，预期 JSON")
                elif len(raw) <= 65_536:
                    json.loads(raw)
        except ValueError:
            problems.append(f"{route} 未返回可解析的 JSON")
        except (OSError, http.client.HTTPException):
            problems.append(f"{route} 未取得 HTTP 响应")
    job.add("api", "接口响应", "failed" if problems else "passed",
            "；".join(problems[:5]) if problems else f"实际请求了 {len(probes)} 个 OpenAPI GET 接口", entry.path)


def _probe_frontend(job: Job, index: str, source: str, port: int, *, shared: bool = False, built: bool = False) -> bool:
    home = "/"
    status, _, content, location = _http(port, home)
    if built and status in {301, 302, 307, 308} and _ROUTE.fullmatch(location) and not location.startswith("//"):
        home = location
        status, _, content, _ = _http(port, home)
    if status != 200 or b"<" not in content:
        job.add("web", "前端页面响应", "failed", f"首页实际 HTTP {status}，未返回有效页面", index)
        return False
    markup = content.decode("utf-8", errors="replace") if built else source
    paths = []
    for asset in _ASSET.findall(markup):
        target = urlsplit(urljoin(home, asset))
        if not asset.startswith("#") and not (target.scheme or target.netloc) and target.path not in paths:
            paths.append(target.path)
    for path in paths[:12]:
        job.tick()
        status, media, _, _ = _http(port, path)
        expected = "javascript" if path.endswith((".js", ".mjs")) else "css" if path.endswith(".css") else ""
        if status != 200 or (built and expected and expected not in media.casefold()):
            job.add("web", "前端页面响应", "failed", f"页面资源 {path} 实际 HTTP {status}，类型 {media or '未声明'}", index)
            return False
    mode = "生产构建" if built else "同源 HTTP" if shared else "HTTP"
    job.add("web", "前端页面响应", "passed",
            f"通过{mode}取到首页及 {min(12, len(paths))} 个本地资源")
    return True


def _same_frontend(port: int, source: str) -> bool:
    try:
        status, _, body, _ = _http(port, "/")
    except (OSError, http.client.HTTPException):
        return False
    return status == 200 and body == source.encode("utf-8")[:65_537]


def _test_failure_path(detail: str, suite: Invocation, entry: Invocation | None, web: str | None) -> str:
    if web and _WEB_FAILURE.search(detail):
        return web
    if entry and re.search(r"AssertionError|\bFAIL:|HTTPError|ConnectionError", detail):
        return entry.path
    return suite.path


def _fixture_scope_failures(output: str, suite: Invocation, files: dict[str, str]) -> list[dict]:
    issues = {}
    for reason, filename, line, fixture in _PYTEST_FIXTURE_SCOPE.findall(output):
        path = filename if filename in files else (PurePosixPath(suite.cwd) / filename).as_posix()
        if path in files and _is_test(path):
            issues[path, fixture] = {"path": path, "message": f"{path}:{line} 夹具 {fixture} · {reason}"[:500]}
    return list(issues.values())[:4]


def _http_operation(node: ast.AST) -> str:
    if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and node.func.attr in _HTTP_METHODS and node.args):
        return ""
    route = node.args[0]
    prefix = route.values[0] if isinstance(route, ast.JoinedStr) and route.values else route
    if not (isinstance(prefix, ast.Constant) and isinstance(prefix.value, str)
            and prefix.value.startswith(("/", "http://", "https://"))):
        return ""
    return f"{node.func.attr.upper()} {ast.unparse(route)}"


def _status_assertion_context(tree: ast.AST, case: str, line: int, observed: str) -> tuple[str, bool]:
    """HTTP 断言前后的真实测试步骤，用于判断应查看哪份源码。"""
    actual = re.match(r"(\d{3})(?:\s|$)", observed)
    if not actual:
        return "", False
    functions = [node for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))]
    function = next((node for node in functions if node.name == case.split(".")[-1] and node.lineno < line <= node.end_lineno), None)
    if not function:
        return "", False
    for index, assertion in enumerate(function.body):
        if isinstance(assertion, ast.Assert) and assertion.lineno == line:
            break
    else:
        return "", False
    expression = assertion.test
    if not (isinstance(expression, ast.Compare) and isinstance(expression.left, ast.Attribute)
            and expression.left.attr == "status_code" and isinstance(response := expression.left.value, ast.Name)
            and len(expression.ops) == 1):
        return "", False
    request = ""
    for statement in reversed(function.body[:index]):
        if isinstance(statement, ast.Assign) and any(isinstance(target, ast.Name) and target.id == response.id for target in statement.targets):
            request = _http_operation(statement.value)
            break
    if not request:
        return "", False

    comparator = expression.comparators[0]
    if isinstance(expression.ops[0], ast.Eq) and isinstance(comparator, ast.Constant) and type(comparator.value) is int:
        expected = {comparator.value}
    elif isinstance(expression.ops[0], ast.In) and isinstance(comparator, (ast.Tuple, ast.List, ast.Set)):
        expected = {value.value for value in comparator.elts if isinstance(value, ast.Constant) and type(value.value) is int}
    else:
        expected = set()
    if not 400 <= int(actual[1]) < 500:
        return request, False
    if any(200 <= status < 300 for status in expected) and any(400 <= status < 500 for status in expected):
        return f"{request} · 断言同时允许成功和拒绝状态，请依据业务规则核对预期并保留验收约束", True
    if expected and all(200 <= status < 300 for status in expected):
        helpers = {node.name for node in functions if any(_http_operation(child) for child in ast.walk(node))}
        for statement in function.body[index + 1:]:
            if isinstance(statement, (ast.Assign, ast.Expr)) and isinstance(statement.value, ast.Call):
                call = statement.value
                following = _http_operation(call)
                if not following and isinstance(call.func, ast.Name) and call.func.id in helpers:
                    following = call.func.id + "(...)"
                if following:
                    return f"前置请求 {request} 未达到预期，后续 {following} 未执行。核对准备状态并保留后续断言", True
    return request, False


def _pytest_case_failures(output: str, suite: Invocation, files: dict[str, str],
                          entry: Invocation | None, web: str | None) -> list[dict]:
    cases = list(_PYTEST_CASE.finditer(output))
    trees = {}
    issues = {}
    for index, case in enumerate(cases):
        end = cases[index + 1].start() if index + 1 < len(cases) else len(output)
        block = output[case.end():end].split("short test summary info", 1)[0]
        frames = []
        for filename, number, function in _PYTEST_FRAME.findall(block):
            path = filename if filename in files else (PurePosixPath(suite.cwd) / filename).as_posix()
            if path in files and (line := int(number)) <= len(files[path].splitlines()):
                frames.append((path, line, function))
        origin = next((frame for frame in reversed(frames) if _is_test(frame[0])), None)
        if not origin:
            continue
        path, line, function = origin
        title = case[1].strip()
        message = next((row[1:].strip() for row in block.splitlines() if row.startswith("E ")), "")
        if title.startswith("ERROR "):
            phase = "测试准备" if title.startswith(("ERROR at setup", "ERROR collecting")) else "测试清理"
            target = frames[-1][0]
            detail = f"{path} 第 {line} 行 · {title} · {phase} · {message}"
        else:
            observed = _FAILED_ASSERTION.search(block)
            if not observed:
                continue
            if path not in trees:
                trees[path] = ast.parse(files[path])
            context, test_side = _status_assertion_context(trees[path], function, line, observed[1])
            target = path if test_side else frames[-1][0]
            if target == path and not test_side:
                target = web if web and _WEB_FAILURE.search(message) else entry.path if entry else path
            detail = f"{path} 第 {line} 行 · {title} · {message}"
            if context:
                detail += f" · {context}"
        issues[target, path, line, title] = {"path": target, "message": detail[:500]}
        if len(issues) >= 6:
            break
    return list(issues.values())


def _assertion_location(detail: str, source: str) -> str:
    messages = re.findall(r"(?:\bFAIL:|AssertionError:)\s*([^·]+)", detail)
    for number, line in enumerate(source.splitlines(), start=1):
        if any(len(message.strip()) >= 6 and message.strip() in line for message in messages):
            return f"第 {number} 行 {line.strip()[:230]} · "
    return ""


def _run_test_status(suite: Invocation, code: int, detail: str) -> str:
    if code != 0:
        return "failed"
    if "pytest" in suite.argv and re.search(r"\b\d+ (?:skipped|xfailed|deselected)\b", detail):
        return "passed" if re.search(r"\b[1-9]\d* passed\b", detail) else "skipped"
    if suite.argv[:2] == ("node", "--test") and re.search(r"(?:^| · )# pass 0\b", detail):
        return "skipped"
    return "passed"


def _suite_owns_backend(suite: Invocation, entry: Invocation | None, files: dict[str, str]) -> bool:
    if not entry:
        return False
    # 识别随包测试自己启动的同一个服务，给它独占的本地端口。
    basename = PurePosixPath(entry.path).name
    for argument in suite.argv:
        path = (PurePosixPath(suite.cwd) / argument).as_posix()
        source = files.get(path, "")
        if re.search(r"\b(?:subprocess\.Popen|spawn)\s*\(", source) and re.search(rf"['\"][^'\"\r\n]*{re.escape(basename)}['\"]", source):
            return True
    return False


def _suite_sources(suite: Invocation, files: dict[str, str]) -> list[str]:
    paths = ((PurePosixPath(suite.cwd) / argument).as_posix() for argument in suite.argv)
    return list(dict.fromkeys(path for path in paths if path in files and _is_test(path)))


def _pytest_passed_cases(output: str, suite: Invocation, test_files: list[str]) -> list[str]:
    _, marker, summary = output.rpartition(" short test summary info ")
    if not marker:
        return []
    observed: dict[str, None] = {}
    for node_id in re.findall(r"(?m)^PASSED ([^\r\n]+)", summary):
        filename, _, name = node_id.partition("::")
        name = name.partition("[")[0]
        path = filename if filename in test_files else (PurePosixPath(suite.cwd) / filename).as_posix()
        if path in test_files and all(part.isidentifier() for part in name.split("::")):
            observed[f"{path}::{name}"] = None
    return list(observed)


def _run_tests(job: Job, files: dict[str, str], readme_path: str, entry: Invocation | None, web: str | None) -> None:
    suites, scripts = _test_commands(files)
    if not suites:
        job.add("tests", "随包功能测试", "skipped", "当前运行环境没有匹配的可执行 Python 或 Node 测试")
    job.readme_edits = _standalone_test_edits(files.get(readme_path, ""), files, scripts)
    if job.readme_edits:
        names = "、".join(sorted({command.split()[-1] for command in job.readme_edits.values()}))
        job.add("test_command", "测试启动说明", "failed",
                f"{names} 是独立脚本，pytest 不会执行其主函数。请在 README 写清实际启动方式。", readme_path)
    ordered = sorted(((suite, _suite_owns_backend(suite, entry, files)) for suite in suites), key=lambda pair: pair[1])
    case_budget = 8192
    for number, (suite, owns_backend) in enumerate(ordered[:12], start=1):
        identifier = f"tests.{number}"
        if unsupported := _unsupported_dependencies(suite, files, versions=job.python_versions):
            job.add(identifier, "随包功能测试", "skipped",
                    f"{suite.path} 所需依赖超出离线环境范围（{'、'.join(unsupported[:6])}）")
            continue
        if owns_backend:
            job.stop()
        process, log = job.start(suite)
        limit = min(job.deadline, time.monotonic() + 30)
        while process.poll() is None and time.monotonic() < limit:
            job.tick()
            time.sleep(0.12)
        if process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            job.add(identifier, "随包功能测试", "failed", f"{suite.path} 运行超时", suite.path)
            return
        detail = job.details(log)
        status = _run_test_status(suite, process.returncode, detail)
        issues = []
        if status == "failed" and "pytest" in suite.argv:
            output = job.output(log, limit=32_768)
            issues = _fixture_scope_failures(output, suite, files) or _pytest_case_failures(output, suite, files, entry, web)
        path = "" if issues else _test_failure_path(detail, suite, entry, web)
        location = _assertion_location(detail, files[suite.path]) if status == "failed" and not issues else ""
        if issues:
            job.issues.extend(issues)
            detail = " · ".join([*(item["message"] for item in issues[:2]), detail])
        test_files = _suite_sources(suite, files)
        test_cases = []
        if status == "passed" and "pytest" in suite.argv:
            for case in _pytest_passed_cases(job.output(log, limit=32_768), suite, test_files):
                size = len(case.encode()) + 3
                if len(case) <= 360 and size <= case_budget:
                    test_cases.append(case)
                    case_budget -= size
        job.add(identifier, "随包功能测试", status,
                f"{suite.path} {location}退出码 {process.returncode}" + (f" · {detail}" if detail else ""),
                path, files=test_files, test_cases=test_cases)
    if len(suites) > 12:
        job.add("tests.limit", "随包功能测试", "skipped", f"有 {len(suites) - 12} 组测试超出单轮验收范围")


def _vite_project(index: str, files: dict[str, str]) -> ViteProject | None:
    path = (PurePosixPath(index).parent / "package.json").as_posix()
    manifest = json.loads(files.get(path, "{}"))
    if not isinstance(manifest, dict):
        raise ValueError(f"{path} 应为 JSON 对象")
    scripts = manifest.get("scripts") or {}
    if not isinstance(scripts, dict):
        raise ValueError(f"{path} 的 scripts 应为对象")
    if _VITE_BUILD.search(str(scripts.get("build"))) or "vite" in dependency_ranges(manifest, {}):
        return ViteProject(path, manifest, scripts)
    return None


def _prepare_web(job: Job, index: str, files: dict[str, str]) -> ViteProject | None:
    """先建立依赖链接，再启动项目源码，避免与子进程争用目录。"""
    identifier, label = "web_build", "前端生产构建"
    path = (PurePosixPath(index).parent / "package.json").as_posix()
    try:
        project = _vite_project(index, files)
        if not project:
            return None
        manager = project.manifest.get("packageManager", "npm")
        directory = PurePosixPath(project.cwd)
        if (not isinstance(manager, str) or manager.split("@", 1)[0] != "npm"
                or project.manifest.get("workspaces")
                or any((directory / name).as_posix() in files for name in ("pnpm-lock.yaml", "yarn.lock", "bun.lock"))):
            job.add(identifier, label, "skipped", f"{path} 使用的包管理方式超出当前离线 npm 构建范围")
            return None
        lock_path = (directory / "package-lock.json").as_posix()
        lock = json.loads(files.get(lock_path, "{}"))
        if not isinstance(lock, dict):
            job.add(identifier, label, "failed", f"{lock_path} 应为 JSON 对象", lock_path)
            return None
        requested = dependency_ranges(project.manifest, lock)
        if "vite" not in requested or not isinstance(project.scripts.get("build"), str) or not project.scripts["build"].strip():
            job.add(identifier, label, "failed", f"{path} 需声明 Vite 依赖和可执行的 build 脚本", path)
            return None
        unsupported = provide_dependencies(job.root / project.cwd, project.manifest, lock, _CHILD_USER)
    except ValueError as error:
        job.add(identifier, label, "failed", f"{path} 无法解析 · {error}", path)
        return None
    if unsupported:
        job.add(identifier, label, "skipped", f"{path} 需要的版本超出离线环境范围（{'、'.join(unsupported[:6])}）")
        return None
    return project


def _vite_port(project: ViteProject, readme: str, backend_port: int | None) -> int:
    for name in ("preview", "dev"):
        command = re.search(rf"\bnpm\s+run\s+{name}\b[^\r\n`]*", readme)
        explicit = _command_port(command[0]) if command else None
        if explicit := explicit or _command_port(str(project.scripts.get(name, ""))):
            return int(explicit)
    urls = {int(value) for value in re.findall(r"http://(?:127\.0\.0\.1|localhost):(\d{2,5})\b", readme)
            if 0 < int(value) <= 65535 and int(value) != backend_port}
    return next(iter(urls)) if len(urls) == 1 else 5173 if backend_port != 5173 else 4173


def _build_failure(output: str, project: ViteProject, files: dict[str, str]) -> tuple[str, str]:
    directory = PurePosixPath(project.cwd)
    for line in output.splitlines():
        if line.lstrip().startswith((">", "at ")) or not _BUILD_DIAGNOSTIC.search(line):
            continue
        for path in files:
            candidate = PurePosixPath(path)
            if (candidate.is_relative_to(directory) and candidate.suffix in _WEB_COMPONENTS + (".css", ".html", ".json")
                    and any(re.search(rf"(?<![\w./-]){re.escape(name)}(?![\w./-])", line)
                            for name in (path, candidate.relative_to(directory).as_posix()))):
                return path, line.strip()[:260]
    return project.path, ""


def _start_vite(job: Job, project: ViteProject, index: str, files: dict[str, str], port: int) -> bool:
    process, log = job.start(Invocation(project.path, project.cwd, (*NPM, "run", "build")))
    limit = min(job.deadline, time.monotonic() + 25)
    while process.poll() is None and time.monotonic() < limit:
        job.tick()
        time.sleep(0.12)
    if process.poll() is None:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        job.add("web_build", "前端生产构建", "failed", f"{project.path} 的 npm run build 超时", project.path)
        return False
    if process.returncode:
        path, location = _build_failure(job.output(log), project, files)
        diagnostics = dict.fromkeys([location, *job.details(log).split(" · ")])
        detail = " · ".join(value for value in diagnostics if value)
        job.add("web_build", "前端生产构建", "failed", f"{path} 构建退出码 {process.returncode} · {detail}", path)
        return False

    job.add("web_build", "前端生产构建", "passed", f"{project.path} 的 npm run build 退出码 0")
    command = ("node", "node_modules/vite/bin/vite.js", "preview", "--host", "127.0.0.1", "--port", str(port), "--strictPort")
    process, log = job.start(Invocation(project.path, project.cwd, command))
    if _await_http(job, process, [port]) is None:
        job.add("web", "前端页面响应", "failed", "Vite 构建产物未能启动预览 · " + job.details(log), project.path)
        return False
    return _probe_frontend(job, index, files[index], port, built=True)


def _start_site(job: Job, files: dict[str, str], scope: str, readme_path: str) -> Site:
    readme = files.get(readme_path, "")
    entry = _entrypoint(files, readme) if scope != "frontend" else None
    web = next((path for path in ("frontend/index.html", "client/index.html", "web/index.html", "index.html")
                if path in files), None)
    vite = _prepare_web(job, web, files) if web and scope != "backend" else None
    web_preparation_failed = bool(job.checks) and job.checks[-1]["id"] == "runtime.web_build"
    has_http = scope == "fullstack" or bool(_api_probes(files)) or bool(
        entry and re.search(r"\b(?:Flask|FastAPI|HTTPServer|createServer|express|listen)\b", files[entry.path]),
    )
    backend_port = None
    blocked = _prepare_python(job, entry, files) if entry else []
    if scope != "frontend" and entry is None:
        job.add("start", "后端启动", "skipped", "启动说明未匹配已支持的 Python 或 Node 入口")
    elif blocked:
        job.add("start", "后端启动", "skipped", f"工程所需依赖超出离线环境范围（{'、'.join(blocked[:6])}）")
    elif entry:
        process, log = job.start(entry)
        if has_http:
            preferred = [int(port) for port in _PORT.findall(readme) if 0 < int(port) <= 65535]
            ports = list(dict.fromkeys([*preferred, 8000, 8765, 5000, 3000, 8080]))[:16]
            backend_port = _await_http(job, process, ports)
            detail = f"已监听本地端口 {backend_port}" if backend_port else "后端未能启动 HTTP 服务 " + job.details(log)
            job.add("start", "后端启动", "passed" if backend_port else "failed", detail, entry.path)
            if backend_port:
                job.env.update(API_BASE_URL=f"http://127.0.0.1:{backend_port}", BASE_URL=f"http://127.0.0.1:{backend_port}")
                _probe_api(job, backend_port, entry, files)
        else:
            limit = min(job.deadline, time.monotonic() + 8)
            while process.poll() is None and time.monotonic() < limit:
                job.tick()
                time.sleep(0.12)
            status = "passed" if process.poll() == 0 else "failed"
            detail = "入口执行结束并正常退出" if status == "passed" else "入口未正常退出 " + job.details(log)
            job.add("start", "程序入口", status, detail, entry.path)
    web_port = None
    if scope != "backend":
        assets = _ASSET.findall(files[web]) if web else []
        ports = [int(value) for value in _WEB_PORT.findall(readme) if 0 < int(value) <= 65535]
        if vite and web:
            port = _vite_port(vite, readme, backend_port)
            if port == backend_port:
                job.add("web", "前端页面响应", "failed", f"启动说明中的前后端使用同一个端口 {port}", readme_path)
            elif _start_vite(job, vite, web, files, port):
                web_port = port
        elif web_preparation_failed:
            job.add("web", "前端页面响应", "skipped", "待前端具备可执行的生产构建后检查页面和资源")
        elif backend_port in ports:
            job.add("web", "前端页面响应", "failed", f"启动说明中的前后端使用同一个端口 {backend_port}", readme_path)
        elif web and not any(asset.split("?", 1)[0].endswith((".ts", ".tsx", ".jsx", ".vue")) for asset in assets):
            shared = bool(backend_port and not ports and _same_frontend(backend_port, files[web]))
            port = backend_port if shared else next(iter(ports), 8766 if backend_port != 8766 else 8767)
            if not shared:
                root = PurePosixPath(web).parent.as_posix()
                process, log = job.start(Invocation(web, root, (*_PYTHON, "-m", "http.server", str(port), "--bind", "127.0.0.1")))
                if _await_http(job, process, [port]) is None:
                    job.add("web", "前端页面响应", "failed", "页面服务启动失败 " + job.details(log), web)
                    port = None
            if port is not None and _probe_frontend(job, web, files[web], port, shared=shared):
                web_port = port
        else:
            job.add("web", "前端页面响应", "skipped", "未匹配离线验收支持的静态入口或 Vite 工程")
    if web_port is not None:
        job.env["FRONTEND_URL"] = f"http://127.0.0.1:{web_port}"
    failed = blocked or any(check["status"] == "failed" and check["id"] == "runtime.start" for check in job.checks)
    return Site(entry, backend_port, web, web_port, bool(failed))


def _browser_issue_file(site: Site, files: dict[str, str], problem: dict) -> str:
    kind = problem.get("kind")
    selector = problem.get("selector") or ""
    unknown_id = (kind == "missing_control" and re.fullmatch(r"#[\w-]+", selector)
                  and not any(re.search(rf"['\"]{re.escape(selector[1:])}['\"]", text)
                              for path, text in files.items() if path.endswith(_WEB_COMPONENTS + (".html",))))
    if kind in {"coverage", "scenario_input"} or unknown_id:
        return next((path for path in files if path.casefold() == BROWSER_CHECKS_PATH), BROWSER_CHECKS_PATH)
    if kind == "api" and site.entry:
        return site.entry.path
    web = site.web or ""
    folder = PurePosixPath(web).parent
    asset = str(problem.get("path") or "").lstrip("/").split("?", 1)[0]
    if asset and (file := (folder / asset).as_posix()) in files:
        return file
    if kind in {"script", "action", "missing_control"}:
        if selector.startswith("#"):
            matches = [path for path, source in files.items() if path.endswith(_WEB_COMPONENTS)
                       and PurePosixPath(path).is_relative_to(folder)
                       and re.search(rf"['\"]{re.escape(selector[1:])}['\"]", source)]
            if len(matches) == 1:
                return matches[0]
        for name in _ASSET.findall(files.get(web, "")):
            file = (folder / name.lstrip("/").split("?", 1)[0].removeprefix("./")).as_posix()
            if file in files and file.endswith(_WEB_COMPONENTS):
                return file
    return web


_BROWSER_CALL_TYPE_ERROR = re.compile(
    r"(?:Uncaught(?: \(in promise\))? )?TypeError: "
    r"([a-zA-Z_$][\w$]*(?:\.[a-zA-Z_$][\w$]*){2,8}) is not a function",
    re.ASCII,
)
_BROWSER_SOURCE_NON_CODE = re.compile(
    r"//[^\r\n]*|/\*[\s\S]*?\*/|<!--[\s\S]*?-->|"
    r'"(?:\\.|[^"\\\r\n])*"|'
    r"'(?:\\.|[^'\\\r\n])*'|`(?:\\.|[^`\\])*`",
)


def _browser_call_source(
    site: Site, files: dict[str, str], diagnostic: str,
) -> tuple[str, tuple[str, int] | None] | None:
    error = _BROWSER_CALL_TYPE_ERROR.fullmatch(diagnostic)
    if not error or not site.web:
        return None
    # 构建可能缩短根变量名，Vue 也可能展开 ref。其余成员必须精确匹配。
    members = error[1].split(".")[1:]
    if len(members) > 2 and members[0] == "value":
        members = members[1:]
    access = re.compile(r"(?<![\w$])" + r"\s*\.\s*".join(map(re.escape, members)) + r"\s*\(")
    frontend = PurePosixPath(site.web).parent
    location = None
    for path, source in files.items():
        if not path.endswith(_WEB_COMPONENTS) or not PurePosixPath(path).is_relative_to(frontend):
            continue
        code = _BROWSER_SOURCE_NON_CODE.sub(lambda match: "\n" * match[0].count("\n") + " ", source)
        for line, text in enumerate(code.splitlines(), 1):
            if access.search(text):
                if location is not None:
                    return ".".join(members), None
                location = (path, line)
    return ".".join(members), location


def _read_browser_probe(stage: Job, site: Site, scenarios: list[dict], readme: str) -> dict:
    browser_home = stage.area / "probe"
    browser_home.mkdir(mode=0o700)
    os.chown(browser_home, _BROWSER_USER, _BROWSER_USER)
    hostname = "localhost" if re.search(rf"\bhttp://localhost:{site.web_port}\b", readme) else "127.0.0.1"
    origin = f"http://{hostname}:{site.web_port}"
    ports = {site.web_port, site.backend_port} - {None}
    allowed = sorted(f"http://{host}:{port}" for port in ports for host in ("localhost", "127.0.0.1"))
    request = browser_home / "checks.json"
    form_scopes = {step["selector"]: scope for scenario in scenarios for step in scenario["steps"]
                   if (scope := browser_form_scope(step))}
    write_json(request, {
        "origin": origin, "allowed": allowed, "scenarios": scenarios, "form_scopes": form_scopes,
        **({"capture_browser_errors": True} if stage.capture_browser_errors else {}),
    })
    os.chown(request, _BROWSER_USER, _BROWSER_USER)
    invocation = Invocation(site.web or "", ".", (sys.executable, "-I", "-B", str(_BROWSER_PROBE), str(request)))
    process, log = stage.start(invocation, browser_home=browser_home)
    until = min(stage.deadline, time.monotonic() + 35)
    while process.poll() is None and time.monotonic() < until:
        stage.tick()
        time.sleep(0.12)
    if process.poll() is None:
        return {"environment": "本轮隔离浏览器超时"}
    if process.returncode != 0:
        return {"environment": "隔离浏览器未返回检查结果"}
    log.seek(0)
    try:
        data = log.read(MAX_RESULT_BYTES + 1)
        result = json.loads(data) if len(data) <= MAX_RESULT_BYTES else None
    except (ValueError, RecursionError):
        result = None
    return result if isinstance(result, dict) else {"environment": "隔离浏览器返回了无效报告"}


def _browser_fixture(job: Job, files: dict[str, str], scope: str, readme_path: str, scenario: dict | None) -> tuple[Site, dict]:
    # 每个场景使用独立工程副本和临时库，避免前一个场景产生的记录成为本场景的前置结果。
    with tempfile.TemporaryDirectory(prefix="browser-site-", dir=job.area) as directory:
        area = Path(directory)
        area.chmod(0o711)
        stage = Job(job.request, job.deadline, area, capture_browser_errors=job.capture_browser_errors)
        try:
            _files_for(stage, {"files": files, "source_digest": source_digest(files)})
            site = _start_site(stage, files, scope, readme_path)
            if site.web_port is None or site.blocked or (scope == "fullstack" and site.backend_port is None):
                return site, {"environment": "浏览器使用的独立工程实例未启动"}
            return site, _read_browser_probe(stage, site, [scenario] if scenario else [], files.get(readme_path, ""))
        finally:
            stage.stop()


def _browser_checks(job: Job, files: dict[str, str], scope: str, readme_path: str, original: Site) -> None:
    if original.web_port is None or original.blocked or (scope == "fullstack" and original.backend_port is None):
        job.add("browser", "浏览器页面加载", "skipped", "本地页面和所需服务启动后才能验证浏览器交互")
        return
    checks_path = next((path for path in files if path.casefold() == BROWSER_CHECKS_PATH), BROWSER_CHECKS_PATH)
    try:
        scenarios = parse_browser_checks(files[checks_path]) if checks_path in files else []
    except ValueError as error:
        job.add("browser", "浏览器场景", "failed", str(error), checks_path)
        return

    job.stop()
    for index, scenario in enumerate(scenarios or [None], 1):
        site, report = _browser_fixture(job, files, scope, readme_path, scenario)
        smoke = report.get("smoke")
        if not isinstance(smoke, dict):
            message = report.get("environment") or "隔离浏览器返回了不完整的检查结果"
            job.add("browser" if index == 1 else f"browser.flow.{index}", "浏览器验收环境", "failed", str(message))
            return
        if index == 1:
            if smoke:
                skipped = smoke.get("kind") == "offline_dependency"
                job.add("browser", "浏览器页面加载", "skipped" if skipped else "failed", str(smoke.get("message") or "页面加载失败"),
                        _browser_issue_file(site, files, smoke))
                return
            job.add("browser", "浏览器页面加载", "passed", "Chromium 已加载首页、可见内容及所需脚本和样式")
        if scenario:
            identifier = f"browser.flow.{index}"
            label = f"浏览器 · {scenario['name']}"
            outcomes = report.get("scenarios")
            if smoke:
                outcome = smoke
            elif isinstance(outcomes, list) and len(outcomes) == 1 and isinstance(outcomes[0], dict):
                outcome = outcomes[0]
            else:
                job.add(identifier, label, "failed", "未取得该浏览器操作场景的结果")
                continue
            if outcome.get("steps") == len(scenario["steps"]):
                job.add(identifier, label, "passed", f"已执行并核对 {outcome['steps']} 步真实页面操作")
            else:
                skipped = outcome.get("kind") == "offline_dependency"
                where = _browser_issue_file(site, files, outcome)
                message = outcome.get("message", "")
                positions = outcome.get("form_scope_steps")
                if (isinstance(positions, list) and 1 <= len(positions) <= 5
                        and all(type(position) is int and 1 < position <= len(scenario["steps"]) for position in positions)):
                    message += "。真实表单字段已唯一定位，需更新步骤 " + ",".join(map(str, positions))
                if (outcome.get("kind") == "document" and outcome.get("step") == 1 and site.web
                        and scenario["steps"][0]["path"] == "/" + site.web):
                    where, message = checks_path, f"使用了源文件路径作为页面 URL。页面已从 / 成功加载 · {message}"
                detail = f"{checks_path} 「{scenario['name']}」第 {outcome.get('step', 1)} 步。{message}"
                diagnostic = outcome.get("console_error")
                if job.capture_browser_errors and not skipped and isinstance(diagnostic, str) and diagnostic:
                    suffix = f" · 浏览器脚本异常 {diagnostic[:180]}"
                    if source_hint := _browser_call_source(site, files, diagnostic[:180]):
                        members, location = source_hint
                        if location:
                            where, line = location
                            display_path = where if len(where) <= 120 else f"…/{PurePosixPath(where).name}"
                            suffix += f" · 源码候选 {display_path}:{line}"
                        else:
                            suffix += f" · 源码位置未确认，请在前端查找 {members}"
                    detail = detail[:500 - len(suffix)] + suffix
                job.add(identifier, label, "skipped" if skipped else "failed", detail, where)
        else:
            job.add("browser.flows", "浏览器操作场景", "skipped", "工程未交付可执行的浏览器操作场景")


def _run_project(job: Job, files: dict[str, str], scope: str) -> None:
    readme_path = next((path for path in files if path.casefold() == "readme.md"), "README.md")
    site = _start_site(job, files, scope, readme_path)
    if site.blocked:
        job.add("tests", "随包功能测试", "skipped", "后端启动后再执行依赖服务的测试")
    else:
        _run_tests(job, files, readme_path, site.entry, site.web)
    if scope != "backend":
        _browser_checks(job, files, scope, readme_path, site)


def _capture_openapi(job: Job, files: dict[str, str]) -> dict | None:
    readme = next((path for path in files if path.casefold() == "readme.md"), "README.md")
    site = _start_site(job, files, "backend", readme)
    if site.backend_port is None or site.blocked:
        return None
    job.tick()
    try:
        status, media, raw, _ = _http(site.backend_port, "/openapi.json")
        if status == 200 and "json" in media.casefold() and len(raw) <= MAX_OPENAPI_BYTES:
            contract = json.loads(raw)
            if openapi_source(contract) is not None:
                return contract
    except (OSError, http.client.HTTPException, ValueError, RecursionError):
        pass
    return None


def _files_for(job: Job, payload: dict) -> dict[str, str]:
    files = payload.get("files")
    if not isinstance(files, dict) or not 0 < len(files) <= MAX_PROJECT_FILES:
        raise ValueError("没有可验收的项目源码")
    budget = MAX_PROJECT_BYTES
    job.root.mkdir()
    for path, content in sorted(files.items()):
        if not valid_job_path(path) or not isinstance(content, str):
            raise ValueError("文件路径或正文无效")
        data = content.encode("utf-8")
        budget -= len(data)
        if len(data) > MAX_PROJECT_FILE_BYTES or budget < 0:
            raise ValueError("工程源码超过验收容量")
        target = job.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    for directory in (job.root, *(path for path in job.root.rglob("*") if path.is_dir())):
        # 已提交的源码由工作进程持有。应用仍可在目录中创建临时数据库和输出文件。
        os.chown(directory, 0, _CHILD_USER)
        directory.chmod(0o1770)
    if source_digest(files) != payload.get("source_digest"):
        raise ValueError("工程源码指纹不匹配")
    return files


def _verify(request: Path, payload: dict, remaining: float) -> dict:
    with tempfile.TemporaryDirectory(prefix="project-") as directory:
        area = Path(directory)
        area.chmod(0o711)
        job = Job(
            request, time.monotonic() + remaining, area,
            capture_browser_errors=payload.get("capture_browser_errors") is True,
        )
        contract = None
        try:
            files = _files_for(job, payload)
            if payload.get("capture_openapi") is True:
                contract = _capture_openapi(job, files)
            else:
                _run_project(job, files, payload["scope"])
        except VerificationCancelled:
            if not request.exists():
                raise
            job.add("deadline", "工程验收时限", "failed", "运行超过本次隔离验收时限")
        except (OSError, SyntaxError, TypeError, ValueError, subprocess.SubprocessError):
            job.add("environment", "工程验收环境", "failed", "验收容器无法读取工程或启动检查进程")
        finally:
            job.stop()
        return {"source_digest": payload.get("source_digest"), "checks": job.checks, "issues": job.issues,
                "readme_edits": job.readme_edits, **({"openapi": contract} if contract is not None else {})}


def serve() -> None:
    import fcntl

    if (os.geteuid() != 0 or not Path("/.dockerenv").is_file()
            or {device.name for device in Path("/sys/class/net").iterdir()} - {"lo"}):
        raise SystemExit("验收进程须在仅有回环接口的独立 Docker 容器运行")
    signal.signal(signal.SIGALRM, _http_timeout)
    _QUEUE.chmod(0o700)
    with (_QUEUE / "worker.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        _cleanup_children()
        while True:
            (_QUEUE / "heartbeat").touch()
            jobs = sorted(path for path in _QUEUE.iterdir() if JOB_NAME.fullmatch(path.name))
            for folder in jobs:
                try:
                    request = folder / "request.json"
                    result = folder / "result.json"
                    if not request.exists():
                        if time.time() - folder.stat().st_mtime > JOB_TIMEOUT_SECONDS + 30:
                            shutil.rmtree(folder)
                        continue
                    payload = read_json(request, MAX_JOB_BYTES)
                    expires = payload.get("deadline")
                    if not isinstance(expires, (float, int)) or not math.isfinite(expires):
                        continue
                    remaining = min(expires - time.time(), JOB_TIMEOUT_SECONDS)
                    if remaining <= -30:
                        shutil.rmtree(folder)
                    elif remaining > 0 and not result.exists() and payload.get("scope") in {"fullstack", "frontend", "backend"}:
                        write_json(result, _verify(request, payload, remaining))
                except (FileNotFoundError, ValueError, VerificationCancelled):
                    continue
            time.sleep(0.2)


if __name__ == "__main__":
    serve()
