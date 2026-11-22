"""恢复模型返回的工程计划，保留用户选择的软件形态与技术约束。"""

from __future__ import annotations

import json
import re
from typing import Any

from .team_project_files import ProjectScope

_LEADING_THINK = re.compile(r"^\s*<think>.*?</think>\s*", re.I | re.S)
_TASK_ID = re.compile(r"[a-z][a-z0-9_-]{0,23}\Z")
_WRAPPERS = ("plan", "project_plan", "projectPlan", "project")
_PLAN_ALIASES = {
    "name": ("title", "project_name", "projectName"),
    "summary": ("description", "overview"),
    "features": ("functional_features", "functionalFeatures"),
    "api_contract": ("apiContract", "interface_contract"),
    "tasks": ("assignments", "work_items", "workItems"),
}
_TASK_ALIASES = {
    "id": ("task_id", "taskId"),
    "role": ("role_name", "roleName", "agent_role"),
    "title": ("name",),
    "brief": ("description", "instructions"),
    "depends_on": ("dependencies", "dependsOn"),
    "files": ("paths", "output_files", "outputFiles"),
}
_SCOPE_ALIASES = {
    "full-stack": "fullstack", "full_stack": "fullstack", "full stack": "fullstack", "全栈": "fullstack",
    "前后端分离": "fullstack", "front-end": "frontend", "前端": "frontend",
    "back-end": "backend", "后端": "backend",
}
_DELEGATED = re.compile(
    r"随意发挥|自由发挥|任你发挥|你(?:来|自己)决[定策]|由你决定|全权交给你|"
    r"(?:技术栈|框架|开发语言).{0,8}(?:不限|随意|灵活选择|自行选择|你来(?:选择|定)|自选|你决定)|"
    r"\b(?:up to you|your choice|use any (?:stack|framework|language)|choose (?:the|your own) (?:stack|framework))\b",
    re.I,
)
_STACK_OR_TARGET = re.compile(
    r"(?:[A-Za-z0-9+#][\w.+#-]*\s*\+\s*[A-Za-z][\w.+#-]*)|"
    r"(?<![A-Za-z0-9_])(?:vue(?:\.js)?|react(?:\.js)?|next(?:\.js)?|nuxt(?:\.js)?|angular|svelte|vite|node(?:\.js)?|"
    r"javascript|typescript|express|nestjs|koa|spring(?:\s*boot)?|java|kotlin|scala|go|golang|gin|rust|"
    r"python|flask|django|fastapi|php|laravel|ruby|rails|.net|dotnet|asp(?:\.net)?|C#|C\+\+|swift|flutter|"
    r"postgres(?:ql)?|mysql|mariadb|mongodb|redis|oracle|sqlite|supabase|firebase|graphql|grpc|spark|"
    r"hadoop|kafka|rabbitmq|ios|android|electron|tauri|unity|unreal|docker(?:[- ]compose)?|kubernetes|k8s|helm|"
    r"systemd|webassembly|html|css)(?![A-Za-z0-9_])|"
    r"微信小程序|支付宝小程序|原生移动应用|桌面客户端|游戏引擎|低代码平台|微服务架构|"
    r"(?:必须|指定|沿用|基于|采用|接入).{0,10}(?:技术栈|框架|数据库|消息队列)|"
    r"(?:现有|已有|既有|原有|上次的).{0,8}(?:技术栈|框架|依赖|部署|基础设施|数据库)|"
    r"(?:无需|无须|不(?:要|用|使用|接入)|禁用).{0,6}(?:数据库|后端|前端|持久化|HTTP)",
    re.I,
)
_DELEGATION_DENIED = re.compile(r"(?:不能|不要|不许|不得|禁止|别).{0,3}(?:随意发挥|自由发挥|自行选择|自选)")
_OPEN_BRIEF = re.compile(
    r"(?:帮我|请你|请|构建|搭建|实现|开发|创建|做|写|制作|交付|生成|一个|一套|款|"
    r"足够|很|较|更|有一定|一定|稍微|稍|复杂|综合|完整|可运行|可维护|的|"
    r"前后端分离|全栈|前端|后端|Web|网页|软件|应用|项目|系统|你|可以|随意发挥|自由发挥|任你发挥|"
    r"[\s，。！？、,.!?:：；;])", re.I,
)


def _aliases(data: dict, aliases: dict[str, tuple[str, ...]]) -> dict:
    result = dict(data)
    for field, alternate in aliases.items():
        names = [name for name in (field, *alternate) if name in data]
        if names:
            value = data[names[0]]
            if any(data[name] != value for name in names[1:]):
                raise ValueError(f"工程计划的 {field} 有相互冲突的取值")
            for name in names:
                result.pop(name)
            result[field] = value
    return result


