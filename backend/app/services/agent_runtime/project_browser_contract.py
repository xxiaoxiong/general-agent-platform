"""项目可随包交付的浏览器操作场景。由平台解释固定动作，不载入项目测试代码。"""

from __future__ import annotations

import json
import re

BROWSER_CHECKS_PATH = "tests/browser-checks.json"
_PATH = re.compile(r"/[A-Za-z0-9_./%?=&~+:#-]{0,220}\Z")
_INPUT_ACTIONS = {"fill", "select", "press"}
_LOCATOR_ACTIONS = {"click", "check", "expect_visible", "expect_hidden", "expect_text", "expect_value", "expect_invalid", *_INPUT_ACTIONS}
_OUTCOMES = {"expect_visible", "expect_hidden", "expect_text", "expect_value", "expect_invalid", "expect_response"}
_INTERACTIONS = {"click", "check", "select", "press"}
_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE"}
_ANNOTATIONS = {"description", "label", "comment", "note"}
_FINDING_STEP = re.compile(r"^tests/browser-checks\.json\s+「([^」]{1,72})」第 (\d+) 步", re.I)
_FORM_SCOPE = re.compile(r"form(?P<form>#[A-Za-z][\w-]{0,63})\s+(?P<control>(?:input|textarea|select)?#[A-Za-z][\w-]{0,63})\Z", re.ASCII)
_FORM_ACTIONS = {"fill", "select", "check"}


def browser_form_scope(step: dict) -> tuple[str, str, str] | None:
    """试探失效 ID 时保留表单约束和原来的精确字段。"""
    if step["action"] in _FORM_ACTIONS and (match := _FORM_SCOPE.fullmatch(step["selector"])):
        control = match["control"]
        return match["form"], f"form {control}", "#" + control.split("#", 1)[1]
    return None


def _string(value, limit: int, *, empty: bool = False) -> bool:
    return isinstance(value, str) and (empty or bool(value.strip())) and len(value) <= limit and "\0" not in value


def _local_path(value) -> bool:
    return isinstance(value, str) and not value.startswith("//") and bool(_PATH.fullmatch(value))


def _canonical_step(step, current_path: str):
    if not isinstance(step, dict):
        return step
    prepared = {key: value for key, value in step.items() if key not in _ANNOTATIONS}
    action = prepared.get("action")
    if not isinstance(action, str):
        return prepared
    aliases = {"target": "selector"} if action in _LOCATOR_ACTIONS else {}
    if action in {"expect_text", "expect_value"}:
        aliases.update({"text": "value", "expected": "value"})
    if action == "expect_response":
        aliases.update({"url": "path", "status_code": "status"})
    for incoming, field in aliases.items():
        if incoming in prepared and (field not in prepared or prepared[field] == prepared[incoming]):
            prepared[field] = prepared.pop(incoming)
    if action in {"expect_visible", "expect_hidden"}:
        redundant = (True, "visible") if action == "expect_visible" else (False, "hidden", "none")
        for field in ("value", "expected"):
            if prepared.get(field) in redundant:
                prepared.pop(field)
    if action == "reload" and prepared.get("path") == current_path:
        prepared.pop("path", None)
    for field in ("path", "selector", "value"):
        if field in prepared and prepared[field] in ("", None):
            used = (field == "path" and action in {"open", "expect_response"}) or (
                field == "selector" and action in _LOCATOR_ACTIONS
            ) or (field == "value" and action in _INPUT_ACTIONS | {"expect_text", "expect_value"})
            if not used:
                prepared.pop(field)
    return prepared


def _step_problem(step) -> str:
    if not isinstance(step, dict) or not isinstance(action := step.get("action"), str):
        return "需要包含 action 的对象"
    keys = {"action"}
    if action in {"open", "expect_response"}:
        keys.add("path")
        if not _local_path(step.get("path")):
            return "path 需要是应用内的绝对路径，例如 / 或 /api/tasks"
    if action in _LOCATOR_ACTIONS:
        keys.add("selector")
        if not _string(step.get("selector"), 240) or ">>" in step["selector"]:
            return "selector 需要是单个 CSS 选择器"
    if action in _INPUT_ACTIONS | {"expect_text", "expect_value"}:
        keys.add("value")
        if not _string(step.get("value"), 300, empty=action in {"fill", "expect_value"}):
            return "value 需要是有效文本"
    if action == "press" and step["value"] not in {"Enter", "Escape", "Tab", "Space"}:
        return "press 支持 Enter、Escape、Tab 和 Space"
    if action == "click" and "confirm" in step:
        keys.add("confirm")
        if type(step["confirm"]) is not bool:
            return "confirm 需要是布尔值"
    if action == "click" and "prompt" in step:
        keys.add("prompt")
        if "confirm" in step:
            return "同一个点击步骤只能使用 confirm 或 prompt"
        if not _string(step["prompt"], 300, empty=True):
            return "prompt 需要是有效文本，最多 300 字符"
    if action == "expect_response":
        keys.update({"method", "status"})
        if not isinstance(step.get("method"), str) or step["method"] not in _METHODS:
            return "method 需要是 GET、POST、PUT、PATCH 或 DELETE"
        if type(step.get("status")) is not int or not 200 <= step["status"] < 600:
            return "status 需要是 200 到 599 的 HTTP 状态码"
    if action not in _LOCATOR_ACTIONS | {"open", "reload", "expect_response"}:
        return "不支持的浏览器动作"
    if extra := set(step) - keys:
        return f" {action} 包含未知字段 {', '.join(sorted(extra))[:100]}。允许字段为 {', '.join(sorted(keys))}"
    return "步骤缺少必要字段" if set(step) != keys else ""


