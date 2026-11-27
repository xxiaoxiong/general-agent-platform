"""配置/模型/工具装配与 Run 提交/Trace 富化（自 service.py 机械拆分，逻辑零改动）。"""
from __future__ import annotations

from ._common import *  # noqa: F401,F403 机械拆分共享依赖
from ._common import (
    _AGENT_CONFIG_VERSION_KEYS,
    _BG_TASKS,
    _STOP_MESSAGES,
    _RunLimitExceeded,
    _action_from_req,
    _derive_title,
    _extract_text,
    _field,
    _last_user_input,
    _synthetic_terminal_event,
    compute_request_hash,
)


class ConfigMixin:

    # ── 内部：装载 ────────────────────────────────────────────────────────

    async def _load_agent_config(self, agent_id: int) -> Dict[str, Any]:
        from sqlalchemy import select
        from app.models.agent import Agent

        async with AsyncSessionLocal() as db:
            agent = (await db.execute(
                select(Agent).where(Agent.id == agent_id, Agent.is_active == True)  # noqa: E712
            )).scalar_one_or_none()
            if agent is None:
                raise RuntimeError(f"Agent {agent_id} 不存在或已禁用")
            return {
                "id": agent.id,
                "name": agent.name,
                "model_config_id": agent.model_config_id,
                "system_prompt": agent.system_prompt,
                "max_iterations": agent.max_iterations,
                "temperature": agent.temperature,
                "tools": agent.tools or [],
                # P3-01：能力包绑定随配置进入运行时（_resolve_tool_specs 消费）
                "capability_ids": getattr(agent, "capability_ids", None) or [],
                "knowledge_base_id": agent.knowledge_base_id,
                "knowledge_base_ids": list(
                    getattr(agent, "knowledge_base_ids", None)
                    or ([agent.knowledge_base_id] if agent.knowledge_base_id else [])
                ),
                "database_connection_id": agent.database_connection_id,
            }

    async def _load_model(self, model_config_id: Optional[int]) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        from sqlalchemy import select
        from app.models.model_provider import ModelConfig, ModelProvider

        async with AsyncSessionLocal() as db:
            if model_config_id:
                mc = (await db.execute(select(ModelConfig).where(ModelConfig.id == model_config_id))).scalar_one_or_none()
                if mc is None:
                    raise ModelCapabilityError(f"模型配置 {model_config_id} 不存在")
                provider = (await db.execute(select(ModelProvider).where(ModelProvider.id == mc.provider_id))).scalar_one_or_none()
                if provider is None:
                    raise ModelCapabilityError(f"模型供应商不存在（model_config_id={model_config_id}）")
            else:
                # P2-03 Model Router：未绑定模型时按能力约束选模（默认模型策略），
                # 无兼容模型明确报错，绝不静默切换到不支持 Tool Calling 的模型
                from app.services.assistant.model_router import candidate_from_row, select_model

                rows = (await db.execute(
                    select(ModelConfig, ModelProvider)
                    .join(ModelProvider, ModelProvider.id == ModelConfig.provider_id)
                    .where(ModelConfig.is_active == True, ModelConfig.model_type == "chat",  # noqa: E712
                           ModelProvider.is_active == True)  # noqa: E712
                )).all()
                candidates = [candidate_from_row(mc, p) for mc, p in rows]
                chosen = select_model(candidates, requirements=("tool_calling",))
                mc = next(mc for mc, _p in rows if mc.id == chosen.id)
                provider = next(p for _mc, p in rows if p.id == mc.provider_id)

            # P2-03：能力矩阵检查（不兼容模型在执行前明确报错，AG-10）
            from app.services.assistant.model_router import candidate_from_row, check_model_requirements

            check_model_requirements(
                candidate_from_row(mc, provider), requirements=("tool_calling",)
            )
            return (
                {"name": provider.name, "base_url": provider.base_url, "api_key": provider.api_key,
                 "extra_config": provider.extra_config or {}},
                {"name": mc.name, "max_tokens": mc.max_tokens, "supports_streaming": mc.supports_streaming,
                 "supports_vision": mc.supports_vision,
                 "supports_function_call": mc.supports_function_call, "extra_params": mc.extra_params or {}},
            )

    def _resolve_tool_specs(self, agent_config: Dict[str, Any]) -> List[Dict[str, Any]]:
        from app.services.tools import get_tool_by_name

        names: List[str] = []
        disabled: set = set()
        # P2-02 能力包解析：capability_ids → 工具集（fail-closed，模型不可自行授权）；
        # 未配置 capability_ids 的存量 Agent 保持显式 tools 语义；
        # 显式 enabled=False 的工具优先级高于能力包默认（Agent 级显式收窄）
        cap_ids = agent_config.get("capability_ids")
        if cap_ids:
            from app.services.assistant.capability import resolve_capabilities

            resolved = resolve_capabilities(
                cap_ids,
                {
                    "database_connection": agent_config.get("database_connection_id"),
                    "knowledge_base": (
                        agent_config.get("knowledge_base_ids")
                        or agent_config.get("knowledge_base_id")
                    ),
                },
            )
            names.extend(resolved.tools)
        for item in (agent_config.get("tools") or []):
            name = item["name"] if isinstance(item, dict) else item
            enabled = item.get("enabled", True) if isinstance(item, dict) else True
            if not enabled:
                disabled.add(name)
                continue
            names.append(name)

        specs = []
        seen = set()
        for name in names:
            if name in seen or name in disabled:
                continue
            seen.add(name)
            spec = get_tool_by_name(name)
            if spec:
                specs.append(spec)
        if agent_config.get("web_enabled") is False:
            # 联网检索开关（收窄语义）：本次 Run 禁用网络检索工具，
            # 子代理工具集与 policy 同步收窄（子代理工具 ∩ 允许集）
            from app.services.tools import WEB_RETRIEVAL_TOOLS

            specs = [s for s in specs if s["name"] not in WEB_RETRIEVAL_TOOLS]
        elif agent_config.get("web_enabled") is True and settings.AGENT_ALLOW_NETWORK:
            # 联网开关是用户级显式授权：Agent 未配置网络研究能力时补齐只读检索工具，
            # 否则开关打开却没有工具可用（前端默认开启，用户会误以为已经联网）。
            # 只补 web_search / fetch_webpage（只读检索），不补 http_get 等通用请求工具；
            # Agent 显式 enabled=False 的工具不补（Agent 级收窄优先）。
            from app.services.tools import get_tool_by_name

            existing = {spec["name"] for spec in specs}
            for name in ("web_search", "fetch_webpage"):
                if name in existing or name in disabled:
                    continue
                spec = get_tool_by_name(name)
                if spec:
                    specs.append(spec)
        # 历史会话检索工具（Conversation Memory）：只读、受 ToolPolicy 约束。
        # 显式 history_memory_enabled=false 或 Agent enabled=false 时不注入。
        if (
            bool(getattr(settings, "CONVERSATION_MEMORY_ENABLED", True))
            and agent_config.get("history_tools_enabled") is True
            and agent_config.get("history_memory_enabled") is not False
        ):
            from app.services.conversation_memory.tools import TOOLS as MEMORY_TOOL_SPECS

            existing = {spec["name"] for spec in specs}
            for spec in MEMORY_TOOL_SPECS:
                if spec["name"] in existing or spec["name"] in disabled:
                    continue
                specs.append(spec)
        # MCP 工具面（装配期写入，见 _load_mcp_runtime）：与内置工具同口径进入
        # 工具面/事件桥接/授权集合
        specs.extend(agent_config.get("mcp_tool_specs") or [])
        # Workspace Sandbox 工具：仅当 Run 绑定项目且 SANDBOX_ENABLED 时注入
        if agent_config.get("sandbox_enabled"):
            from app.services.tools import SANDBOX_TOOLS

            existing = {spec["name"] for spec in specs}
            for spec in SANDBOX_TOOLS:
                if spec["name"] not in existing:
                    specs.append(spec)
        return specs

    async def _load_session_project_id(self, session_id: Optional[int]) -> Optional[int]:
        """读取会话的项目归属（Workspace Sandbox）；存量会话为 None。"""
        if not session_id:
            return None
        from sqlalchemy import select

        from app.models.agent import AgentSession

        async with AsyncSessionLocal() as db:
            value = (await db.execute(
                select(AgentSession.project_id).where(AgentSession.id == session_id)
            )).scalar_one_or_none()
        return int(value) if value else None

    async def _load_mcp_runtime(
        self, agent_id: Optional[int], agent_config: Dict[str, Any]
    ) -> tuple:
        """MCP 工具装配（短会话查询，不复用 Run 级 tool_db）。"""
        from app.services.mcp.registry import load_mcp_runtime

        async with AsyncSessionLocal() as db:
            return await load_mcp_runtime(db, agent_id, agent_config)

    async def _skill_files(self, db, agent_id: Optional[int]) -> Dict[str, Any]:
        """技能注入：内置/目录技能（全局）+ 管理面 DB 技能（Agent 级绑定过滤）。

        同名技能 DB **整目录覆盖** registry（不是只覆盖 SKILL.md 的杂交残留）：
        先剔除 registry 中 /skills/<name>/ 前缀的键，再合并 DB 技能文件。
        """
        from sqlalchemy import select

        from app.models.skill import Skill

        from app.services.agent_runtime.skills import SkillRegistry, db_enabled_names, db_files_payload

        registry = SkillRegistry.from_settings()
        files = registry.files_payload()
        rows = (await db.execute(select(Skill))).scalars().all()
        for name in db_enabled_names(rows, agent_id):
            prefix = f"/skills/{name}/"
            for key in [k for k in files if k.startswith(prefix)]:
                del files[key]
        files.update(db_files_payload(rows, agent_id))
        return {"files": files} if files else {}

    def _db_hint(self, db_info: Dict[str, Any]) -> str:
        if "database" not in db_info:
            return (
                f"## 数据库连接\n已配置数据源 {db_info.get('name')}。"
                "使用 db_schema 获取表结构，再用 db_query 执行只读查询。"
            )
        tables = ", ".join(db_info.get("tables") or []) or "(未预加载)"
        return (
            f"## 数据库连接\n已连接数据库 `{db_info.get('database')}`（主机 {db_info.get('host')}）。\n"
            f"db_query/db_count/db_aggregate 工具将作用于该数据库（只读）。可用表：{tables}"
        )

    # ── 内部：持久化（关键写入失败一律向上抛出） ──────────────────────────

    async def _commit(
        self,
        run: RunHandle,
        *,
        status: str,
        error: Optional[str] = None,
        final_text: Optional[str] = None,
        summary: Optional[Dict[str, Any]] = None,
        recovery_error: Optional[str] = None,
        finished: bool = False,
    ) -> None:
        """提交 Run 状态（P0-02：全条件 Fencing，失败显式抛出）。

        - ``RunLeaseLost``：租约易主/过期 —— 调用方必须立即停止推进
          （不写 Session、不发布终态事件、不产生新副作用）；
        - ``IllegalTransition``：终态覆盖/非法迁移 —— 不得继续写库；
        - ``PersistenceError``：数据库异常（R-12）。
        不再返回易被忽略的布尔值：所有调用点必须显式处理异常。
        """
        lease = run.execution_lease()
        if lease is None:
            raise RunLeaseLost(
                f"run {run.run_id} 无有效执行权（worker/lease_version 缺失），状态 {status} 提交被拒"
            )
        try:
            async with AsyncSessionLocal() as db:
                await RunRepository(db).update_status(
                    run.run_id,
                    status=status,
                    lease=lease,
                    error=error,
                    final_text=final_text,
                    summary=summary,
                    recovery_error=recovery_error,
                    finished=finished,
                )
                if finished and run.session_id:
                    # Run 终态与 Session 写回解耦（P1-02 C）：同一事务提交
                    # Run 终态 + SessionResultOutbox（写回由 Outbox Worker 完成，
                    # 写回失败/重试不改变 Run 终态）；同事务释放 Session 执行权。
                    await SessionOutboxRepository(db).enqueue(
                        run.run_id, run.session_id
                    )
                    await SessionRepository(db).release_execution(
                        run.session_id, run.run_id
                    )
                await db.commit()
        except (RunLeaseLost, IllegalTransition) as e:
            logger.warning(
                "run %s 状态 %s 提交被拒（fencing/状态机）：%s",
                run.run_id, status, e,
            )
            raise
        except PersistenceError:
            raise
        except Exception as e:
            raise PersistenceError(f"run {run.run_id} 状态持久化失败: {e}") from e

        if finished:
            # Run 终态富化 Langfuse Trace（单一漏斗：completed/cancelled/failed/
            # limit_exceeded 全部经此）；后台 best-effort 执行——HTTP 写入不得
            # 阻塞终态事件落库时序（否则 SSE 回放会抢先合成终止事件）
            task = asyncio.create_task(self._enrich_run_trace(
                run, status=status, error=error,
                final_text=final_text, summary=summary,
            ))
            _BG_TASKS.add(task)
            task.add_done_callback(_BG_TASKS.discard)

    async def _enrich_run_trace(
        self,
        run: RunHandle,
        *,
        status: str,
        error: Optional[str] = None,
        final_text: Optional[str] = None,
        summary: Optional[Dict[str, Any]] = None,
    ) -> None:
        """把 Run 终态（名称/用户/会话/输入/输出/状态/预算）写回 Langfuse Trace。"""
        try:
            from sqlalchemy import select
            from app.models.agent import Agent

            agent_name = ""
            task: Any = None
            async with AsyncSessionLocal() as db:
                row = (await db.execute(
                    select(AgentRunRow.input_payload, Agent.name)
                    .join(Agent, Agent.id == AgentRunRow.agent_id, isouter=True)
                    .where(AgentRunRow.id == run.run_id)
                )).first()
                if row is not None:
                    task = row[0]
                    agent_name = row[1] or ""
            route = (task or {}).get("execution_context") or {}
            document_run = route.get("task_type") in DOCUMENT_GRAPH_TYPES | {"document_analyze"}
            if document_run:
                task = {key: route.get(key) for key in (
                    "task_type", "document_profile_id", "document_profile_version",
                    "document_id", "document_version",
                )}
            await enrich_run_trace(
                run.run_id,
                name=agent_name or f"agent-{run.agent_id}",
                user_id=run.user_id,
                session_id=run.session_id,
                task=task,
                final_text=None if document_run else (
                    final_text if final_text is not None else (run.final_text or None)
                ),
                metadata={
                    "run_id": run.run_id,
                    "thread_id": run.thread_id,
                    "agent_id": run.agent_id,
                    "status": status,
                    "error": (error or "")[:500] or None,
                    "attempt": run.attempt,
                    "summary": summary or run.summary or {},
                },
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("run %s Trace 富化失败（忽略，不影响执行）: %s", run.run_id, e)
        finally:
            # 终态后立即冲刷 span 批量缓冲：Run 一结束完整链路即在 Langfuse 可见
            # （不依赖 SDK 周期导出/服务端摄取节奏）。
            # to_thread：flush 是同步阻塞 HTTP，不得卡事件循环（实测会拖垮
            # 并发会话清理时序）；本任务已在后台，阻塞线程无害。
            await asyncio.to_thread(flush_traces)