def _is_plan(value: Any) -> bool:
    return isinstance(value, dict) and any(name in value for name in ("tasks", *_PLAN_ALIASES["tasks"]))


def _unwrap(value: Any) -> dict | None:
    if not isinstance(value, dict):
        return None
    candidates = [value[key] for key in _WRAPPERS if _is_plan(value.get(key))]
    if _is_plan(value):
        candidates.append(value)
    if len(candidates) > 1:
        raise ValueError("模型返回了多份可选工程计划")
    return candidates[0] if candidates else None


def _decode_plan(reply: str) -> dict:
    if not isinstance(reply, str):
        raise ValueError("工程计划需要文本回复")
    text = _LEADING_THINK.sub("", reply.lstrip("\ufeff"), count=1).strip()
    if len(text) > 120_000:
        raise ValueError("工程计划回复过长")
    if text.lower().startswith("<think>"):
        raise ValueError("工程计划回复未结束推理")
    decoder = json.JSONDecoder()
    try:
        value = decoder.decode(text)
    except json.JSONDecodeError:
        pass
    else:
        if (plan := _unwrap(value)) is not None:
            return plan
        raise ValueError("工程计划缺少任务列表")

    # 允许有前言和 Markdown 围栏的单份计划。已解析的对象不再搜索内部片段。
    candidates = []
    offset = 0
    for _ in range(160):
        start = text.find("{", offset)
        if start < 0:
            break
        try:
            value, offset = decoder.raw_decode(text, start)
        except json.JSONDecodeError:
            offset = start + 1
            continue
        if (plan := _unwrap(value)) is not None:
            candidates.append(plan)
    if len(candidates) != 1:
        raise ValueError("需要一份完整且唯一的 JSON 工程计划")
    return candidates[0]


def _identifier(source: str, index: int) -> str:
    key = re.sub(r"\s+", "-", source.strip().lower())
    return key if _TASK_ID.fullmatch(key) else f"task-{index}"


def _file_path(source: Any) -> Any:
    if isinstance(source, dict) and isinstance(source.get("path"), str):
        source = source["path"]
    if isinstance(source, str):
        source = source.strip().replace("\\", "/")
        while source.startswith("./"):
            source = source[2:]
    return source


def _normalize_tasks(items: list) -> list[dict]:
    tasks: list[dict] = []
    identities: dict[str, set[str]] = {}
    raw_ids: set[str] = set()
    assigned: set[str] = set()
    for index, item in enumerate(items, 1):
        if not isinstance(item, dict):
            raise ValueError("工程计划的任务必须是对象")
        task = _aliases(item, _TASK_ALIASES)
        source_id = task.get("id", "")
        if not isinstance(source_id, str):
            raise ValueError("工程任务编号必须是文本")
        alias = source_id.strip().casefold()
        if alias and alias in raw_ids:
            raise ValueError("工程任务编号重复")
        raw_ids.add(alias)
        key = _identifier(source_id, index)
        if key in assigned:
            raise ValueError("工程任务的规范化编号存在冲突")
        assigned.add(key)
        task["id"] = key
        for name in (source_id, key, task.get("role"), task.get("title")):
            if isinstance(name, str) and name.strip():
                identities.setdefault(name.strip().casefold(), set()).add(key)
        if isinstance(task.get("files"), list):
            paths = [_file_path(path) for path in task["files"]]
            if all(isinstance(path, str) for path in paths):
                task["files"] = list(dict.fromkeys(paths))
        tasks.append(task)

    for task in tasks:
        if "depends_on" not in task:
            continue
        dependencies = task["depends_on"]
        if dependencies is None or dependencies == "":
            dependencies = []
        elif isinstance(dependencies, str):
            dependencies = [dependencies]
        if not isinstance(dependencies, list) or any(not isinstance(dep, str) for dep in dependencies):
            raise ValueError("工程任务依赖必须引用已登记的任务")
        resolved = []
        for dependency in dependencies:
            matching = identities.get(dependency.strip().casefold(), set())
            if len(matching) != 1:
                raise ValueError("工程任务依赖无法唯一定位")
            resolved.append(next(iter(matching)))
        task["depends_on"] = list(dict.fromkeys(resolved))
    return tasks


def recover_project_plan(reply: str, scope: ProjectScope) -> dict:
    """兼容常见结构变体。范围、文件安全、依赖图仍由项目计划模型严格校验。"""
    plan = _aliases(_decode_plan(reply), _PLAN_ALIASES)
    response_scope = plan.get("scope", scope)
    if not isinstance(response_scope, str):
        raise ValueError("工程计划的范围必须是文本")
    response_scope = response_scope.strip().lower()
    if _SCOPE_ALIASES.get(response_scope, response_scope) != scope:
        raise ValueError("工程计划改动了请求确定的软件范围")
    plan["scope"] = scope
    if isinstance(plan.get("api_contract"), (dict, list)):
        plan["api_contract"] = json.dumps(plan["api_contract"], ensure_ascii=False, separators=(",", ":"))
    if isinstance(plan.get("tasks"), list):
        plan["tasks"] = _normalize_tasks(plan["tasks"])
    return plan