def parse_browser_checks(source: str) -> list[dict]:
    """保留作者的操作次序，同时限制单轮验收的体积与动作范围。"""
    try:
        document = json.loads(source.lstrip("\ufeff"))
    except (ValueError, RecursionError):
        raise ValueError("浏览器场景须返回完整的 JSON 对象") from None
    if (not isinstance(document, dict) or set(document) != {"version", "scenarios"}
            or type(document["version"]) is not int or document["version"] != 1
            or not isinstance(document["scenarios"], list) or not 1 <= len(document["scenarios"]) <= 3):
        raise ValueError("浏览器场景须包含 version 为 1 和 1 至 3 项 scenarios")
    seen = set()
    for number, scenario in enumerate(document["scenarios"], 1):
        if (not isinstance(scenario, dict) or set(scenario) != {"name", "steps"}
                or not _string(scenario["name"], 72) or (name := scenario["name"].strip()) in seen
                or not isinstance(steps := scenario["steps"], list) or not 3 <= len(steps) <= 24):
            raise ValueError(f"第 {number} 个场景须有唯一的名称和 3 至 24 行操作")
        seen.add(name)
        current_path = ""
        for line, step in enumerate(steps, 1):
            step = _canonical_step(step, current_path)
            if problem := _step_problem(step):
                raise ValueError(f"第 {number} 个场景第 {line} 步{problem}")
            if step["action"] == "open":
                current_path = step["path"]
            steps[line - 1] = step
        actions = [step["action"] for step in steps]
        if (actions[0] != "open" or actions[-1] not in _OUTCOMES or not _INTERACTIONS.intersection(actions)
                or not (_OUTCOMES - {"expect_response"}).intersection(actions)):
            raise ValueError(f"第 {number} 个场景需要从页面入口开始，执行操作并校验结果")
    return document["scenarios"]


def browser_repair_problem(previous: str, updated: str, findings: list[str]) -> str:
    """修正场景输入或定位时保留其它已验证操作。"""
    if not previous:
        return ""
    try:
        old = parse_browser_checks(previous)
        new = {scenario["name"]: scenario for scenario in parse_browser_checks(updated)}
    except ValueError:
        return ""
    failed = {scenario["name"] for scenario in old if any(f"「{scenario['name']}」" in line for line in findings)}
    if not failed:
        return ""
    for scenario in old:
        name = scenario["name"]
        if name not in new or (name not in failed and new[name] != scenario):
            return f"请保留现有场景「{name}」及与本次失败无关的操作和断言"
    return ""


def browser_entry_source(source: str, available: list[str]) -> str:
    entry_files = {"/frontend/index.html", "/client/index.html", "/web/index.html"}
    entry_files.intersection_update("/" + path for path in available)
    scenarios = parse_browser_checks(source)
    changed = False
    for scenario in scenarios:
        first = scenario["steps"][0]
        if first["path"] in entry_files:
            first["path"] = "/"
            changed = True
    return json.dumps({"version": 1, "scenarios": scenarios}, ensure_ascii=False, indent=2) + "\n" if changed else source


def browser_observed_source(source: str, findings: list[str]) -> str:
    """将实际观察到的浏览器校验与确认行为写入对应步骤。"""
    scenarios = parse_browser_checks(source)
    by_name = {scenario["name"]: scenario["steps"] for scenario in scenarios}
    changed = False
    for message in findings:
        location = _FINDING_STEP.match(message)
        if not location or (steps := by_name.get(location[1])) is None:
            continue
        index = int(location[2]) - 1
        if not 0 < index < len(steps):
            continue
        step = steps[index]
        scope_change = re.search(r"。真实表单字段已唯一定位，需更新步骤 ([0-9]{1,2}(?:,[0-9]{1,2}){0,4})$", message)
        if scope_change and (origin := browser_form_scope(step)):
            positions = [int(value) - 1 for value in scope_change[1].split(",")]
            if positions == list(range(index, index + len(positions))) and positions[-1] < len(steps):
                scopes = [browser_form_scope(steps[position]) for position in positions]
                if all(scope and scope[0] == origin[0] for scope in scopes):
                    for position, scope in zip(positions, scopes, strict=True):
                        steps[position]["selector"] = scope[1]
                    changed = True
            continue
        field = re.search(r"场景输入未满足表单要求 (#[\w-]+)$", message)
        if (field and step["action"] == "expect_visible" and steps[index - 1]["action"] == "click"
                and re.search(r"无效|校验|必填|invalid|required", location[1], re.I)
                and re.fullmatch(r"#[\w-]*(?:error|validation|invalid)[\w-]*", step.get("selector", ""), re.I)):
            steps[index] = {"action": "expect_invalid", "selector": field[1]}
            changed = True
        elif ("上一次点击的确认弹窗已取消" in message and step["action"] == "expect_hidden"
              and steps[index - 1]["action"] == "click" and not steps[index - 1].get("confirm")):
            steps[index - 1]["confirm"] = True
            changed = True
        elif "匹配到文本元素，请使用 expect_text" in message and step["action"] == "expect_value" and step["value"]:
            step["action"] = "expect_text"
            changed = True
    return json.dumps({"version": 1, "scenarios": scenarios}, ensure_ascii=False, indent=2) + "\n" if changed else source

