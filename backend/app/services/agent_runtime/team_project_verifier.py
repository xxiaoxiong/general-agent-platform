"""工程协作图与离线验收容器的交接。"""

from __future__ import annotations

import asyncio
import ast
import json
import posixpath
import re
import shutil
import time
from pathlib import Path
from uuid import uuid4

from app.core.config import settings

from .events import redact_secrets
from .project_verifier_protocol import (
    HEARTBEAT_SECONDS, JOB_NAME, JOB_TIMEOUT_SECONDS, MAX_RESULT_BYTES, read_json, source_digest, write_json,
)

_PYTHON_TEST_COMMAND = re.compile(r"[ \t]*python3? (?:\./)?[\w./-]+\.py\Z")
_MISSING_TS_CONFIG = re.compile(r"error TS6053: File '([^']+/tsconfig\.node\.json)' not found\.")
_TS_REFERENCES = re.compile(r'(?P<indent>[ \t]+)"references"\s*:\s*(?P<array>\[[^\r\n]*\])(?P<comma>,?)\s*\Z')
_TMP_FIXTURE_SCOPE = re.compile(
    r"夹具 ([A-Za-z_]\w*) · ScopeMismatch: You tried to access the function scoped fixture tmp_path "
    r"with a (class|module|package|session) scoped request object\.",
)


def _process_fixture_edits(source: str) -> dict[str, str]:
    """只修复调用方明确接收进程对象、fixture 却裸 yield 的情形。"""
    try:
        tree = ast.parse(source.lstrip("\ufeff"))
    except (SyntaxError, ValueError):
        return {}
    functions = [node for node in tree.body if isinstance(node, ast.FunctionDef)]
    lines = source.splitlines()
    edits = {}
    for fixture in functions:
        if not any(ast.unparse(decorator).startswith("pytest.fixture") for decorator in fixture.decorator_list):
            continue
        for start, yielded, stop in zip(fixture.body, fixture.body[1:], fixture.body[2:]):
            if not (isinstance(start, ast.Assign) and len(start.targets) == 1 and isinstance(start.targets[0], ast.Name)
                    and isinstance(start.value, ast.Call) and isinstance(start.value.func, ast.Name)
                    and isinstance(yielded, ast.Expr) and isinstance(yielded.value, ast.Yield) and yielded.value.value is None
                    and isinstance(stop, ast.Expr) and isinstance(stop.value, ast.Call) and isinstance(stop.value.func, ast.Name)):
                continue
            process = start.targets[0].id
            if [ast.unparse(arg) for arg in stop.value.args] != [process]:
                continue
            starts_process = any(node.name == start.value.func.id and "subprocess.Popen(" in ast.unparse(node) for node in functions)
            caller = f"{stop.value.func.id}({fixture.name})"
            consumed = any(node.name.startswith("test_") and fixture.name in {arg.arg for arg in node.args.args}
                           and caller in ast.unparse(node) for node in functions)
            old = lines[yielded.lineno - 1]
            if starts_process and consumed and old.strip() == "yield" and lines.count(old) == 1:
                edits[old] = f"{old} {process}"
    return edits


def _vite_reference_edits(path: str, files: dict[str, str], finding: str) -> dict[str, str]:
    """仅删除已由编译器证实不存在的 Vite 配置引用。"""
    directory = posixpath.dirname(path)
    missing = set(_MISSING_TS_CONFIG.findall(finding)) - files.keys()
    if (not path.endswith("tsconfig.json") or not missing
            or not any(posixpath.join(directory, name) in files for name in ("vite.config.ts", "vite.config.js", "vite.config.mjs"))):
        return {}
    for line in files[path].splitlines():
        match = _TS_REFERENCES.fullmatch(line)
        if not match:
            continue
        try:
            references = json.loads(match["array"])
        except ValueError:
            continue
        kept = [entry for entry in references if not (isinstance(entry, dict) and isinstance(entry.get("path"), str)
                and posixpath.normpath(posixpath.join(directory, entry["path"])) in missing)]
        if len(kept) != len(references):
            value = json.dumps(kept, ensure_ascii=False)
            return {line: f'{match["indent"]}"references": {value}{match["comma"]}'}
    return {}


def _fixture_scope_edits(source: str, finding: str) -> dict[str, str]:
    if not (match := _TMP_FIXTURE_SCOPE.search(finding)):
        return {}
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return {}
    lines = source.splitlines()
    for function in tree.body:
        if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)) or function.name != match[1]:
            continue
        if not any(argument.arg == "tmp_path" for argument in function.args.args + function.args.kwonlyargs):
            continue
        for decorator in function.decorator_list:
            if (not isinstance(decorator, ast.Call) or ast.unparse(decorator.func) != "pytest.fixture"
                    or decorator.lineno != decorator.end_lineno):
                continue
            old = lines[decorator.lineno - 1]
            new = re.sub(rf"(\bscope\s*=\s*)(['\"]){match[2]}\2", r"\g<1>\g<2>function\g<2>", old, count=1)
            if old != new and lines.count(old) == 1:
                return {old: new}
    return {}


def _unavailable(digest: str, detail: str) -> dict:
    return {
        "source_digest": digest, "checks": [
            {"id": "runtime.environment", "label": "工程验收环境", "status": "failed", "detail": detail},
        ], "issues": [],
    }


