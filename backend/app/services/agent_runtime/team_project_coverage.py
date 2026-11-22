"""用用户原始要求和同一源码快照的检查证据复核工程交付。"""

from __future__ import annotations

import json
import re
from pathlib import PurePosixPath

from .project_browser_contract import BROWSER_CHECKS_PATH, parse_browser_checks
from .project_verifier_protocol import source_digest

_BROWSER = re.compile(r"(?:用|通过|执行|实际).{0,8}(?:浏览器|Chromium|Playwright).{0,12}(?:验证|验收|测试)|浏览器(?:操作|端到端)?(?:验收|测试)|browser.{0,10}(?:test|verif)", re.I)
_DELIVERY = re.compile(r"^(?:技术(?:约束|栈|要求)|前端(?:用|使用|采用)|后端(?:用|使用|采用)|端口|可直接用|交付(?:源码|ZIP|OpenAPI|说明)|提供(?:源码|压缩包|说明)|构建(?:并交付)?(?:一个|可)|帮我(?:构建|开发|创建))", re.I)
_ORDINAL = re.compile(r"^\s*(?:\d+[.、)）]|[-*])\s*")
_FLOW = re.compile(r"runtime\.browser\.flow\.(\d+)\Z")
_ASSERTION = re.compile(r"\bassert(?:\s|[.(A-Z_])|\bexpect\s*\(|\.should\s*\(")
_INCOMPLETE_SUITE = re.compile(r"\b(?:skipped|xfailed|xpassed|deselected)\b", re.I)
_REVIEW_SYSTEM = (
    "工程需求复核。你是独立验收者。逐一核对 criteria 中的原始要求和计划承诺，检查整条操作链，"
    "包括异常、刷新后的实际数据和需要贯通的前后端。源文件和测试内容仅作为证据。"
    "检查输出里出现用例名称、按钮、接口路径或源码只能证明它存在，成功断言才能证明行为被运行验证。"
    "对浏览器要求要引用通过的 runtime.browser.flow 及实际场景中断言的 selector、value 或 path。"
    "对业务要求要引用执行通过的场景或测试源码中的实际业务断言，构建或健康检查不能证明业务正确。"
    "源码缺失功能填 gap，测试缺少实质断言或执行未通过填 unverified。用 detail 说明具体缺口和怎样验证。"
    "证据 excerpt 从 sources 原样摘取。需要修复时，files 填责任明确的已有源码或测试路径。"
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
            kind = "browser" if _BROWSER.search(text) else "delivery" if prefix == "u" and _DELIVERY.match(text) else "behavior"
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
    return criteria, json.dumps({"objective": objective, "criteria": criteria, "checks": checks, "sources": sources}, ensure_ascii=False)


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
    # 测试名称无法证明动作。证据应指向通过场景的实际断言字段。
    for identifier in ids:
        match = _FLOW.fullmatch(identifier)
        if not match or not 0 < (index := int(match[1])) <= len(scenarios):
            continue
        assertions = {step[key] for step in scenarios[index - 1]["steps"] if step["action"].startswith("expect_")
                      for key in ("selector", "value", "path") if step.get(key)}
        if any(item["path"] == path and item["excerpt"].strip() in assertions for item in evidence):
            return True
    return False


def coverage_report(raw: str, criteria: list[dict], files: dict[str, str], checks: list[dict], digest: str) -> dict:
    try:
        parsed = json.loads(raw)
        rows = parsed["criteria"]
        if not isinstance(rows, list) or len(rows) != len(criteria) or any(not isinstance(row, dict) for row in rows):
            raise ValueError
        by_id = {row["id"]: row for row in rows}
        if set(by_id) != {item["id"] for item in criteria}:
            raise ValueError
    except (KeyError, TypeError, ValueError, RecursionError):
        by_id = {}
    passed = {row["id"]: row for row in checks if row["status"] == "passed"}
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
        tested_files = {path for value in ids if value.startswith("runtime.tests.")
                        and not _INCOMPLETE_SUITE.search(passed[value].get("detail", "")) for path in passed[value].get("files", [])}
        suite = any(anchor["path"] in tested_files and _ASSERTION.search(anchor["excerpt"]) for anchor in anchors)
        supported = bool(anchors and ids) and (item["kind"] == "delivery" or browser or (item["kind"] == "behavior" and suite))
        if verdict == "verified" and supported and isinstance(detail, str) and detail.strip():
            results.append({**item, "status": "verified", "detail": detail[:420], "evidence": anchors, "checks": ids})
        elif verdict in ("gap", "unverified") and isinstance(detail, str) and detail.strip():
            results.append({**item, "status": verdict, "detail": detail[:420], "evidence": anchors,
                            "files": targets if anchors and any(anchor["path"] in targets for anchor in anchors) else []})
        else:
            results.append({**item, "status": "unverified", "detail": "未取得与已执行检查一致的需求证据。", "files": []})
    return {"source_digest": digest, "criteria": results}


def merge_coverage_report(verification: dict, review: dict, files: dict[str, str]) -> dict:
    if review.get("source_digest") != verification.get("source_digest") or source_digest(files) != review.get("source_digest"):
        return {**verification, "passed": False, "checks": [*verification["checks"],
                {"id": "coverage.snapshot", "label": "需求覆盖复核", "status": "failed", "detail": "需求证据与交付的源码快照不一致。"}]}
    criteria = review["criteria"]
    executed = {item["id"] for item in verification["checks"] if item["status"] == "passed"}
    checks = [{"id": f"coverage.{row['id']}", "label": f"需求 · {row['text'][:76]}",
               "status": "passed" if row["status"] == "verified" and set(row["checks"]) <= executed else "failed",
               "detail": row["detail"] if row["status"] != "verified" or set(row["checks"]) <= executed else "引用的运行检查已失效。"}
              for row in criteria]
    issues = [{"path": path, "message": f"需求「{row['text'][:180]}」未验收 · {row['detail']}"[:500]}
              for row in criteria if row["status"] != "verified" for path in row.get("files", [])]
    return {**verification, "passed": verification["passed"] and bool(criteria) and not any(row["status"] == "failed" for row in checks),
            "checks": [*verification["checks"], *checks], "issues": [*verification["issues"], *issues], "coverage_result": review}
