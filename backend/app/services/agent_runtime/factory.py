"""
DeepAgents 内核组装工厂。

create_deep_agent(...) 一次性组装：
- LangChain ChatModel（model_adapter 构造）
- 平台业务工具（ToolAdapterFactory）
- Middleware：TodoListMiddleware（规划）、ToolCallLimit/ModelCallLimit（预算）、
  DeepAgents 内置 SummarizationMiddleware（上下文压缩）
- SubAgent（subagents.py，收窄工具）
- Skills（skills=["/skills/"] + StateBackend files 注入）
- HITL：interrupt_on 高危工具
- Checkpointer / Store / Backend（persistence / backends）

组装一次即用，不做跨 run 缓存：工具闭包绑定 run 级上下文（db session、
事件总线、预算计数），缓存会引入跨 run 状态泄漏。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from langchain.agents.middleware import (
    ModelCallLimitMiddleware,
    TodoListMiddleware,
    ToolCallLimitMiddleware,
)

from app.core.config import settings

from .backends import build_backend
from .context import RuntimeContext
from .events import EventBus
from .model_adapter import build_chat_model
from .skills import SkillRegistry
from .subagents import build_subagents
from .tools.adapter import ToolAdapterFactory
from .tools.policy import ToolPolicy

# 需要 HITL 审批的工具 -> interrupt_on 配置
from langchain.agents.middleware.human_in_the_loop import InterruptOnConfig

# ── P0-05：DeepAgents 原生工具安全 ─────────────────────────────────────────
# 原生文件工具真名（deepagents FsToolName）：delete 为独立工具；
# execute 仅在真实 Sandbox 可用时暴露，否则从工具面移除（A05：不暴露执行能力）。
FS_NATIVE_TOOLS: tuple = ("ls", "read_file", "write_file", "edit_file", "delete", "glob", "grep")


def build_fs_tools(policy: ToolPolicy) -> List[str]:
    """原生工具面白名单：无外部 Sandbox 时不含 execute。

    restricted（进程内限制）不是安全沙箱；仅 external 或开发逃生开关
    （allow_unsafe_python）时才暴露 execute，与 ToolPolicy.check_exec_allowed 同口径。
    """
    tools = list(FS_NATIVE_TOOLS)
    if policy.sandbox_mode == "external" or policy.allow_unsafe_python:
        tools.append("execute")
    return tools


def build_fs_permissions(policy: ToolPolicy) -> List[Any]:
    """原生文件工具统一授权（A04）：FilesystemPermission allow/deny/interrupt 规则。

    - 敏感凭据类文件（.env / 私钥 / credentials / secrets）读写一律 deny，
      独立于 Backend 路由隔离做纵深防御（delete 命中 deny-write 同样被拒）；
    - 其余路径默认 allow：实际读写只会落在用户/Agent/Session 隔离工作区
      （/workspace/ FilesystemBackend root）与会话虚拟文件（StateBackend）；
    - delete 等危险操作的审批由 interrupt_on（ToolPolicy.hitl_tools）承担，
      不依赖风险登记表冒充保护。
    """
    from deepagents.middleware.filesystem import FilesystemPermission

    return [
        FilesystemPermission(
            operations=["read", "write"],
            paths=[
                "/**/.env", "/**/.env.*",
                "/**/id_rsa*", "/**/id_ed25519*", "/**/.ssh/**",
                "/**/*.pem", "/**/*.key",
                "/**/credentials*", "/**/secrets/**",
            ],
            mode="deny",
        ),
    ]


def build_interrupt_config(policy: ToolPolicy) -> Dict[str, Any]:
    if not policy.enable_hitl:
        return {}
    return {
        name: InterruptOnConfig(
            allowed_decisions=["approve", "reject"],
            description=f"高风险工具 {name}，需要人工审批后执行",
        )
        for name in sorted(policy.hitl_tools)
    }


def build_agent(
    ctx: RuntimeContext,
    policy: ToolPolicy,
    bus: EventBus,
    provider: Dict[str, Any],
    model_config: Dict[str, Any],
    tool_specs: List[Dict[str, Any]],
    *,
    checkpointer: Any = None,
    store: Any = None,
):
    """组装 DeepAgents 编译图。"""
    from deepagents import create_deep_agent

    chat_model = build_chat_model(
        provider,
        model_config,
        temperature=ctx.agent_config.get("temperature"),
    )

    adapter = ToolAdapterFactory(ctx, policy, bus)
    langchain_tools = adapter.build(tool_specs)

    # P0-05：原生文件工具面收窄（无 Sandbox 不含 execute）；主代理与子代理同口径
    from deepagents.middleware.filesystem import FilesystemMiddleware

    fs_middleware = FilesystemMiddleware(tools=build_fs_tools(policy))
    subagents = [
        {**sa, "tools": _subagent_tools(sa, adapter),
         "middleware": [FilesystemMiddleware(tools=build_fs_tools(policy))]}
        for sa in build_subagents(policy)
    ]

    registry = SkillRegistry.from_settings()
    skill_sources = ["/skills/"] if registry.enabled_skills() else None

    interrupt_config = build_interrupt_config(policy)

    # P2-04：长期记忆 —— MemoryMiddleware 从 /memory/AGENTS.md（StoreBackend，
    # 跨 Session 持久化、按用户隔离）加载记忆并支持 edit_file 更新；
    # memory_policy.use_memory_context=false 的 Agent 不挂载
    memory_policy = (ctx.agent_config.get("memory_policy") or {})
    memory_sources = ["/memory/AGENTS.md"] if memory_policy.get("use_memory_context", True) else None

    # exit_behavior="error"：超限抛出明确异常，由 service 映射为
    # limit_exceeded 状态（任务书 P0-A：超限返回明确 failed/limit_exceeded）
    # 预算 <=0 = 不限制：不挂对应 Limit 中间件（防真死循环仍由重复调用防护兜底）
    # FilesystemMiddleware 替换基栈同名中间件（按名替换、保留顺序），继承 permissions
    middleware = [fs_middleware, TodoListMiddleware()]
    if ctx.budget.max_tool_calls > 0:
        middleware.append(
            ToolCallLimitMiddleware(run_limit=ctx.budget.max_tool_calls, exit_behavior="error")
        )
    if ctx.budget.max_model_calls > 0:
        middleware.append(
            ModelCallLimitMiddleware(run_limit=ctx.budget.max_model_calls, exit_behavior="error")
        )

    system_prompt = _compose_system_prompt(ctx)

    agent = create_deep_agent(
        chat_model,
        langchain_tools,
        system_prompt=system_prompt,
        middleware=middleware,
        subagents=subagents or None,
        skills=skill_sources,
        memory=memory_sources,
        backend=build_backend(ctx.scope.user_id, ctx.agent_id, ctx.session_id, store=store),
        permissions=build_fs_permissions(policy),
        interrupt_on=interrupt_config or None,
        checkpointer=checkpointer,
        store=store,
        name=f"agent-{ctx.agent_id}",
    )
    return agent


def _subagent_tools(subagent: Dict[str, Any], adapter: ToolAdapterFactory):
    """把子代理工具名列表转为实际 StructuredTool（收窄后的交集）。"""
    from app.services.tools import get_tool_by_name

    specs = []
    for name in subagent.get("tools", []):
        spec = get_tool_by_name(name)
        if spec:
            specs.append(spec)
    return adapter.build(specs)


def _compose_system_prompt(ctx: RuntimeContext) -> str:
    parts = [ctx.agent_config.get("system_prompt") or "你是一个智能助手。"]
    if ctx.db_info and ctx.db_info.get("hint"):
        parts.append(ctx.db_info["hint"])
    if ctx.memory_context:
        parts.append("## 参考记忆\n" + ctx.memory_context)
    parts.append(
        "## 执行约束\n"
        "- 工具调用前先确认参数完整；工具失败时给出明确状态，不要假装成功。\n"
        "- 涉及外部副作用（HTTP POST/邮件/Webhook）的操作会在执行前要求人工审批。\n"
        "- 不要输出与任务无关的内容；最终回答直接给出结论。"
    )
    return "\n\n".join(parts)
