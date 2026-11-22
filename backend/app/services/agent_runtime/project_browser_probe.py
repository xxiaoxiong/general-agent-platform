"""在单独的低权限进程中解释浏览器验收场景。"""

from __future__ import annotations

import json
import re
import sys
from collections import deque
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import Error, expect, sync_playwright

_WAIT_MS = 2500
_CRITICAL_RESOURCES = {"document", "script", "stylesheet"}
_UI_OUTCOMES = {"expect_visible", "expect_hidden", "expect_text", "expect_value", "expect_invalid"}


class PageCheck:
    def __init__(self, context, origin: str, allowed: set[str]):
        self.page = context.new_page()
        self.page.set_default_timeout(_WAIT_MS)
        self.origin = origin
        self.allowed = allowed
        self.errors: list[dict] = []
        self.responses: deque[tuple[str, str, int]] = deque(maxlen=40)
        self.server_failure: str = ""
        self.form_errors: list[dict] = []
        self.confirm_dismissed = False
        self.page.add_init_script(
            """(() => {
                window.__projectCheckInvalid = [];
                document.addEventListener('invalid', ({target: input}) => {
                    if (window.__projectCheckInvalid.length === 5) return;
                    window.__projectCheckInvalid.push({
                        name: (input.id ? '#' + input.id : (input.name || input.tagName)).slice(0, 60),
                        visible: input.getClientRects().length > 0 && getComputedStyle(input).visibility !== 'hidden'
                    });
                }, true);
            })();""",
        )
        context.route("**/*", self.route)
        self.page.on("pageerror", self.page_error)
        self.page.on("response", self.response)
        self.page.on("requestfailed", self.request_failed)

    def route(self, route) -> None:
        location = urlsplit(route.request.url)
        if f"{location.scheme}://{location.netloc}" in self.allowed:
            route.continue_()
        else:
            route.abort("blockedbyclient")

    def fault(self, message: str, path: str = "", kind: str = "page") -> None:
        if len(self.errors) < 5:
            self.errors.append({"message": message[:280], "path": path[:200], "kind": kind})

    def page_error(self, error: Error) -> None:
        stack = getattr(error, "stack", "") or ""
        location = re.search(r"https?://(?:127\.0\.0\.1|localhost):\d+([^\s()]+?):\d+:\d+", stack)
        self.fault(str(error).split("\n", 1)[0], location[1] if location else "", "script")

    def response(self, response) -> None:
        path = urlsplit(response.url).path
        resource = response.request.resource_type
        # 重定向随后还会有最终响应，实际 HTTP 状态保留给场景断言。
        self.responses.append((response.request.method, path, response.status))
        if response.status >= 400 and resource in _CRITICAL_RESOURCES:
            self.fault(f"页面资源 {path} 返回 HTTP {response.status}", path, resource)
        elif response.status >= 500 and resource in {"xhr", "fetch"}:
            self.server_failure = f"{response.request.method} {path} 返回 HTTP {response.status}"

    def request_failed(self, request) -> None:
        location = urlsplit(request.url)
        if request.resource_type not in _CRITICAL_RESOURCES or request.failure == "net::ERR_ABORTED":
            return
        if f"{location.scheme}://{location.netloc}" not in self.allowed:
            self.fault("页面依赖离线环境无法访问的外部资源", location.path, "offline_dependency")
        else:
            self.fault(f"页面资源请求失败 {request.failure}", location.path, request.resource_type)

    def open(self, path: str) -> None:
        self.form_errors = []
        self.server_failure = ""
        self.confirm_dismissed = False
        response = self.page.goto(self.origin + path, wait_until="load")
        if response is None or not response.ok:
            raise AssertionError(f"页面打开后未返回成功响应 {path}")

    def smoke(self) -> dict:
        try:
            self.open("/")
            self.page.wait_for_function(
                """() => document.body && (document.body.innerText.trim().length > 0 ||
                   [...document.querySelectorAll('canvas, svg, video, input, button')].some(node => {
                     const bounds = node.getBoundingClientRect();
                     return bounds.width > 0 && bounds.height > 0 && getComputedStyle(node).visibility !== 'hidden';
                   }))""",
                timeout=_WAIT_MS,
            )
        except (Error, AssertionError):
            fallback = "浏览器未能打开有可见内容的首页"
            return self.errors[0] if self.errors else {"message": fallback, "path": "/", "kind": "page"}
        return self.errors[0] if self.errors else {}

    def locator(self, step: dict):
        return self.page.locator(f"css={step['selector']}")

    def visible_outcome(self, step: dict) -> bool:
        """记录首屏状态，要求至少一条用户可见断言能观察到操作效果。"""
        control = self.locator(step)
        action = step["action"]
        try:
            visible = control.filter(visible=True)
            if action == "expect_hidden":
                return visible.count() == 0
            if action == "expect_visible":
                return visible.count() > 0
            if action == "expect_invalid" or visible.count() != 1 or control.count() != 1:
                return False
            if action == "expect_text":
                return step["value"] in control.inner_text(timeout=400)
            if action == "expect_value":
                return step["value"] == control.input_value(timeout=400)
            return True
        except Error:
            return False

    def act(self, step: dict) -> None:
        action = step["action"]
        if action == "open":
            self.open(step["path"])
        elif action == "reload":
            self.form_errors = []
            self.page.reload(wait_until="load")
        elif action == "expect_response":
            wanted = (step["method"], urlsplit(step["path"]).path, step["status"])
            if wanted not in self.responses:
                self.page.wait_for_event("response", predicate=lambda row: (
                    row.request.method, urlsplit(row.url).path, row.status,
                ) == wanted, timeout=_WAIT_MS)
        else:
            control = self.locator(step)
            if action == "click":
                self.server_failure = ""
                self.confirm_dismissed = False
                self.page.evaluate("() => { window.__projectCheckInvalid = []; }")

                def handle_dialog(dialog):
                    if step.get("confirm"):
                        dialog.accept()
                    else:
                        self.confirm_dismissed = dialog.type == "confirm"
                        dialog.dismiss()

                self.page.on("dialog", handle_dialog)
                try:
                    control.click()
                finally:
                    self.page.remove_listener("dialog", handle_dialog)
                validation = self.page.evaluate("() => window.__projectCheckInvalid || []")
                self.form_errors = [field for field in validation[:5] if isinstance(field, dict)
                                    and isinstance(field.get("name"), str) and type(field.get("visible")) is bool
                                    ] if isinstance(validation, list) else []
            elif action == "fill":
                control.fill(step["value"])
            elif action == "select":
                control.select_option(value=step["value"])
            elif action == "check":
                control.check()
            elif action == "press":
                control.press(step["value"])
            elif action == "expect_hidden":
                expect(control.filter(visible=True)).to_have_count(0, timeout=_WAIT_MS)
            elif action == "expect_visible":
                expect(control.filter(visible=True).first).to_be_visible(timeout=_WAIT_MS)
            elif action == "expect_invalid":
                label = control.evaluate("input => input.id ? '#' + input.id : (input.name || input.tagName)", timeout=400)
                if not any(row["name"] == label and row["visible"] for row in self.form_errors):
                    raise AssertionError("表单提交时未观察到预期的浏览器输入校验")
            else:
                expect(control).to_be_visible(timeout=_WAIT_MS)
                if action == "expect_text":
                    expect(control).to_contain_text(step["value"], timeout=_WAIT_MS)
                elif action == "expect_value":
                    expect(control).to_have_value(step["value"], timeout=_WAIT_MS)

    def describe_control(self, step: dict) -> tuple[str, str]:
        if "selector" not in step:
            return "", "action"
        control = self.locator(step)
        try:
            count = control.count()
            if count == 0:
                return "。当前页面缺少该元素", "missing_control"
            if count != 1:
                return f"。选择器匹配到 {count} 个元素，需要唯一定位", "scenario_input"
            if not control.is_visible():
                hidden = control.evaluate(
                    """node => {
                        for (let current = node; current; current = current.parentElement) {
                            if (getComputedStyle(current).display === 'none') return current.id ? '#' + current.id : current.tagName;
                        }
                        return '';
                    }""", timeout=400,
                )
                return f"。元素不可见{f'，隐藏位置 {hidden}' if hidden else ''}", "action"
            if step["action"] == "fill" and control.evaluate("node => node.tagName === 'SELECT'", timeout=400):
                return "。下拉选项需要使用 select", "scenario_input"
            if step["action"] == "expect_value" and not control.evaluate(
                "node => node.matches('input, select, textarea')", timeout=400,
            ):
                return "。匹配到文本元素，请使用 expect_text", "scenario_input"
            if step["action"] in {"expect_text", "expect_value"}:
                return f"。预期 {step['value'][:80]}", "action"
        except Error:
            pass
        return "", "action"

    def scenario(self, steps: list[dict]) -> dict:
        before = {}
        for index, step in enumerate(steps, 1):
            try:
                self.act(step)
                if self.errors:
                    return {**self.errors[0], "step": index}
                if index == 1:
                    before = {number: self.visible_outcome(candidate) for number, candidate in enumerate(steps, 1)
                              if candidate["action"] in _UI_OUTCOMES}
            except (Error, AssertionError):
                target = step.get("selector") or step.get("path") or ""
                description, kind = self.describe_control(step)
                detail = {"message": f"操作或断言未满足 {step['action']} {target}".strip() + description,
                          "kind": kind, "selector": step.get("selector"), "path": urlsplit(self.page.url).path, "step": index}
                fields = self.form_errors
                if fields and step["action"].startswith("expect_"):
                    hidden = any(not field["visible"] for field in fields)
                    failed = "、".join(str(field["name"])[:60] for field in fields)
                    reason = "不可见字段仍参与表单校验" if hidden else "场景输入未满足表单要求"
                    detail.update(kind="action" if hidden else "scenario_input", message=f"{detail['message']}。{reason} {failed}")
                elif self.confirm_dismissed and step["action"] == "expect_hidden":
                    detail.update(kind="scenario_input", message=f"{detail['message']}。上一次点击的确认弹窗已取消")
                elif self.server_failure:
                    detail.update(kind="api", message=f"{detail['message']} · {self.server_failure}")
                return {**detail, **(self.errors[0] if self.errors else {})}
        if all(before.values()):
            return {"kind": "coverage", "step": len(steps), "path": "",
                    "message": "结果断言在执行场景前已成立，请加入能验证操作实际产生变化的页面断言"}
        return {"steps": len(steps)}


def run(configuration: dict) -> dict:
    origin = configuration["origin"]
    allowed = set(configuration["allowed"])
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, args=["--disable-dev-shm-usage", "--renderer-process-limit=2"], timeout=9_000)

        def new_check():
            context = browser.new_context(viewport={"width": 1280, "height": 800}, service_workers="block", accept_downloads=False)
            return context, PageCheck(context, origin, allowed)

        try:
            context, page = new_check()
            try:
                smoke = page.smoke()
            finally:
                context.close()
            results = []
            for scenario in configuration["scenarios"] if not smoke else []:
                context, page = new_check()
                try:
                    results.append(page.scenario(scenario["steps"]))
                finally:
                    context.close()
            return {"smoke": smoke, "scenarios": results}
        finally:
            browser.close()


if __name__ == "__main__":
    try:
        result = run(json.loads(Path(sys.argv[1]).read_text(encoding="utf-8")))
    except (OSError, Error):
        result = {"environment": "隔离浏览器未能启动或保持连接"}
    sys.stdout.write(json.dumps(result, ensure_ascii=True))