def _task(task_id: str, role: str, title: str, brief: str, files: list[str], dependencies: list[str]) -> dict:
    return {"id": task_id, "role": role, "title": title, "brief": brief,
            "depends_on": dependencies, "files": files}


def fallback_project_plan(objective: str, scope: ProjectScope) -> dict | None:
    """仅在用户授予技术取舍权且没有明确技术约束时采用可靠的通用工程分工。"""
    if not _DELEGATED.search(objective) or _DELEGATION_DENIED.search(objective) or _STACK_OR_TARGET.search(objective):
        return None

    open_brief = not _OPEN_BRIEF.sub("", objective)
    product = (
        "协作工单台"
        if open_brief else {"fullstack": "前后端分离项目", "frontend": "交互式前端项目", "backend": "后台服务项目"}[scope]
    )
    scope_context = (
        "自行选题的请求采用协作工单台。支持创建工单、按状态及关键词检索、更新处理状态和跨刷新保留记录。"
        if open_brief else "按原始请求确定项目标题、领域和功能，逐条落实用户明确提出的用途及交付要求。"
    )
    contract = ""
    tasks = []
    if scope != "frontend":
        contract = (
            "openapi.json 是共享契约。先确定业务资源和字段，登记 /api 下的实际路径、方法、请求响应模型、错误码和健康检查。"
            "以 127.0.0.1:8765 为后端本地默认地址，按契约接入；前端使用 8766 端口。"
        )
        tasks.append(_task(
            "contract", "接口架构", "定义共享 API 契约",
            scope_context + "用 OpenAPI 3 描述真实用例和产品，让前后续成员据此使用同一字段、接口和状态含义。" + contract,
            ["openapi.json"], [],
        ))
        tasks.append(_task(
            "backend", "后端工程", "实现业务与本地持久化",
            "使用 Python 3.11 标准库的 HTTP 服务和 SQLite，无需下载依赖。store.py 管理参数化查询和事务，"
            "domain.py 实现业务校验和状态规则，app.py 实现真实路由、分页筛选、可理解的错误和本地 CORS。"
            "按已提交的 OpenAPI 逐个实现接口，并让 `python backend/app.py` 从项目根目录启动服务。",
            ["backend/store.py", "backend/domain.py", "backend/app.py"], ["contract"],
        ))
    if scope != "backend":
        tasks.append(_task(
            "frontend", "前端工程", "实现完整界面与可操作流程",
            scope_context + "使用原生 HTML、CSS 和 JavaScript ES 模块。index.html 提供可访问的结构与表单，"
            "state.mjs 管理筛选和数据状态，app.js 完成增改查询、确认、加载、空态及错误反馈，style.css 适配桌面和窄屏。"
            + ("严格接入共享 API 契约，默认调用 127.0.0.1:8765。" if scope == "fullstack" else
               "以 localStorage 持久化本地记录，纯逻辑可通过 Node 单独测试。"),
            ["frontend/index.html", "frontend/state.mjs", "frontend/app.js", "frontend/style.css"],
            ["contract"] if scope == "fullstack" else [],
        ))
    test_file = "tests/state.test.mjs" if scope == "frontend" else "tests/test_app.py"
    commands = []
    if scope != "frontend":
        commands.append("python backend/app.py")
    if scope != "backend":
        commands.append("python -m http.server 8766 --bind 127.0.0.1 --directory frontend")
    commands.append("node --test tests/state.test.mjs" if scope == "frontend" else "python -m unittest discover -s tests")
    tasks.append(_task(
        "quality", "集成验收", "交付启动说明与回归测试",
        "根据前序实际代码编写带断言的测试，覆盖正常主流程、异常输入和状态保存；README 列出必要端口、数据目录、"
        "使用步骤和测试前置条件。从项目根目录分别执行：" + "、".join(f"`{command}`" for command in commands) + "。",
        [test_file, "README.md"], [task["id"] for task in tasks],
    ))
    return {
        "name": product,
        "scope": scope,
        "summary": scope_context + "交付可启动的完整源码、带断言的测试和启动说明。",
        "features": ["完成用户指定业务的创建、查询和处理主流程", "覆盖加载、空数据和错误反馈", "为关键业务与异常输入提供可执行测试"],
        "api_contract": contract,
        "tasks": tasks,
    }
