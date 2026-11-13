"""Capability Registry（P2-02）：能力包 → 工具 / Skills / SubAgents 映射与授权。

第一阶段为代码注册表 + 配置化定义（不做市场）。能力包是平台领域抽象，
内部映射到 DeepAgents Tools / Skills / SubAgents / Middleware，不是新的 Agent Loop。

核心语义（任务书 §4.2 / §2.5）：
- 模型可以建议需要什么能力，但不能自行授权——能力最终必须经本注册表解析
  并与用户资源绑定校验（fail-closed）；
- 每个 Capability 声明 ID、版本、描述、关联 Tools/Skills/SubAgents、
  依赖资源、风险等级、所需权限、HITL、重试策略和模型能力要求；
- 资源型能力必须显式绑定资源：data_analysis → database_connection，
  knowledge → knowledge_base；缺失即拒绝。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, FrozenSet, List, Optional, Sequence, Tuple


class CapabilityError(Exception):
    """能力解析 / 授权失败（fail-closed，不得静默降级为全工具）。"""


@dataclass(frozen=True)
class Capability:
    id: str
    version: int
    name: str
    description: str
    tools: Tuple[str, ...] = ()
    skill_sources: Tuple[str, ...] = ()
    subagent_profiles: Tuple[str, ...] = ()
    required_resources: Tuple[str, ...] = ()   # 需要的资源绑定类型
    risk_level: str = "low"                     # low / medium / high
    requires_hitl: bool = False
    requires_network: bool = False
    retry_policy: str = "safe_retry"            # 与 tools/classification 口径一致
    model_requirements: FrozenSet[str] = frozenset({"tool_calling"})
    default_enabled: bool = False
    policy_note: str = ""                       # 默认策略说明（§2.5 表）


# ── 能力包注册表（§2.5）─────────────────────────────────────────────────────

CAPABILITY_PACKS: Dict[str, Capability] = {
    c.id: c
    for c in (
        Capability(
            id="general", version=1, name="通用助手",
            description="对话、计算、时间、基础文件处理",
            tools=("datetime_now", "calculator"),
            default_enabled=True,
            policy_note="默认启用",
        ),
        Capability(
            id="web_research", version=1, name="网络研究",
            description="搜索、网页读取、来源整理、事实核验",
            tools=("web_search", "fetch_webpage", "http_get", "weather",
                   "datetime_now", "calculator"),
            subagent_profiles=("researcher",),
            requires_network=True,
            risk_level="medium",
            policy_note="受管理员网络策略控制",
        ),
        Capability(
            id="doc_processing", version=1, name="文档处理",
            description="PDF/Word/Excel/PPT 读取、提取、生成",
            tools=("knowledge_search", "calculator", "datetime_now"),
            risk_level="medium",
            policy_note="限制在安全文件范围（用户隔离工作区）",
        ),
        Capability(
            id="data_analysis", version=1, name="数据分析",
            description="数据库只读查询、计算、统计、图表",
            tools=("db_schema", "db_query", "db_count", "db_aggregate",
                   "calculator", "datetime_now"),
            subagent_profiles=("data-analyst",),
            required_resources=("database_connection",),
            risk_level="medium",
            policy_note="必须显式绑定数据源；db_query 只读",
        ),
        Capability(
            id="knowledge", version=1, name="知识助手",
            description="知识库检索、引用、知识整理",
            tools=("knowledge_search", "datetime_now"),
            required_resources=("knowledge_base",),
            risk_level="low",
            policy_note="必须绑定授权知识库",
        ),
        Capability(
            id="coding", version=1, name="编程开发",
            description="文件操作、代码分析、测试",
            tools=("python_exec", "calculator", "datetime_now"),
            risk_level="high",
            requires_hitl=True,
            policy_note="文件隔离；代码执行必须有 Sandbox（无 Sandbox 时 execute 面收窄）",
        ),
        Capability(
            id="business_automation", version=1, name="业务自动化",
            description="HTTP、邮件、Webhook、外部业务写操作",
            tools=("http_post", "send_email_tool", "webhook_notify"),
            requires_network=True,
            requires_hitl=True,
            risk_level="high",
            retry_policy="none",
            policy_note="默认关闭，逐项授权并 HITL",
        ),
    )
}


@dataclass
class ResolvedCapabilities:
    capability_ids: List[str]
    tools: List[str] = field(default_factory=list)              # 去重保序
    skill_sources: List[str] = field(default_factory=list)
    subagent_profiles: List[str] = field(default_factory=list)
    requires_hitl: bool = False
    requires_network: bool = False
    model_requirements: set = field(default_factory=set)
    risk_level: str = "low"

    def to_audit_fields(self) -> Dict[str, Any]:
        return {
            "capability_ids": list(self.capability_ids),
            "tools": list(self.tools),
            "requires_hitl": self.requires_hitl,
            "requires_network": self.requires_network,
            "risk_level": self.risk_level,
        }


_RISK_ORDER = {"low": 0, "medium": 1, "high": 2}


def list_capabilities(scope: Any = None, user_context: Any = None) -> List[Dict[str, Any]]:
    """列出可用能力包（含默认策略）。

    ``scope`` / ``user_context`` 预留组织级过滤；当前返回全部注册能力，
    授权收口在 ``resolve_capabilities``（模型可建议、不可授权）。
    """
    return [
        {
            "id": c.id,
            "version": c.version,
            "name": c.name,
            "description": c.description,
            "risk_level": c.risk_level,
            "default_enabled": c.default_enabled,
            "requires_hitl": c.requires_hitl,
            "requires_network": c.requires_network,
            "required_resources": list(c.required_resources),
            "policy_note": c.policy_note,
            "model_requirements": sorted(c.model_requirements),
        }
        for c in CAPABILITY_PACKS.values()
    ]


def resolve_capabilities(
    capability_ids: Optional[Sequence[str]],
    resource_bindings: Optional[Dict[str, Any]] = None,
) -> ResolvedCapabilities:
    """解析能力包为工具/技能/子代理集合（fail-closed）。

    - 未登记能力 ID → ``CapabilityError``（不得默认放行）；
    - 需要资源绑定而未提供 → ``CapabilityError``（如 data_analysis 无数据源）；
    - 空列表按默认能力包（general）处理——平台默认助手语义。
    """
    ids = [i for i in (capability_ids or []) if i]
    if not ids:
        ids = [c.id for c in CAPABILITY_PACKS.values() if c.default_enabled] or ["general"]

    bindings = resource_bindings or {}
    resolved = ResolvedCapabilities(capability_ids=list(ids))
    seen_tools: set = set()
    for cid in ids:
        cap = CAPABILITY_PACKS.get(cid)
        if cap is None:
            raise CapabilityError(f"未登记的能力包: {cid}（模型可建议能力，但授权必须经注册表）")
        for res in cap.required_resources:
            bound = bindings.get(res) or bindings.get(f"{res}_id")
            if not bound:
                raise CapabilityError(
                    f"能力包 '{cap.name}' 需要绑定资源 {res}，当前未提供"
                )
        for t in cap.tools:
            if t not in seen_tools:
                seen_tools.add(t)
                resolved.tools.append(t)
        for s in cap.skill_sources:
            if s not in resolved.skill_sources:
                resolved.skill_sources.append(s)
        for p in cap.subagent_profiles:
            if p not in resolved.subagent_profiles:
                resolved.subagent_profiles.append(p)
        resolved.requires_hitl = resolved.requires_hitl or cap.requires_hitl
        resolved.requires_network = resolved.requires_network or cap.requires_network
        resolved.model_requirements |= set(cap.model_requirements)
        if _RISK_ORDER.get(cap.risk_level, 0) > _RISK_ORDER.get(resolved.risk_level, 0):
            resolved.risk_level = cap.risk_level
    return resolved


def validate_capability_policy(
    resolved: ResolvedCapabilities,
    *,
    sandbox_mode: str = "restricted",
    enable_hitl: bool = True,
    allow_network: bool = True,
) -> None:
    """校验能力包与运行安全策略的一致性（不一致明确拒绝）。

    - coding / business_automation 等高危能力要求 HITL 开启；
    - requires_network 能力在网络被禁用时拒绝；
    - 高危执行能力在 disabled 沙箱模式下拒绝（execute 工具面另由
      factory.build_fs_tools 按沙箱收窄，双层防线）。
    """
    if resolved.requires_hitl and not enable_hitl:
        raise CapabilityError(
            f"能力 {resolved.capability_ids} 要求人工审批（HITL），当前运行禁用了 HITL，拒绝执行"
        )
    if resolved.requires_network and not allow_network:
        raise CapabilityError(
            f"能力 {resolved.capability_ids} 需要网络访问，当前运行禁止网络，拒绝执行"
        )
    if "coding" in resolved.capability_ids and sandbox_mode == "disabled":
        raise CapabilityError("编程开发能力在沙箱禁用模式下不可用")


# ── 给 Harness 组装的映射接口（§4.2）────────────────────────────────────────

def build_toolset(resolved: ResolvedCapabilities) -> List[str]:
    return list(resolved.tools)


def build_skill_sources(resolved: ResolvedCapabilities) -> List[str]:
    return list(resolved.skill_sources)


def build_subagent_profiles(resolved: ResolvedCapabilities) -> List[str]:
    return list(resolved.subagent_profiles)
