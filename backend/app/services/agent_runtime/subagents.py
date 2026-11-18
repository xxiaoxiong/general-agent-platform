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
                "你是研究检索子代理。使用搜索与网页抓取工具收集信息，"
                "输出结构化调研摘要（要点 + 来源），不要输出与调研无关的内容。"
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
    reg = registry or SubAgentRegistry()
    out = []
    for p in reg.list_profiles():
        allowed = sorted(set(p.tools) & policy.allowed_tools)
        if not allowed:
            continue
        out.append({
            "name": p.name,
            "description": p.description,
            "system_prompt": p.system_prompt,
            "tools": allowed,
        })
    return out
