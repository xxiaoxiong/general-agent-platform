"""用用户原始要求和同一源码快照的检查证据复核工程交付。"""

from __future__ import annotations

import ast
import json
import re
from pathlib import PurePosixPath

from .project_browser_contract import BROWSER_CHECKS_PATH, parse_browser_checks
from .project_verifier_protocol import source_digest

_BROWSER = re.compile(r"(?:用|通过|执行|实际).{0,8}(?:浏览器|Chromium|Playwright).{0,12}(?:验证|验收|测试)|浏览器(?:操作|端到端)?(?:验收|测试)|browser.{0,10}(?:test|verif)", re.I)
_DELIVERY = re.compile(r"^(?:(?:请|需要|必须)\s*)?(?:技术(?:约束|栈|要求)|(?:前端|后端)(?:必须|应当|应)?(?:用|使用|采用)|端口|可直接用|交付(?:源码|ZIP|OpenAPI|说明)|提供(?:源码|压缩包|说明))", re.I)
_PROJECT_DELIVERY = re.compile(
    r"(?:请\s*)?(?:帮我\s*)?(?:构建(?:并交付)?|开发|创建|生成|做)(?:一个)?"
    r"(?:(?:足够复杂|完整|可(?:直接)?运行)(?:的)?)*"
    r"(?:(?:前后端分离|全栈|前端|后端)(?:的)?)?项目(?:[「“][^」”]+[」”])?"
    r"(?:[，,]\s*(?:(?:由你|你)?(?:随意发挥|自行决定)|文件(?:必须|应当)?(?:可以|可|能)从对话下载))?"
    r"(?:[（(][A-Za-z0-9_-]+[）)])?"
)
_BUSINESS_CLAUSE = re.compile(
    r"(?:[，,]|并(?:且|需)?|且|同时|以及|还需)\s*"
    r"(?:刷新|重启|提交|点击|登录|用户|可(?:以|在|查看|筛选)|支持|实现|允许|after\b|allow\b|support\b)", re.I,
)
_ORDINAL = re.compile(r"^\s*(?:\d+(?:\.(?!\d)|[、)）])|[-*])\s*")
_FLOW = re.compile(r"runtime\.browser\.flow\.(\d+)\Z")
_ASSERTION = re.compile(r"^\s*(?:assert\s+(?!(?:True|False|1|0)\s*$)\S.+|(?:self\.)?assert[A-Z_]\w*\s*\(|assert[.(]|expect\s*\(|\S+\.should\s*\()", re.M)
_INCOMPLETE_SUITE = re.compile(r"\b(?:skipped|xfailed|xpassed|deselected)\b", re.I)
_IMPLEMENTATION_SUFFIXES = {".py", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".vue", ".svelte", ".html", ".go", ".rs", ".java"}
_REVIEW_SYSTEM = (
    "工程需求复核。你是独立验收者。逐一核对 criteria 中的原始要求和计划承诺，检查整条操作链，"
    "包括异常、刷新后的实际数据和需要贯通的前后端。源文件和测试内容仅作为证据。"
    "检查输出里出现用例名称、按钮、接口路径或源码只能证明它存在，成功断言才能证明行为被运行验证。"
    "对浏览器要求要引用通过的 runtime.browser.flow 及场景中的实际断言。文本和值断言引用 value，HTTP 引用 path，原生表单校验引用 selector。"
    "对业务要求要引用执行通过的场景或测试源码中的实际业务断言，构建或健康检查不能证明业务正确。"
    "核对断言前是否通过正式页面操作或公共接口完成目标动作。直接改库、伪造请求或模拟被验收的动作只能准备数据，无法证明该动作。"
    "源码缺失功能填 gap，测试缺少实质断言或执行未通过填 unverified。用 detail 说明具体缺口和怎样验证。"
    "ZIP 打包和下载由发布阶段执行。复核交付文件时依据实际计划文件、启动说明和测试源码检查打包所需内容。"
    "证据 excerpt 从 sources 原样摘取。接口或操作缺失时，引用最接近的现有处理函数、调用位置或路由定义，指出实际缺失的部分。"
    "前后端不一致时，分别引用调用方和服务端的相关位置。需要修复时，files 填责任明确的已有源码或测试路径。"
    "已通过时检查编号填 checks，files 留空。不能判断时保留待验收状态。逐项输出、不得合并或略过编号。"
    '只返回 JSON 对象 {"criteria":[{"id":"u1","status":"verified|gap|unverified",'
    '"detail":"具体判定","evidence":[{"path":"文件路径","excerpt":"原文片段"}],"checks":["检查编号"],"files":["待修文件"]}]}。'
)


