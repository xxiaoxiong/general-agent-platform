"""
SubAgent 配置：专用子代理注册，最小权限收窄（P2-06 注册表）。

内置示例（可实际调用）：
- researcher   研究检索：网络搜索/网页抓取/时间/计算
- data-analyst 数据分析：数据库查询/计算/时间

安全边界：
- 工具集合由父运行的 ToolPolicy 派生并收窄（白名单 ∩ 父授权）；
- 派生深度受 ExecutionBudget.max_subagent_depth 控制；
- 只允许委派注册表内 Profile——模型运行时无法创建未登记子代理或工具
  （task 工具的 subagent_type 仅接受注册名）；
- 子代理与父代理共享同一 Run Trace（Langfuse 统一关联）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set

from .tools.policy import ToolPolicy

# 各子代理允许的工具（与平台 BUILTIN_TOOLS 名称对应）
RESEARCHER_TOOLS: Set[str] = {"web_search", "fetch_webpage", "datetime_now", "calculator", "knowledge_search"}
DATA_ANALYST_TOOLS: Set[str] = {"db_schema", "db_query", "db_count", "db_aggregate", "calculator", "datetime_now"}
# 通用子代理：deepagents 默认会自动注入 general-purpose（全工具面、绕过注册表白名单），
# 收编进注册表并收窄工具（文件工具仍由 FilesystemMiddleware 提供）
GENERAL_PURPOSE_TOOLS: Set[str] = RESEARCHER_TOOLS | {"python_exec"}


@dataclass(frozen=True)
class SubAgentProfile:
    """可注册的子代理 Profile（P2-06）：白名单 + 模型策略 + 技能 + 预算。"""

    name: str
    description: str
    system_prompt: str
    tools: frozenset
    model_policy: str = "inherit"        # inherit=随父模型 / 未来可指定模型策略
    skills: tuple = ()                   # 子代理技能引用
    max_depth: int = 1                   # 子代理自身可再派生的深度
    enabled: bool = True


SUBAGENT_PROFILES: Dict[str, SubAgentProfile] = {
    p.name: p
    for p in (
        SubAgentProfile(
            name="researcher",
            description="研究检索子代理：负责网络搜索、资料收集与事实核对，返回调研摘要。",
            system_prompt=(
                "你是研究检索子代理，执行网络调研任务时严格遵守以下研究协议：\n"
                "1. 提纲先行：先列出调研提纲（覆盖问题全部维度），再逐项检索；"
                "检索完成后对照提纲检查缺口，缺什么补搜什么，直到提纲各项都有依据。\n"
                "2. 先检索后作答：必须先用 web_search 检索，再用 fetch_webpage 阅读关键来源；"
                "禁止仅凭模型记忆输出调研结论，禁止编造数据、星标数、发布日期。\n"
                "3. 迭代补搜：先宽搜建立概念，再针对缺口补搜；关键事实（数字/日期/排名）"
                "尽量用 2 个独立来源交叉核对，无法核对的信息标注「单一来源」。\n"
                "4. 时效要求：涉及「最新/进展/趋势」的问题必须检索，并优先采用带发布日期的"
                "近期资料；引用时注明信息时间，过时信息要明确说明其时间。检索到的最新资料"
                "若明显早于当前时间，必须标注资料截止时间，不得把旧资料当作当前状态。\n"
                "5. 引用规范：正文中每条事实结论后用 [N] 编号引用（如 [1][2]），文末输出"
                "「## 来源清单」表（N / 标题 / URL / 发布时间），编号一一对应；"
                "URL 必须来自检索结果，不得引用未在检索结果中出现的链接。\n"
                "6. 输出结构化调研摘要（要点 + 来源清单），不要输出与调研无关的内容。"
            ),
            tools=frozenset(RESEARCHER_TOOLS),
        ),
        SubAgentProfile(
            name="data-analyst",
            description="数据分析子代理：负责数据库查询、统计聚合与数据解读，返回分析结论。",
            system_prompt=(
                "你是数据分析子代理。使用数据库查询工具获取数据并进行统计分析，"
                "只允许只读查询，输出结论与关键数字，不要修改任何数据。"
            ),
            tools=frozenset(DATA_ANALYST_TOOLS),
        ),
        SubAgentProfile(
            name="general-purpose",
            description="通用子代理：处理文件转换、脚本执行等一般性杂项任务，返回处理结果。",
            system_prompt=(
                "你是通用子代理，处理一般性杂项任务（文件处理、脚本运行、格式整理等）。"
                "如实报告执行结果：工具无法完成的事（如写入用户本地磁盘、安装软件）"
                "直接说明做不到并给出可行替代，不要假装成功或虚构输出。"
            ),
            tools=frozenset(GENERAL_PURPOSE_TOOLS),
        ),
    )
}


class SubAgentRegistry:
    """子代理注册表：登记 / 查询 / 按父策略解析（白名单收窄）。"""

    def __init__(self, profiles: Optional[Dict[str, SubAgentProfile]] = None):
        self._profiles = dict(profiles if profiles is not None else SUBAGENT_PROFILES)

    def register(self, profile: SubAgentProfile) -> None:
        self._profiles[profile.name] = profile

    def get(self, name: str) -> Optional[SubAgentProfile]:
        return self._profiles.get(name)

    def list_profiles(self, *, enabled_only: bool = True) -> List[SubAgentProfile]:
        return [p for p in self._profiles.values() if p.enabled or not enabled_only]


def build_subagents(policy: ToolPolicy, registry: Optional[SubAgentRegistry] = None) -> List[Dict[str, Any]]:
    """按当前运行策略生成 deepagents subagents 配置（工具收窄到交集）。"""
    from app.core.time_context import current_time_text

    reg = registry or SubAgentRegistry()
    # 注入当前时间：子代理系统提示独立于主提示，时效判断同样依赖"现在几点"
    today_line = f"\n\n当前时间：{current_time_text()}。"
    out = []
    for p in reg.list_profiles():
        allowed = sorted(set(p.tools) & policy.allowed_tools)
        # general-purpose 即使工具面为空也必须保留：一旦缺席，deepagents 会自动注入
        # 默认全工具 general-purpose，绕过注册表白名单
        if not allowed and p.name != "general-purpose":
            continue
        out.append({
            "name": p.name,
            "description": p.description,
            "system_prompt": p.system_prompt + today_line,
            "tools": allowed,
        })
    return out
