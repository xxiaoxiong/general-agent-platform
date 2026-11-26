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
from .tool_batches import NativeToolBatchMiddleware
from .tools.adapter import ToolAdapterFactory
from .tools.policy import ToolPolicy

# 需要 HITL 审批的工具 -> interrupt_on 配置
from langchain.agents.middleware.human_in_the_loop import InterruptOnConfig

# ── P0-05：DeepAgents 原生工具安全 ─────────────────────────────────────────
# 原生文件工具真名（deepagents FsToolName）：delete 为独立工具；
# execute 仅在真实 Sandbox 可用时暴露，否则从工具面移除（A05：不暴露执行能力）。
FS_NATIVE_TOOLS: tuple = ("ls", "read_file", "write_file", "edit_file", "delete", "glob", "grep")


def build_fs_tools(policy: ToolPolicy, backend: Any) -> List[str]:
    """原生工具面白名单：无外部 Sandbox 时不含 execute。

    配置允许执行且实际后端实现了 Sandbox 接口时，才向模型公开 execute。
    """
    from deepagents.backends.protocol import SandboxBackendProtocol

    tools = list(FS_NATIVE_TOOLS)
    if isinstance(backend, SandboxBackendProtocol) and (policy.sandbox_mode == "external" or policy.allow_unsafe_python):
        tools.append("execute")
    return tools