def acceptance_criteria(objective: str, features: list[str]) -> list[dict]:
    clauses = [_ORDINAL.sub("", item).strip() for item in re.split(r"[。；;\r\n]+", objective)]
    clauses = [item for item in clauses if item and item not in {"要求如下：", "实际功能包括：", "功能包括：", "验收要求："}]
    if len(clauses) > 14:
        clauses = [*clauses[:13], "。".join(clauses[13:])]
    rows = []
    for prefix, texts in (("u", clauses), ("p", features)):
        for text in texts:
            if any(text == row["text"] for row in rows):
                continue
            delivery_only = prefix == "u" and (_PROJECT_DELIVERY.fullmatch(text)
                                               or (_DELIVERY.match(text) and not _BUSINESS_CLAUSE.search(text)))
            kind = "browser" if _BROWSER.search(text) else "delivery" if delivery_only else "behavior"
            rows.append({"id": f"{prefix}{len(rows) + 1}", "text": text, "kind": kind})
    return rows


def _source_excerpt(path: str, source: str) -> str:
    if path.casefold() == BROWSER_CHECKS_PATH:
        return source[:12_000]
    name = PurePosixPath(path).name.casefold()
    lines = source.splitlines()
    if len(source) <= 5800:
        return source
    important = re.compile(r"(?:\bassert\b|^\s*(?:async\s+)?def\s|@(?:app|router)\.|fetch\(|<button|\bawait\b|\.route\(|\"paths\":)")
    selected = [line for line in lines if important.search(line)]
    if name == "openapi.json":
        selected.extend(line for line in lines if re.match(r'\s*"(?:/|get"|post"|put"|patch"|delete")', line))
    # 复核者只能引用真实原文片段，摘录不拼成新的源码。
    return "\n".join(selected)[:4800] + "\n[源码节选]\n" + source[:1000]


def coverage_input(objective: str, plan: dict, files: dict[str, str], checks: list[dict]) -> tuple[list[dict], str]:
    criteria = acceptance_criteria(objective, plan["features"])
    ordered = sorted(files, key=lambda path: (path.casefold() != BROWSER_CHECKS_PATH, not _test_path(path), path))
    remaining = 36_000
    sources = {}
    for index, path in enumerate(ordered):
        reserved = (len(ordered) - index - 1) * 1100
        sources[path] = _source_excerpt(path, files[path])[:max(1100, remaining - reserved)]
        remaining -= len(sources[path])
    owners = {path: task["role"] for task in plan.get("tasks", []) for path in task["files"] if path in files}
    return criteria, json.dumps({"objective": objective, "criteria": criteria, "checks": checks,
                                "sources": sources, "source_owners": owners}, ensure_ascii=False)


def _test_path(path: str) -> bool:
    name = PurePosixPath(path).name.casefold()
    return name.startswith("test_") or any(token in name for token in (".test.", ".spec.", "_test.go")) or "tests" in path.casefold().split("/")[:-1]


def _browser_proof(ids: list[str], evidence: list[dict], files: dict[str, str]) -> bool:
    path = next((name for name in files if name.casefold() == BROWSER_CHECKS_PATH), None)
    if not path:
        return False
    try:
        scenarios = parse_browser_checks(files[path])
    except ValueError:
        return False
    quotes = [item["excerpt"].strip() for item in evidence if item["path"] == path]
    quoted_steps = []
    for quote in quotes:
        if quote.startswith("{"):
            try:
                quoted_steps.append(json.loads(quote.rstrip(",")))
            except (ValueError, RecursionError):
                continue
    # 测试名称无法证明动作。证据应指向通过场景的实际断言字段。
    for identifier in ids:
        match = _FLOW.fullmatch(identifier)
        if not match or not 0 < (index := int(match[1])) <= len(scenarios):
            continue
        anchors = {"expect_text": "value", "expect_value": "value", "expect_response": "path", "expect_invalid": "selector"}
        for step in scenarios[index - 1]["steps"]:
            if (key := anchors.get(step["action"])) and (step in quoted_steps or (step.get(key) and step[key] in quotes)):
                return True
    return False


