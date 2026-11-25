"""Task Contract（P4-01）：结构化任务目标与完成条件。

任务书 §9：准确度不能只靠换模型或加 System Prompt——复杂任务必须建立
可验证的完成条件（用户目标 → 结构化契约 → 执行 → 收集证据 → Verification）。

设计约束：
- 确定性推导（关键词 + 能力包），不调用模型（契约生成零成本、可测试）；
- 按任务类型声明完成检查项（§9 验证方式表），简单问答不加重验证负担
  （"不要每个简单问题都额外调用一次模型"）；
- 契约只描述"查什么"，不描述"怎么执行"——执行仍由 DeepAgents 规划。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Literal, Optional, Sequence

from .table_facts import TableFacts, derive_table_facts

# 任务类型（§9 验证方式表）
TASK_QA = "qa"                    # 普通知识问答
TASK_RESEARCH = "research"        # 网络研究
TASK_DOC = "doc"                  # 文档生成
TASK_DATA = "data"                # Excel / 数据分析
TASK_DB = "db"                    # 数据库分析
TASK_CODE = "code"                # 代码开发
TASK_BUSINESS = "business"        # 业务操作
TASK_OTHER = "other"

ProjectScope = Literal["fullstack", "frontend", "backend"]

# 能力包 → 任务类型（能力包是更强的信号，优先于关键词）
_CAPABILITY_TASK_TYPES = {
    "web_research": TASK_RESEARCH,
    "doc_processing": TASK_DOC,
    "data_analysis": TASK_DATA,
    "knowledge": TASK_QA,
    "coding": TASK_CODE,
    "business_automation": TASK_BUSINESS,
}

# 关键词 → 任务类型（无能力包信号时的兜底）
_KEYWORD_TASK_TYPES = [
    (r"研究|调研|竞品|行业|市场|新闻|核实|来源|文献", TASK_RESEARCH),
    (r"报告|文档|PPT|Word|PDF|方案|白皮书|写作|撰写|生成.*文件", TASK_DOC),
    (r"Excel|表格|统计|指标|图表|数据分析|汇总", TASK_DATA),
    (r"SQL|数据库|数据表|查询数据", TASK_DB),
    (r"代码|编程|脚本|重构|函数|接口实现|测试用例|bug|报错", TASK_CODE),
    (r"邮件|webhook|定时|自动化|审批|下单|外部系统|工单", TASK_BUSINESS),
]

_ADVICE = re.compile(r"^\s*(?:请(?:问|解释|介绍)?\s*)?(?:如何|怎样|怎么|什么是|解释|介绍|示例|how\b|what\b|explain\b)", re.I)
_TEXT_ONLY = re.compile(r"不用(?:真的)?(?:发送|执行|创建文件)|不(?:要|需要)(?:写入文件|生成(?:实际)?文件)|只(?:要|需|提供)\s*(?:思路|建议|方案|示例|代码片段)|\b(?:no files|outline only)\b", re.I)
_DELIVER = re.compile(r"保存(?:到|为)|导出|输出|交付|生成|创建|写入|写成|写到|命名为|export|save (?:as|to)|deliver|create|write (?:to|a)", re.I)
_FILE_INTENT = re.compile(r"可下载|落盘|文件|文档|打包|\b(?:download|files?|archive)\b", re.I)
_FILE_NAME = re.compile(r"(?<![\w.@/-])(?:/workspace/)?((?:[\w-][\w.-]*/)*[\w-][\w.()-]*\.(?:md|txt|json|csv|tsv|xlsx|docx|pdf|pptx|zip|html|css|[mc]?jsx?|tsx?|vue|svelte|py|sh|toml|yaml|yml|sql))(?![\w./-])", re.I)
_FORMATS = {"markdown": "md", "excel": "xlsx", "word": "docx", "powerpoint": "pptx"}
_FORMAT = re.compile(r"\b(markdown|excel|word|powerpoint|md|txt|json|csv|tsv|xlsx|docx|pdf|pptx|zip)\b", re.I)
_RESEARCH = re.compile(r"调研|研究.{0,25}(?:趋势|市场|行业|竞品|进展|现状|证据)|(?:交叉)?(?:核实|核验|核对|查证).{0,20}(?:资料|来源|事实)|\b(?:research|fact[- ]check)\b", re.I)
_NO_WEB = re.compile(r"不要联网|无需联网|不用联网|禁止联网|不能联网|不要搜索|无需搜索|不用搜索|离线", re.I)
# 用户上传附件时，附件注记里的工作区路径（用于判断本轮已有本地资料可引用）
_ATTACHMENT_NOTE = re.compile(r"- /workspace/uploads/([^\n（]+)")
# 明确要求外部/在线检索：有本地资料时，只有出现这些诉求才额外要求网络检索证据
_WEB_INTENT = re.compile(
    r"联网|上网|在线|网上|外网|搜索|检索|最新|实时|公开资料|外部来源|外部资料|外部信息"
    r"|\bonline\b|\bweb\b|\bsearch\b|\binternet\b", re.I,
)
_DATABASE = re.compile(r"(?:查询|核对|统计|分析).{0,24}(?:数据库|数据表)|从.{0,12}(?:数据库|数据表).{0,18}(?:查|取|读)|\bquery (?:the |a )?database\b", re.I)
_EXTERNAL_ACTION = re.compile(r"(?:发送|发出|代发).{0,20}(?:邮件|通知|webhook)|\b(?:send (?:an? |the )?e-?mail|invoke (?:a |the )?webhook)\b", re.I)
_DEPTH = re.compile(r"深入|深度|全面|端到端|交叉验证|逐项|分阶段|多阶段|\b(?:in.depth|end.to.end|comprehensive|cross.check)\b", re.I)
_NUMBERED_STEP = re.compile(r"(?m)^\s*(?:\d{1,2}[.、)）]|[一二三四五六七八九十]+、)\s*\S")
_MINIMUM_SOURCES = re.compile(r"至少\s*([1-9]|[一二两三四五六七八九十])\s*(?:个|条|份|篇|项)?\s*(?:独立|可核验|可靠|权威)?\s*(?:来源|信源|文献)|\bat least\s+([1-9])\s+(?:independent\s+)?(?:sources|references)\b", re.I)
_CHAR_COUNT = re.compile(r"(?:至少|不少于|不低于)\s*(\d{2,5})\s*(?:个汉字|字)(?![段节])")
_STRICT_JSON = re.compile(r"(?:仅|只)(?:能)?(?:返回|输出).{0,8}\bJSON\b|\b(?:return|output) only (?:valid )?JSON\b", re.I)
_PROJECT_IMPLEMENT = re.compile(
    r"构建(?:并交付)?|搭建|开发|实现|创建|生成|制作|修复|修改|重构|做(?:出|一套|一个|个)|写(?:一套|一个|个)"
    r"|\b(?:build|create|implement|develop|make|scaffold|fix|refactor)\b", re.I,
)
_PROJECT_OBJECT = re.compile(r"项目|系统|应用|网站|网页|端应用|工作台|仪表盘|服务|平台|\b(?:project|app(?:lication)?|website|dashboard|service)\b", re.I)
_PROJECT_DESCRIPTION = re.compile(
    r"(?:项目|系统|应用|网站|网页|服务|平台)(?:的)?(?:用户|操作|维护|技术)?(?:设计方案|开发方案|方案|报告|说明书|文档|手册|介绍|复盘)"
    r"|(?:一份|撰写|编写).{0,12}(?:方案|报告|说明书|文档|手册)"
    r"|\b(?:proposal|report|manual|documentation|design document|architecture plan)\b", re.I,
)
_PROJECT_ADVICE = re.compile(r"只(?:要|需|给|输出|提供)\s*(?:方案|思路|建议|示例|代码片段)|不(?:需要|用|要)\s*(?:实际)?(?:实现项目|写代码|开发项目|写入文件|生成(?:实际)?文件)|\b(?:plan only|design only|no code|do not implement)\b", re.I)
_PROJECT_FULLSTACK = re.compile(r"前[后後]端|全栈|\bfull[\s-]?stack\b", re.I)
_PROJECT_FRONTEND = re.compile(r"前端|网站|网页|页面|仪表盘|(?<![A-Za-z0-9_])(?:front[\s-]?end|vue|vite|website|dashboard)(?![A-Za-z0-9_])", re.I)
_PROJECT_BACKEND = re.compile(r"后端|服务端|接口服务|(?<![A-Za-z0-9_])(?:back[\s-]?end|fastapi|uvicorn|rest api|api\s*(?:server|服务))(?![A-Za-z0-9_])", re.I)
_PROJECT_STACK_CONSTRAINT = re.compile(r"(?:前端|后端|服务端)(?:必须|需要)?(?:用|使用|采用|基于|技术栈)", re.I)
_PROJECT_REQUESTS = {
    "runtime_build": re.compile(r"(?:生产|真实|实际)构建|(?:构建|编译)(?:应|需|要)?(?:成功|通过)|\bnpm run build\b|\bproduction build\b", re.I),
    "runtime_tests": re.compile(r"可执行测试|测试(?:用例|套件|覆盖|通过)|(?:单元|集成|回归|端到端|自动化)(?:功能)?测试(?!说明|计划)|(?:运行|执行|编写|提供|交付|附上)测试(?!说明|计划)|\b(?:pytest|runnable tests?|test suite|run (?:the )?tests?)\b", re.I),
    "runtime_browser": re.compile(r"(?:用|通过|在|使用).{0,10}(?:浏览器|Chromium|Playwright).{0,10}(?:验证|验收|测试)|浏览器(?:端到端|操作|功能)?(?:测试|验收|验证)|\b(?:browser|playwright) (?:tests?|validation|verification)\b", re.I),
    "openapi": re.compile(r"(?<![A-Za-z0-9_])(?:openapi|swagger)(?![A-Za-z0-9_])", re.I),
    "archive": re.compile(r"(?<![A-Za-z0-9_])(?:ZIP|archive)(?![A-Za-z0-9_.])|(?:源码|代码|工程|项目)打包|压缩包", re.I),
}
_PROJECT_NEGATIVE = re.compile(r"(?:无需|不需要|不要求|不必|不用|不要|暂不|只需说明|仅说明|skip|without|not required|do not).{0,8}$|不$", re.I)


def _software_project_scope(request: str) -> ProjectScope | None:
    if _PROJECT_ADVICE.search(request):
        return None
    explicit_project = False
    for clause in re.split(r"[。；;\n]", request):
        action = _PROJECT_IMPLEMENT.search(clause)
        if not action:
            continue
        target = re.split(r"[，,]|(?:并且?|同时)(?:提供|编写|写|交付)", clause[action.end():], maxsplit=1)[0]
        if _PROJECT_DESCRIPTION.search(target) or not _PROJECT_OBJECT.search(target):
            continue
        explicit_project = True
        # 技术栈经常出现在目标之前。范围只从实施语句推导，避免把参考接口误当交付范围。
        intent = clause[:action.start()] + " " + target
        frontend, backend = _PROJECT_FRONTEND.search(intent), _PROJECT_BACKEND.search(intent)
        if _PROJECT_FULLSTACK.search(intent) or (frontend and backend):
            return "fullstack"
        if frontend:
            return "frontend"
        if backend:
            return "backend"
    if explicit_project:
        stack = " ".join(_PROJECT_STACK_CONSTRAINT.findall(request))
        frontend, backend = _PROJECT_FRONTEND.search(stack), _PROJECT_BACKEND.search(stack)
        if frontend and backend:
            return "fullstack"
        if frontend:
            return "frontend"
        if backend:
            return "backend"
    return None


def _project_requirement(request: str, pattern: re.Pattern[str]) -> bool:
    return any(not _PROJECT_NEGATIVE.search(clause[:match.start()])
               for clause in re.split(r"[。；;，,、\n]", request) for match in pattern.finditer(clause))

@dataclass
class TaskContract:
    """一次 Run 的结构化任务契约。"""

    goal: str                                  # 用户目标（原始输入摘要）
    task_type: str = TASK_OTHER                # 任务类型
    completion_checks: List[str] = field(default_factory=list)  # 完成检查项 ID
    forbidden: List[str] = field(default_factory=list)          # 禁止操作（预留）
    source: str = "keyword"                    # 推导来源：capability / keyword / default
    artifact_paths: tuple[str, ...] = ()
    artifact_formats: tuple[str, ...] = ()
    source_count: int = 0
    supplied_urls: tuple[str, ...] = ()
    minimum_chars: int = 0
    deep: bool = False
    table_facts: TableFacts | None = None
    expected_json: dict[str, Any] = field(default_factory=dict)
    project_scope: ProjectScope | None = None
    project_checks: tuple[str, ...] = ()
    project_archive_path: str | None = None
    project_openapi_path: str | None = None

    @property
    def is_complex(self) -> bool:
        return self.deep or len(self.completion_checks) > 1

    def to_dict(self) -> Dict[str, Any]:
        return {
            "goal": self.goal,
            "task_type": self.task_type,
            "completion_checks": list(self.completion_checks),
            "forbidden": list(self.forbidden),
            "source": self.source,
            "artifact_paths": list(self.artifact_paths),
            "artifact_formats": list(self.artifact_formats),
            "source_count": self.source_count,
            "minimum_chars": self.minimum_chars,
            **({"project_scope": self.project_scope, "project_checks": list(self.project_checks),
                "project_archive_path": self.project_archive_path,
                "project_openapi_path": self.project_openapi_path} if self.project_scope else {}),
        }


def infer_task_type(
    user_input: str,
    capability_ids: Optional[Sequence[str]] = None,
    *,
    tool_names: Optional[Sequence[str]] = None,
) -> tuple:
    """推断任务类型（capability > 已用工具 > 关键词 > other）。返回 (type, source)。"""
    # 1) 能力包信号（排除 general/knowledge 这类无强类型指向的包）
    for cid in (capability_ids or []):
        t = _CAPABILITY_TASK_TYPES.get(cid)
        if t and t != TASK_QA:
            return t, "capability"

    # 2) 已用工具信号（执行事实比关键词更可靠）
    tools = set(tool_names or [])
    if tools & {"db_query", "db_count", "db_aggregate", "db_schema"}:
        return TASK_DB, "tools"
    if tools & {"python_exec"}:
        return TASK_CODE, "tools"
    if tools & {"web_search", "fetch_webpage", "http_get"}:
        return TASK_RESEARCH, "tools"
    if tools & {"http_post", "send_email_tool", "webhook_notify"}:
        return TASK_BUSINESS, "tools"

    # 3) 关键词
    for pattern, t in _KEYWORD_TASK_TYPES:
        if re.search(pattern, user_input or ""):
            return t, "keyword"

    return TASK_OTHER, "default"


def build_task_contract(
    user_input: str,
    *,
    capability_ids: Optional[Sequence[str]] = None,
    tool_names: Optional[Sequence[str]] = None,
    goal_limit: int = 200,
    web_searchable: bool = True,
) -> TaskContract:
    """用户目标 → 结构化任务契约（确定性，不调用模型）。"""
    request = (user_input or "").split("\n\n[附件]", 1)[0].split("\n\n[参考文档]", 1)[0].strip()
    local_sources = tuple(_ATTACHMENT_NOTE.findall(user_input or ""))
    goal = request[:goal_limit]
    # 能力包负责提供工具。验收义务由本次用户要求确定，避免给咨询强加写入、查询或发送动作。
    task_type, source = infer_task_type(request, capability_ids, tool_names=tool_names)
    advisory = bool(_ADVICE.match(request))
    text_only = bool(_TEXT_ONLY.search(request))
    project_scope = _software_project_scope(request) if not advisory else None
    project_checks: tuple[str, ...] = ()
    project_archive_path = project_openapi_path = None
    checks = ["answer_present"]
    paths: dict[str, None] = {}
    formats: dict[str, None] = {}
    for clause in re.split(r"[。；;\n]", request) if not (advisory or text_only) else ():
        verb = _DELIVER.search(clause)
        if not verb:
            continue
        output = clause[verb.end():]
        named = _FILE_NAME.findall(output)
        for path in named:
            paths[path.removeprefix("workspace/")] = None
        if _FILE_INTENT.search(output):
            checks.append("artifact_produced")
            if not named:
                for suffix in _FORMAT.findall(output):
                    suffix = suffix.casefold()
                    formats[_FORMATS.get(suffix, suffix)] = None
    if project_scope:
        task_type, source = TASK_CODE, "project"
        checks.append("artifact_produced")
        project_checks = ("runtime_start", *(name for name, pattern in _PROJECT_REQUESTS.items()
                                             if (project_scope != "backend" or name != "runtime_browser")
                                             and _project_requirement(request, pattern)))
        if "archive" in project_checks:
            project_archive_path = next((path for path in paths if path.casefold().endswith(".zip")), None)
            if project_archive_path:
                paths.pop(project_archive_path)
            formats.pop("zip", None)
        if "openapi" in project_checks:
            project_openapi_path = next((path for path in paths if re.search(r"(?:^|/)openapi\.(?:json|yaml|yml)$", path, re.I)), None)
            if project_openapi_path:
                paths.pop(project_openapi_path)
    if paths:
        checks.append("artifact_produced")
    minimum_sources = _MINIMUM_SOURCES.search(request)
    source_count = 0
    if not advisory and (_RESEARCH.search(request) or minimum_sources):
        source_count = 1
        if minimum_sources:
            value = minimum_sources.group(1) or minimum_sources.group(2)
            chinese = {key: index for index, key in enumerate("零一二三四五六七八九十")}
            source_count = int(value) if value.isdigit() else chinese.get(value, 2)
        checks.append("citations_present")
        # 网络调研任务必须有真实检索证据（工具账本），防止凭模型记忆编造调研结论。
        # 用户明确禁网（"不要联网"等）或本次 Run 禁用网络检索时不强加该义务；
        # 用户已上传本地资料且未要求在线检索时，来源义务由 citation 检查按已读文件核对。
        explicit_web = bool(_WEB_INTENT.search(request))
        if web_searchable and not _NO_WEB.search(request) and (explicit_web or not local_sources):
            checks.append("web_evidence")
    if not (advisory or text_only) and _DATABASE.search(request):
        checks.append("db_evidence")
    if not (advisory or text_only) and _EXTERNAL_ACTION.search(request):
        checks.append("side_effect_evidence")
    minimum = _CHAR_COUNT.search(request)
    minimum_chars = int(minimum.group(1)) if minimum and not paths and not formats else 0
    if minimum_chars:
        checks.append("minimum_chars")
    if _STRICT_JSON.search(request) and not paths:
        checks.append("json_answer")
    facts = derive_table_facts(request) if not advisory else None
    json_delivery = "json_answer" in checks or "json" in formats or any(path.casefold().endswith(".json") for path in paths)
    expected_json = facts.declared_fields(request) if facts and json_delivery else {}
    if facts:
        checks.append("table_facts")
    return TaskContract(
        goal=goal,
        task_type=task_type,
        completion_checks=list(dict.fromkeys(checks)),
        source=source,
        artifact_paths=tuple(paths), artifact_formats=tuple(formats), source_count=source_count,
        supplied_urls=tuple(re.findall(r"https?://[^\s<>\"'）)。，；]+", request, re.I)),
        minimum_chars=minimum_chars,
        deep=bool(_DEPTH.search(request) or len(_NUMBERED_STEP.findall(request)) >= 3),
        table_facts=facts, expected_json=expected_json,
        project_scope=project_scope, project_checks=project_checks,
        project_archive_path=project_archive_path, project_openapi_path=project_openapi_path,
    )
