"""MCP 工具注册与运行时装配（扩展性治理：MCP 管理模块）。

- 工具命名空间：``mcp__<server>__<tool>``（与平台内置工具天然隔离）；
- 装配（load_mcp_runtime）：MCPServer 行 × discovered_tools 缓存 × tool_settings
  → 平台工具规格（BUILTIN_TOOLS 同构 dict）+ 运行时连接映射；
- 绑定语义（fail-closed）：仅 ``agent_ids`` 含当前 Agent 且 ``enabled`` 的服务注入；
  Agent 配置 tools 中 ``{"name": "mcp__s__t", "enabled": false}`` 可逐工具收窄；
- 风险登记（register_mcp_tool_risks）：MCP 工具默认 non_idempotent + 需审批，
  管理员可逐服务/逐工具显式降级为 read_only；未登记即被 ToolPolicy 拒绝执行。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.mcp_server import (
    SIDE_EFFECT_NON_IDEMPOTENT,
    SIDE_EFFECT_READ_ONLY,
    MCPServer,
)
# 注意：policy 依赖放在函数内导入（register_mcp_tool_risks），避免
# mcp.registry <-> agent_runtime 包的循环导入（tools/__init__ 顶层引本模块）。

MCP_TOOL_PREFIX = "mcp__"


def mcp_tool_name(server_name: str, tool: str) -> str:
    return f"{MCP_TOOL_PREFIX}{server_name}__{tool}"


def parse_mcp_tool_name(name: str) -> Optional[Tuple[str, str]]:
    """``mcp__<server>__<tool>`` → (server, tool)；server 名含 __ 时按首段切分。"""
    if not name.startswith(MCP_TOOL_PREFIX):
        return None
    rest = name[len(MCP_TOOL_PREFIX):]
    if "__" not in rest:
        return None
    server, tool = rest.split("__", 1)
    return (server, tool) if server and tool else None


def _schema_to_params(input_schema: Any) -> Tuple[Dict[str, Any], List[str]]:
    """MCP inputSchema（JSON Schema）→ 平台工具参数格式（parameters/required）。"""
    if not isinstance(input_schema, dict):
        return {}, []
    props = input_schema.get("properties") or {}
    params: Dict[str, Any] = {}
    for key, spec in props.items():
        spec = spec if isinstance(spec, dict) else {}
        params[key] = {
            "type": spec.get("type", "string"),
            "description": spec.get("description") or "",
        }
    required = [k for k in (input_schema.get("required") or []) if k in params]
    return params, required


def _tool_settings(tool_settings: Dict[str, Any], tool: str) -> Dict[str, Any]:
    raw = (tool_settings or {}).get(tool)
    return raw if isinstance(raw, dict) else {}


def build_tool_specs(
    server_name: str,
    tools: List[Dict[str, Any]],
    *,
    tool_settings: Optional[Dict[str, Any]] = None,
    description_prefix: str = "",
) -> List[Dict[str, Any]]:
    """MCP 工具清单 → 平台工具规格列表（跳过显式 disabled 的工具）。"""
    settings_map = tool_settings or {}
    specs: List[Dict[str, Any]] = []
    for t in tools:
        name = t.get("name")
        if not name:
            continue
        override = _tool_settings(settings_map, name)
        if override.get("enabled", True) is False:
            continue
        params, required = _schema_to_params(t.get("inputSchema"))
        desc = t.get("description") or f"MCP 工具 {name}"
        specs.append({
            "name": mcp_tool_name(server_name, name),
            "display_name": f"🔧 {name}",
            "category": "MCP",
            "description": f"{description_prefix}{desc}",
            "parameters": params,
            "required": required,
        })
    return specs


async def load_mcp_runtime(
    db: Optional[AsyncSession],
    agent_id: Optional[int],
    agent_config: Dict[str, Any],
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """装配当前 Agent 的 MCP 工具面（规格 + 运行时连接映射）。

    返回 (specs, runtime)；runtime 形如
    ``{server_name: {"connection": {...}, "tools": {tool: {"requires_approval", "side_effect_class"}}}}``。
    无绑定/无发现工具时返回 ([], {})。db 为空时返回空（未启用场景）。
    """
    if db is None:
        return [], {}
    rows = (await db.execute(select(MCPServer).where(MCPServer.enabled == True))).scalars().all()  # noqa: E712
    # Agent 配置里显式禁用的 MCP 工具名（{"name": ..., "enabled": false}）
    disabled = {
        item["name"]
        for item in (agent_config.get("tools") or [])
        if isinstance(item, dict) and item.get("enabled", True) is False and item.get("name")
    }

    specs: List[Dict[str, Any]] = []
    runtime: Dict[str, Any] = {}
    for row in rows:
        bound = row.agent_ids or []
        if agent_id is not None and agent_id not in bound:
            continue  # fail-closed：未显式绑定的 Agent 不注入
        server_specs = build_tool_specs(
            row.name,
            row.discovered_tools or [],
            tool_settings=row.tool_settings or {},
            description_prefix=f"[MCP:{row.name}] ",
        )
        server_specs = [s for s in server_specs if s["name"] not in disabled]
        if not server_specs:
            continue
        specs.extend(server_specs)
        tools_meta: Dict[str, Any] = {}
        for s in server_specs:
            tool = parse_mcp_tool_name(s["name"])[1]
            override = _tool_settings(row.tool_settings or {}, tool)
            tools_meta[s["name"]] = {
                "requires_approval": bool(override.get("requires_approval", row.require_approval)),
                "side_effect_class": override.get(
                    "side_effect_class",
                    row.side_effect_class or SIDE_EFFECT_NON_IDEMPOTENT,
                ),
            }
        runtime[row.name] = {
            "connection": {
                "transport": row.transport,
                "command": row.command or "",
                "args": list(row.args or []),
                "env": dict(row.env or {}),
                "url": row.url or "",
                "headers": dict(row.headers or {}),
            },
            "tools": tools_meta,
        }
    return specs, runtime


def register_mcp_tool_risks(mcp_runtime: Dict[str, Any]) -> None:
    """把 MCP 工具显式登记进 ToolRiskRegistry（fail-closed 的前置条件）。

    - read_only：可安全重试（safe_retry）；
    - 其余（non_idempotent 等）：结果未知禁止自动重试，默认需 HITL 审批。
    """
    from app.services.agent_runtime.tools.policy import (
        CLASS_READ_ONLY,
        CLASS_NON_IDEMPOTENT,
        RETRY_NONE,
        RETRY_SAFE,
        ToolRisk,
        ToolRiskProfile,
        register_tool_risk,
    )

    for server_name, entry in (mcp_runtime or {}).items():
        for tool_name, meta in (entry.get("tools") or {}).items():
            side = meta.get("side_effect_class") or SIDE_EFFECT_NON_IDEMPOTENT
            read_only = side == SIDE_EFFECT_READ_ONLY
            register_tool_risk(ToolRiskProfile(
                name=tool_name,
                risk=ToolRisk.READ if read_only else ToolRisk.NETWORK,
                side_effect_class=CLASS_READ_ONLY if read_only else CLASS_NON_IDEMPOTENT,
                requires_approval=bool(meta.get("requires_approval", True)),
                retry_policy=RETRY_SAFE if read_only else RETRY_NONE,
                network_access=not read_only,
            ))