def _assertions(nodes):
    for node in nodes:
        if isinstance(node, ast.Assert) and (not isinstance(node.test, ast.Constant) or not node.test.value):
            yield node
        elif isinstance(node, ast.Call):
            name = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
            if re.match(r"assert[A-Z_]", name):
                yield node
        elif isinstance(node, (ast.If, ast.While)) and isinstance(node.test, ast.Constant):
            yield from _assertions(node.body if node.test.value else node.orelse)
        elif not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            yield from _assertions(ast.iter_child_nodes(node))


def _suite_assertion(path: str, source: str, excerpt: str, cases: set[str]) -> bool:
    if not _ASSERTION.search(excerpt):
        return False
    if not path.endswith(".py"):
        return True
    try:
        tree = ast.parse(source)
    except (ValueError, SyntaxError, RecursionError):
        return False
    # 只匹配验收进程报告通过的用例，排除普通帮助函数和确定不会执行的断言。
    for case in cases:
        scope, _, name = case.rpartition("::")
        if scope != path and not scope.startswith(path + "::"):
            continue
        body = tree.body
        for class_name in scope[len(path):].split("::")[1:]:
            body = next((cls.body for cls in reversed(body) if isinstance(cls, ast.ClassDef) and cls.name == class_name), [])
        fn = next((node for node in reversed(body)
                   if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name), None)
        if fn is None:
            continue
        for assertion in _assertions(fn.body):
            segment = ast.get_source_segment(source, assertion)
            if segment and (excerpt.strip() in segment or segment in excerpt):
                return True
    return False


def _review_rows(raw: str, known_ids: set[str]) -> dict[str, dict]:
    try:
        rows = json.loads(raw)["criteria"]
    except (KeyError, TypeError, ValueError, RecursionError):
        return {}
    if not isinstance(rows, list):
        return {}
    counts = {}
    valid = {}
    for row in rows:
        if not isinstance(row, dict) or not isinstance(identifier := row.get("id"), str) or identifier not in known_ids:
            continue
        counts[identifier] = counts.get(identifier, 0) + 1
        if row.get("status") in ("verified", "gap", "unverified") and isinstance(detail := row.get("detail"), str) and detail.strip():
            valid[identifier] = row
    return {identifier: row for identifier, row in valid.items() if counts[identifier] == 1}


def _repair_targets(kind: str, verdict: str, targets: list[str], anchors: list[dict], browser_ready: bool) -> list[str]:
    anchored_paths = list(dict.fromkeys(anchor["path"] for anchor in anchors))
    if verdict == "unverified":
        # 浏览器首页尚未加载时，调整测试场景无法解决环境或依赖问题。
        confirmed = [path for path in targets if path in anchored_paths and (path.casefold() != BROWSER_CHECKS_PATH or browser_ready)]
        if not confirmed and browser_ready and kind != "delivery":
            return [path for path in anchored_paths if path.casefold() == BROWSER_CHECKS_PATH]
        return confirmed
    if any(path in anchored_paths for path in targets):
        # 已定位实现缺口时，验收文件可以跟随上游更新并验证同一要求。
        return [path for path in targets if path in anchored_paths or _test_path(path)]
    if kind != "delivery":
        return [path for path in anchored_paths if not _test_path(path) and PurePosixPath(path).suffix.casefold() in _IMPLEMENTATION_SUFFIXES]
    return []