def build_fs_permissions(policy: ToolPolicy) -> List[Any]:
    """原生文件工具统一授权（A04）：FilesystemPermission allow/deny/interrupt 规则。

    - 敏感凭据类文件（.env / 私钥 / credentials / secrets）**读取**一律 deny
      （任意路径深度，与 Backend 路由隔离做纵深防御）；
    - **写保护只锚定 /workspace/**（FilesystemBackend 真实落盘根目录，真实
      凭据文件只可能出现在这里）：deepagents 的 delete 权限检查对无锚点
      （anchor="/"）的 deny-write 模式一律保守阻断，若把 /​**/ 形式放进
      write 规则，删除任何"疑似目录/不存在的文件"都会被误杀并报出误导性
      错误（实测：删除普通 .css 文件被拒并列出 .env 等规则）。StateBackend
      虚拟文件是会话临时状态，无真实凭据可保护；
    - delete 等危险操作的审批由 interrupt_on（ToolPolicy.hitl_tools）承担，
      不依赖风险登记表冒充保护。
    """
    from deepagents.middleware.filesystem import FilesystemPermission

    sensitive = [
        "/**/.env", "/**/.env.*",
        "/**/id_rsa*", "/**/id_ed25519*", "/**/.ssh/**",
        "/**/*.pem", "/**/*.key",
        "/**/credentials*", "/**/secrets/**",
    ]
    return [
        # 读保护：任意路径深度（read 按路径精确匹配，不影响 delete）
        FilesystemPermission(operations=["read"], paths=[*sensitive, "/workspace/.run-artifacts", "/workspace/.run-artifacts/**"], mode="deny"),
        # 写保护：仅锚定真实落盘工作区（带字面前缀，delete 检查可精确推理）
        FilesystemPermission(
            operations=["write"],
            paths=[p.replace("/**/", "/workspace/**/", 1) if p.startswith("/**/") else p
                   for p in sensitive],
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

    # P0-05：原生文件工具面收窄（无 Sandbox 不含 execute）；主代理与子代理同口径。
    # 注意：自定义 FilesystemMiddleware 按名替换基栈同名中间件时只继承 permissions，
    # **不继承 backend** —— 必须显式传入，否则落到默认 StateBackend（会话虚拟文件），
    # 文件工具看不到真实磁盘工作区（上传附件 / tool-outputs 卸载文件都读不到）
    from deepagents.middleware.filesystem import FilesystemMiddleware

    backend = build_backend(ctx.scope.user_id, ctx.agent_id, ctx.session_id, store=store)
    file_tools = build_fs_tools(policy, backend)
    fs_middleware = FilesystemMiddleware(tools=file_tools, backend=backend)
    subagents = [
        {**sa, "tools": _subagent_tools(sa, adapter),
         "middleware": [FilesystemMiddleware(tools=file_tools, backend=backend)]}
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
    middleware = [fs_middleware, TodoListMiddleware(), NativeToolBatchMiddleware()]
    # 执行中补充要求是通用能力（不限目标模式）：每次模型调用前按稳定 ID 注入
    from .steering import SteeringMiddleware

    middleware.insert(0, SteeringMiddleware(ctx, bus))
    if ctx.task_contract is not None:
        from .completion import CompletionMiddleware

        # after_model 按逆序执行，验收回到模型前已更新模型调用计数。
        middleware.insert(0, CompletionMiddleware(ctx, ctx.task_contract, bus))
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
        backend=backend,
        permissions=build_fs_permissions(policy),
        interrupt_on=interrupt_config or None,
        checkpointer=checkpointer,
        store=store,
        name=f"agent-{ctx.agent_id}",
    )
    return agent


def _subagent_tools(subagent: Dict[str, Any], adapter: ToolAdapterFactory):
    """把子代理工具名列表转为实际 StructuredTool（收窄后的交集）。

    source=子代理名：工具事件带 subagent 归属标记，前端按子代理分组展示。
    """
    from app.services.tools import get_tool_by_name

    specs = []
    for name in subagent.get("tools", []):
        spec = get_tool_by_name(name)
        if spec:
            specs.append(spec)
    return adapter.build(specs, source=subagent.get("name"))


def _compose_system_prompt(ctx: RuntimeContext) -> str:
    parts = [ctx.agent_config.get("system_prompt") or "你是一个智能助手。"]
    # 注入当前日期：模型知识有截止时间，时效判断必须知道"今天"，否则会把旧资料当现状
    from datetime import date

    parts.append(f"今天是 {date.today().isoformat()}。涉及「最新/进展/趋势」的问题必须联网检索核实，不得凭记忆断言现状。")
    if ctx.db_info and ctx.db_info.get("hint"):
        parts.append(ctx.db_info["hint"])
    if ctx.memory_context:
        parts.append("## 参考记忆\n" + ctx.memory_context)
    if ctx.task_contract is not None and ctx.task_contract.is_complex:
        from .completion import completion_prompt

        parts.append("## 本次交付要求\n" + completion_prompt(ctx.task_contract))
    if ctx.completion_continuation:
        from .completion import continuation_prompt

        parts.append(continuation_prompt(ctx))
    if ctx.goal_mode:
        # 目标模式不是“只去限制”：注入目标协议（计划强制 + 交付核对 + 诚实缺口）
        from .goal import goal_protocol_prompt

        parts.append(goal_protocol_prompt(ctx))
    deliverable = ctx.agent_config.get("_deliverable")
    if isinstance(deliverable, dict) and deliverable.get("format"):
        fmt = str(deliverable["format"])
        parts.append(
            "## 本轮文件交付\n"
            f"用户要求交付 {fmt} 格式的文件。完成内容后必须用写入/代码工具把最终文件真实保存到 "
            f"`/workspace/` 下（扩展名 .{fmt}，文件名贴合主题），确认写入成功后再在最终回答中"
            "给出文件名、下载位置与关键结论。不得只把内容贴在对话里充当文件交付，"
            "也不得在文件尚未创建时声称已生成或可下载。"
        )
    parts.append(
        "## 执行约束\n"
        "- 始终使用用户提问所用的语言回答（用户用中文则用中文）。\n"
        "- 最终回答必须直接给出用户要的完整结果（报告、方案、分析等正文），不要把完整结果只写进文件"
        "再只回一句摘要或文件路径。调研/分析/写作类任务直接在最终回答中撰写完整正文，"
        "不要把完整报告先写进 /research/ 等临时文件再总结。\n"
        "- 只有 /workspace/ 下的文件会作为用户可下载的会话文件展示；/research/、/notes/、/tmp/ 等路径"
        "是会话临时区，不能作为交付物，也不要声称用户可下载。需要文件交付时写入 /workspace/，"
        "并在最终回答中给出文件路径与关键结论。\n"
        "- 用户消息带 [附件] 时，必须先用 read_file 逐个读取附件再回答；图片附件 read_file 后"
        "可直接看到图片内容，读完再描述/分析；不要跳过读取、不要让用户重新上传。\n"
        "- 工具调用前先确认参数完整；工具失败时给出明确状态，不要假装成功。\n"
        "- write_todos 每轮调用一次即可，不要与其他工具并行发起多个 write_todos。\n"
        "- 同一文件的写入/编辑/读取请串行进行，不要在同一步并行操作同一路径。\n"
        "- write_file 会自动创建工作区中的父目录。请直接填写最终文件路径，目录本身无需写成空文件。\n"
        "- 涉及外部副作用（HTTP POST/邮件/Webhook）的操作会在执行前要求人工审批。\n"
        "- 网络检索得到的事实结论须标注来源：正文用 [N] 编号引用（如 [1]），"
        "文末输出「## 来源清单」表（N / 标题 / 链接 / 发布时间），编号一一对应；"
        "只引用工具实际返回的 URL，不得引用未在检索结果或用户输入中出现的链接。\n"
        "- 用户要求重新发送/输出完整报告时，必须输出全文（保留原有章节并合并更新），"
        "不能只给更新段落或摘要。同一查询方向连续 2 次检索无结果时，停止改写查询，"
        "如实说明未检索到该数据，改用已有来源或标注缺口。\n"
        "- 不要输出与任务无关的内容；最终回答直接给出结论。"
    )
    return "\n\n".join(parts)