async def verify_runtime(
    files: dict[str, str], scope: str, *, capture_openapi: bool = False, capture_browser_errors: bool = False,
) -> dict | None:
    directory = settings.AGENT_PROJECT_VERIFIER_DIR.strip()
    if not directory:
        return None
    queue = Path(directory)
    digest = source_digest(files)
    heartbeat = queue / "heartbeat"
    job = queue / f"job-{uuid4().hex}"
    created = False
    expires = time.time() + JOB_TIMEOUT_SECONDS
    try:
        if time.time() - heartbeat.stat().st_mtime > HEARTBEAT_SECONDS:
            return _unavailable(digest, "离线验收容器暂时不可用，已保留工程草稿")
        if sum(JOB_NAME.fullmatch(path.name) is not None for path in queue.iterdir()) >= 24:
            return _unavailable(digest, "离线验收队列已满，已保留工程草稿")
        job.mkdir(mode=0o700)
        created = True
        await asyncio.to_thread(write_json, job / "request.json", {
            "source_digest": digest, "scope": scope, "files": files, "deadline": expires,
            **({"capture_openapi": True} if capture_openapi else {}),
            **({"capture_browser_errors": True} if capture_browser_errors else {}),
        })
        while time.time() < expires:
            if (response := job / "result.json").is_file():
                receipt = await asyncio.to_thread(read_json, response, MAX_RESULT_BYTES)
                if receipt.get("source_digest") != digest:
                    return _unavailable(digest, "运行报告与交付源码版本不一致")
                return receipt
            if time.time() - heartbeat.stat().st_mtime > HEARTBEAT_SECONDS:
                return _unavailable(digest, "离线验收容器未完成本轮检查，已保留工程草稿")
            await asyncio.sleep(0.25)
        return _unavailable(digest, "离线验收超过等待时限，已保留工程草稿")
    except (OSError, ValueError):
        return _unavailable(digest, "离线验收容器或报告暂不可用，已保留工程草稿")
    finally:
        if created:
            await asyncio.to_thread(shutil.rmtree, job, ignore_errors=True)


def merge_runtime_report(verification: dict, receipt: dict, files: dict[str, str]) -> dict:
    digest = verification["source_digest"]
    if receipt.get("source_digest") != digest or not isinstance(receipt.get("checks"), list):
        receipt = _unavailable(digest, "运行报告与交付源码版本不一致")
    checks = []
    identifiers = set()
    for item in receipt["checks"][:24]:
        if not isinstance(item, dict):
            continue
        identifier, status = item.get("id"), item.get("status")
        if (not isinstance(identifier, str) or not identifier.startswith("runtime.") or identifier in identifiers
                or status not in ("passed", "failed", "skipped")):
            continue
        identifiers.add(identifier)
        is_suite = identifier.startswith("runtime.tests.")
        test_files = [path for path in item["files"][:24] if isinstance(path, str) and path in files
                      ] if is_suite and isinstance(item.get("files"), list) else []
        test_cases = []
        if status == "passed" and test_files and isinstance(item.get("test_cases"), list):
            for case in item["test_cases"]:
                if not isinstance(case, str) or len(case) > 360:
                    continue
                path, _, name = case.partition("::")
                if path in test_files and path.endswith(".py") and all(part.isidentifier() for part in name.split("::")):
                    test_cases.append(case)
        checks.append({
            "id": identifier, "status": status, "label": str(item.get("label") or "运行检查")[:100],
            "detail": str(redact_secrets(item.get("detail") or ""))[:1200],
            **({"files": test_files} if test_files else {}),
            **({"test_cases": list(dict.fromkeys(test_cases))} if test_cases else {}),
        })
    if not checks:
        checks = _unavailable(digest, "未取得有效的运行验收结果")["checks"]
    raw_issues = receipt.get("issues")
    issues = [{"path": item["path"], "message": str(redact_secrets(item["message"]))[:500]}
              for item in raw_issues[:30] if isinstance(item, dict) and isinstance(item.get("path"), str)
              and item["path"] in files and isinstance(item.get("message"), str)] if isinstance(raw_issues, list) else []
    readme_edits = {}
    if "runtime.test_command" in identifiers and isinstance(edits := receipt.get("readme_edits"), dict):
        readme = next((source for path, source in files.items() if path.casefold() == "readme.md"), "")
        lines = readme.splitlines()
        readme_edits = {old: new for old, new in list(edits.items())[:12] if isinstance(new, str)
                        and "pytest" in old and lines.count(old) == 1 and len(new) <= 220
                        and _PYTHON_TEST_COMMAND.fullmatch(new)}
    line_repairs = dict(verification.get("line_repairs") or {})
    build_failed = any(item["id"] == "runtime.web_build" and item["status"] == "failed" for item in checks)
    for issue in issues:
        path = issue["path"]
        if path.endswith(".py") and re.search(r"'NoneType'.*'(?:terminate|kill|wait|poll)'", issue["message"]):
            if edits := _process_fixture_edits(files[path]):
                line_repairs.setdefault(path, {}).update(edits)
        elif path.endswith(".py") and (edits := _fixture_scope_edits(files[path], issue["message"])):
            line_repairs.setdefault(path, {}).update(edits)
        elif build_failed and (edits := _vite_reference_edits(path, files, issue["message"])):
            line_repairs[path] = edits
    executed = any(item["id"] != "runtime.environment" and item["status"] != "skipped" for item in checks)
    return {
        **verification, "scope": "sandbox" if executed else "static",
        "passed": verification["passed"] and all(item["status"] != "failed" for item in checks),
        "checks": [item for item in verification["checks"] if item["id"] != "runtime"] + checks,
        "issues": verification["issues"] + issues,
        "line_repairs": line_repairs,
        "runtime_result": {"source_digest": digest, "checks": checks, "issues": issues, "readme_edits": readme_edits},
    }