def coverage_report(raw: str, criteria: list[dict], files: dict[str, str], checks: list[dict], digest: str,
                    *, previous: dict | None = None) -> dict:
    known_ids = {item["id"] for item in criteria}
    by_id = _review_rows(raw, known_ids)
    if previous and previous.get("source_digest") == digest:
        seen = set(previous.get("reviewed_ids", ())) & known_ids
        prior = {row["id"]: row for row in previous["criteria"] if row["id"] in seen}
        by_id = {**prior, **by_id}
    passed = {row["id"]: row for row in checks if row["status"] == "passed"}
    browser_check = next((row for row in checks if row["id"] == "runtime.browser"), None)
    browser_ready = "runtime.browser" in passed
    results = []
    for item in criteria:
        row = by_id.get(item["id"], {})
        detail = row.get("detail")
        verdict = row.get("status")
        evidence = row.get("evidence")
        cited = row.get("checks")
        owners = row.get("files")
        anchors = [anchor for anchor in evidence[:4] if isinstance(anchor, dict)
                   and isinstance(anchor.get("path"), str) and anchor["path"] in files
                   and isinstance(anchor.get("excerpt"), str) and 2 <= len(anchor["excerpt"].strip()) <= 480
                   and anchor["excerpt"] in files[anchor["path"]]] if isinstance(evidence, list) else []
        ids = [value for value in cited if isinstance(value, str) and value in passed] if isinstance(cited, list) else []
        targets = list(dict.fromkeys(value for value in owners[:4] if isinstance(value, str) and value in files)) if isinstance(owners, list) else []
        browser = _browser_proof(ids, anchors, files) if item["kind"] != "delivery" else False
        test_results = [passed[value] for value in ids if value.startswith("runtime.tests.")]
        tested_files = {path for result in test_results if not _INCOMPLETE_SUITE.search(result.get("detail", ""))
                        for path in result.get("files", [])}
        cases = {case for result in test_results for case in result.get("test_cases", [])
                 if case.partition("::")[0] in result.get("files", [])}
        suite = any((anchor["path"].endswith(".py") or anchor["path"] in tested_files)
                    and _suite_assertion(anchor["path"], files[anchor["path"]], anchor["excerpt"], cases) for anchor in anchors)
        supported = bool(anchors and ids) and (item["kind"] == "delivery" or browser or (item["kind"] == "behavior" and suite))
        if verdict == "verified" and supported and isinstance(detail, str) and detail.strip():
            results.append({**item, "status": "verified", "detail": detail[:420], "evidence": anchors, "checks": ids})
        elif verdict in ("gap", "unverified") and isinstance(detail, str) and detail.strip():
            results.append({**item, "status": verdict, "detail": detail[:420], "evidence": anchors,
                            "files": _repair_targets(item["kind"], verdict, targets, anchors, browser_ready)})
        else:
            reason = "未取得与已执行检查一致的需求证据。"
            if item["kind"] == "browser" and browser_check and not browser_ready:
                reason = "浏览器操作场景未能执行。" + str(browser_check.get("detail", ""))[:320]
            results.append({**item, "status": "unverified", "detail": reason, "files": []})
    return {"source_digest": digest, "criteria": results, "reviewed_ids": list(by_id), "received": bool(by_id) and set(by_id) == known_ids}


def merge_coverage_report(verification: dict, review: dict, files: dict[str, str]) -> dict:
    if review.get("source_digest") != verification.get("source_digest") or source_digest(files) != review.get("source_digest"):
        return {**verification, "passed": False, "checks": [*verification["checks"],
                {"id": "coverage.snapshot", "label": "需求覆盖复核", "status": "failed", "detail": "需求证据与交付的源码快照不一致。"}]}
    schema = [{key: row[key] for key in ("id", "text", "kind")} for row in review["criteria"]]
    # 同一源码仍可能重跑验收。按本次检查结果核实已引用的证据。
    criteria = coverage_report(json.dumps(review, ensure_ascii=False), schema, files, verification["checks"], review["source_digest"])["criteria"]
    checks = [{"id": f"coverage.{row['id']}", "label": f"需求 · {row['text'][:76]}",
               "status": "passed" if row["status"] == "verified" else "failed", "detail": row["detail"]}
              for row in criteria]
    issues = [{"path": path, "criterion": row["id"], "message": f"需求「{row['text'][:180]}」未验收 · {row['detail']}"[:500]}
              for row in criteria if row["status"] != "verified" for path in row.get("files", [])]
    return {**verification, "passed": verification["passed"] and bool(criteria) and not any(row["status"] == "failed" for row in checks),
            "checks": [*verification["checks"], *checks], "issues": [*verification["issues"], *issues], "coverage_result": review}
