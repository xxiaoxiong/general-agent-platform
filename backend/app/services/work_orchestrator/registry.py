"""Executor Registry：把受控 executor_profile 映射到真实存在的执行引擎。

安全边界（任务书 §6）：
- Registry 只由服务端维护，模型只能提出 profile 建议；
- 未注册的 profile、当前 Agent 未授权的执行器、未开启的团队模式一律拒绝；
- 注册表只描述"用哪个已有引擎执行"，不授予任何新工具/权限。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

from .schemas import ExecutorProfile

# 各 profile 需要的工具信号（满足任一即可，代表该执行器有真实能力支撑）
RESEARCH_TOOLS = frozenset({"web_search", "fetch_webpage", "http_get", "knowledge_search"})
DATABASE_TOOLS = frozenset({"db_schema", "db_query", "db_count", "db_aggregate"})
BUSINESS_TOOLS = frozenset({"http_post", "send_email_tool", "webhook_notify"})
CODING_TOOLS = frozenset({"python_exec", "execute", "execute_command", "start_service"})


@dataclass(frozen=True)
class ExecutorSpec:
    profile: ExecutorProfile
    engine: str                     # deep_agent / document_graph / team_graph / team_project_graph
    label: str                      # 用户可理解的中文名称
    description: str
    requires_tools: frozenset[str] = frozenset()
    requires_team: bool = False


_REGISTRY: dict[str, ExecutorSpec] = {
    "general_agent": ExecutorSpec(
        "general_agent", "deep_agent", "通用智能体",
        "通用推理、分析、综合、写作与普通任务，使用当前助手的完整授权工具面",
    ),
    "research": ExecutorSpec(
        "research", "deep_agent", "资料检索与研究",
        "联网搜索、网页读取、来源整理与事实核验",
        requires_tools=RESEARCH_TOOLS,
    ),
    "data_analysis": ExecutorSpec(
        "data_analysis", "deep_agent", "数据分析",
        "读取会话数据/表格，进行统计、计算与分析",
    ),
    "database": ExecutorSpec(
        "database", "deep_agent", "数据库查询",
        "通过已绑定数据库执行只读查询与聚合",
        requires_tools=DATABASE_TOOLS,
    ),
    "coding": ExecutorSpec(
        "coding", "deep_agent", "代码任务",
        "编写、修改、调试代码，使用当前代码/工作区工具",
    ),
    "business_action": ExecutorSpec(
        "business_action", "deep_agent", "受控业务操作",
        "发邮件、Webhook、通知等外部副作用操作，继续遵守 ToolPolicy 与 HITL 审批",
        requires_tools=BUSINESS_TOOLS,
    ),
    "document_docx": ExecutorSpec(
        "document_docx", "document_graph", "Word 文档",
        "生成、编辑、审阅 DOCX 文档，并输出可预览/可下载版本",
    ),
    "team": ExecutorSpec(
        "team", "team_graph", "团队协作",
        "单个复杂工作步骤内部的多角色分工、并行与证据复核",
        requires_team=True,
    ),
    "software_project": ExecutorSpec(
        "software_project", "team_project_graph", "软件项目",
        "软件项目规划、源码生成、运行与验收",
        requires_team=True,
    ),
}


@dataclass
class ResolvedExecutor:
    spec: ExecutorSpec
    allowed: bool
    reason: str = ""
    warnings: list[str] = field(default_factory=list)


class ExecutorRegistry:
    """服务端执行器注册表（按 Run 的实际权限解析）。"""

    def __init__(
        self,
        *,
        tool_names: Iterable[str],
        capability_ids: Iterable[str] = (),
        auto_team: bool = False,
        explicit_team: bool = False,
        database_bound: bool = False,
        web_enabled: bool = True,
    ) -> None:
        self.tools = frozenset(str(name) for name in tool_names if name)
        self.capabilities = frozenset(str(item) for item in capability_ids if item)
        self.auto_team = bool(auto_team)
        self.explicit_team = bool(explicit_team)
        self.database_bound = bool(database_bound)
        self.web_enabled = bool(web_enabled)

    def spec(self, profile: str) -> ExecutorSpec | None:
        return _REGISTRY.get(str(profile))

    def resolve(self, profile: str) -> ResolvedExecutor:
        spec = self.spec(profile)
        if spec is None:
            return ResolvedExecutor(
                spec=ExecutorSpec("general_agent", "deep_agent", "通用智能体", ""),
                allowed=False, reason=f"未注册的执行器 {profile}",
            )
        if spec.requires_team and not (self.auto_team or self.explicit_team):
            return ResolvedExecutor(spec, False, "当前未开启团队协作，不能使用该执行器")
        available = set(self.tools)
        if spec.profile == "database":
            if not self.database_bound:
                return ResolvedExecutor(spec, False, "当前助手未绑定数据库")
        if spec.requires_tools and not (available & set(spec.requires_tools)):
            missing = "、".join(sorted(spec.requires_tools))
            return ResolvedExecutor(spec, False, f"当前助手缺少该执行器所需工具（{missing}）")
        if spec.profile in {"research"} and not self.web_enabled:
            return ResolvedExecutor(spec, False, "本轮已关闭联网检索")
        return ResolvedExecutor(spec, True)

    def allowed_profiles(self) -> list[ExecutorSpec]:
        return [spec for name, spec in _REGISTRY.items() if self.resolve(name).allowed]

    def capability_allowed(self, names: Iterable[str]) -> tuple[list[str], list[str]]:
        """返回 (已授权能力, 被拒绝能力)；能力只能来自当前助手。"""
        allowed, denied = [], []
        for name in names:
            text = str(name or "").strip()
            if not text:
                continue
            (allowed if text in self.capabilities else denied).append(text)
        return allowed, denied

    def render_prompt(self) -> str:
        """给 Planner 的执行器清单（只列出当前真正可用的执行器）。"""
        lines = ["## 可用执行器（只能从以下 profile 中选择）"]
        for spec in self.allowed_profiles():
            lines.append(f"- {spec.profile}（{spec.label}）：{spec.description}")
        denied = [
            f"- {spec.profile}：{self.resolve(spec.profile).reason}"
            for spec in _REGISTRY.values()
            if not self.resolve(spec.profile).allowed
        ]
        if denied:
            lines.append("\n## 当前不可用执行器（禁止在计划中使用）")
            lines.extend(denied)
        if self.capabilities:
            lines.append("\n## 已授权能力")
            lines.append("- " + "、".join(sorted(self.capabilities)))
        return "\n".join(lines)


def engine_for_profile(profile: str) -> str:
    spec = _REGISTRY.get(profile)
    return spec.engine if spec else ""
