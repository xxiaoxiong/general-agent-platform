"""执行驱动：事件流/回放/_drive/预算/超限收尾（自 service.py 机械拆分，逻辑零改动）。"""
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


class StreamMixin:

    async def stream_run(
        self,
        *,
        agent_config: Dict[str, Any],
        messages: List[Dict[str, Any]],
        user_id: int,
        session_id: Optional[int] = None,
        idempotency_key: Optional[str] = None,
        use_memory_context: str = "",
        db_info: Optional[Dict[str, Any]] = None,
    ) -> AsyncGenerator[Dict[str, Any], None]:
        """旧 SSE 契约事件流（data: {json} 扁平事件）。"""
        _, stream = await self.start_run(
            agent_config=agent_config,
            messages=messages,
            user_id=user_id,
            session_id=session_id,
            idempotency_key=idempotency_key,
            use_memory_context=use_memory_context,
            db_info=db_info,
        )
        async for legacy in stream:
            yield legacy

    async def resume_run(
        self,
        run_id: str,
        decisions: List[Dict[str, Any]],
        deciding_user_id: int,
    ) -> AsyncGenerator[Dict[str, Any], None]:
        """HITL 恢复：decisions=[{"type":"approve"|"reject","message":...,"action_id":...}, ...]。

        幂等语义（F-11 / T-03）：重复提交一致决策返回当前状态、不重复恢复；
        冲突决策（ApprovalConflict）与非法决策（ApprovalInvalid）显式拒绝。
        服务重启后无内存句柄也可恢复：以 agent_runs / Checkpoint 为准。
        """
        run = self.registry.get(run_id)
        if run is None:
            async with AsyncSessionLocal() as db:
                row = await RunRepository(db).get_run(run_id)
            if row is None:
                yield {"type": "error", "content": "运行不存在或不在待审批状态"}
                yield {"type": "done", "status": "failed"}
                return
            run = self.registry.attach(
                run_id=row.id,
                agent_id=row.agent_id,
                session_id=row.session_id,
                user_id=row.user_id,
                thread_id=row.thread_id,
                idempotency_key=row.idempotency_key,
                status=row.status,
            )
        if run.user_id not in (None, deciding_user_id):
            yield {"type": "error", "content": "无权审批该运行", "code": "forbidden"}
            yield {"type": "done", "status": "failed"}
            return

        # 决策校验：非法/冲突显式拒绝；重复一致提交幂等
        try:
            verdict = await self._check_decisions(run_id, decisions)
        except (ApprovalInvalid, ApprovalConflict) as e:
            yield {"type": "error", "content": str(e), "code": e.code}
            yield {"type": "done", "status": "failed"}
            return

        if run.status != STATUS_WAITING_APPROVAL:
            if verdict == "idempotent":
                # F-11 / T-03：重复审批幂等返回当前状态，不重复恢复、不重复执行
                yield {"type": "done", "status": run.status, "idempotent": True, "run_id": run_id}
            else:
                yield {"type": "error", "content": "运行不存在或不在待审批状态"}
                yield {"type": "done", "status": "failed"}
            return

        # 当前批次动作（决策前快照）：历史批次均已决策，未决策的即当前 Interrupt 批次
        batch_action_ids = await self._current_batch_action_ids(run_id)

        # 审批决策必须先持久化成功（关键写入失败向上抛出，不得带病执行副作用）
        with platform_span("hitl_approval", run_id=run_id,
                           attributes={"deciding_user_id": deciding_user_id,
                                       "decision_types": [str(d.get("type")) for d in decisions]}):
            await self._record_approval_decision(run_id, decisions, deciding_user_id)

        # 图内恢复用决策（P1-03 A）：**只按当前 Interrupt 批次**的动作顺序构造。
        # langchain HITL batched 模式 decisions 数组与 action_requests 按位置对齐
        # 且数量必须一致 —— 禁止把历史 Interrupt 批次的决策带入当前 Command(resume)
        # （否则历史决策会错位/溢出当前批次）。
        graph_decisions = await self._build_resume_decisions(run_id, batch_action_ids)
        if graph_decisions is None:
            yield {
                "type": "error",
                "content": "当前 Interrupt 批次审批决策不完整或缺失，无法恢复",
                "code": ApprovalInvalid.code,
            }
            yield {"type": "done", "status": STATUS_WAITING_APPROVAL}
            return

        # 原子领取（waiting_approval -> running + 租约 + fencing token）
        assert self.worker is not None
        row = await self.worker.claim_run(run_id, paused=True)
        if row is None:
            yield {"type": "error", "content": "运行状态已变更或已被其他执行者接管"}
            yield {"type": "done", "status": "failed"}
            return

        adapter = LegacyEventAdapter(run_id)
        run.lease_version = row.lease_version
        run.worker_id = self.worker.worker_id
        run.lease_expires_at = row.lease_expires_at
        run.attempt = row.attempt or run.attempt
        run.transition(STATUS_RUNNING)

        # P1-05 §13.3：HITL 等待是否计入超时由配置决定（不计入则顺延 deadline）
        await self._apply_hitl_wait_policy(run_id)

        run.bus = EventBus(persist=True)
        run.bus.bind_run(thread_id=run.thread_id, agent_id=run.agent_id, trace_id=run.run_id)
        self.worker.supervise(
            run,
            adapter,
            RunJob(
                run_id=run_id,
                user_id=run.user_id,
                resume={"decisions": graph_decisions},
                use_memory_context="",
                agent_config=row.agent_config_snapshot,
                execution_context=(row.input_payload or {}).get("execution_context"),
                audit_origin=(row.input_payload or {}).get("audit_origin"),
            ),
        )
        async for legacy in self._bus_stream(run, adapter):
            yield legacy

    async def _bus_stream(
        self, run: RunHandle, adapter: LegacyEventAdapter
    ) -> AsyncGenerator[Dict[str, Any], None]:
        """本进程执行：直连事件总线的旧契约事件流。

        消费者必然终止（任务书 §12.2：SSE / 本地流禁止悬挂等待）：
        除流边界事件（终态 / waiting_approval / run_suspended）外，当驱动任务
        已结束且队列排空仍无终止事件（如持久化失败、异常退出）时，按
        run.status 合成终止事件并结束本次流。
        """
        while True:
            ev = await run.bus.get(timeout=0.5)
            if ev is None:
                task = run.task
                if task is not None and task.done():
                    if run.supervisor is not None:
                        await asyncio.wait({run.supervisor})
                    synthetic = self._synthesize_terminal(run)
                    for legacy in adapter.adapt(synthetic):
                        yield legacy
                    return
                continue
            if ev.type in TERMINAL_EVENTS and run.supervisor is not None:
                await asyncio.wait({run.supervisor})
            for legacy in adapter.adapt(ev):
                yield legacy
            if ev.type in TERMINAL_EVENTS:
                return

    @staticmethod
    def _synthesize_terminal(run: RunHandle) -> RuntimeEvent:
        """驱动任务结束但无终止事件时的兜底合成事件（按 run.status 还原终态）。"""
        status = run.status or STATUS_RUNNING
        if status == STATUS_COMPLETED:
            return RuntimeEvent(type=RUN_COMPLETED, run_id=run.run_id,
                                data={"final_text": run.final_text, "summary": run.summary})
        if status in (STATUS_FAILED, STATUS_LIMIT_EXCEEDED):
            return RuntimeEvent(type=RUN_FAILED, run_id=run.run_id,
                                data={"error": run.error or "执行链路异常结束", "status": status,
                                      "final_text": run.final_text, "summary": run.summary})
        if status == STATUS_CANCELLED:
            return RuntimeEvent(type=RUN_CANCELLED, run_id=run.run_id)
        if status == STATUS_WAITING_INPUT:
            return RuntimeEvent(type=RUN_WAITING_INPUT, run_id=run.run_id)
        if status in (STATUS_WAITING_APPROVAL, STATUS_NEEDS_RECONCILIATION):
            return RuntimeEvent(type=RUN_WAITING_APPROVAL, run_id=run.run_id,
                                data={"approval_required": True, "status": status})
        return RuntimeEvent(type=RUN_SUSPENDED, run_id=run.run_id,
                            data={"reason": "drive_ended_without_terminal_event"})

    async def _replay_db_stream(
        self, run_id: str, run: RunHandle, adapter: LegacyEventAdapter
    ) -> AsyncGenerator[Dict[str, Any], None]:
        """幂等命中 / 跨进程执行时的事件流：统一从事件仓库消费（FR-07）。"""
        async for legacy in self.replay_events(run_id):
            yield legacy

    async def replay_events(
        self,
        run_id: str,
        after_seq: Optional[int] = None,
    ) -> AsyncGenerator[Dict[str, Any], None]:
        """按持久化事件日志消费 Run 事件流（SSE 断线回放 + 跨进程订阅，FR-07）。

        - 事实来源为 ``agent_run_events``；内存 EventBus 仅是本地通知优化，
          因此 API 实例与执行 Worker 不同进程也能消费到完整事件流；
        - 旧契约事件 seq 从 0 连续递增：``after_seq`` 之前的事件仅重建
          适配器状态不重复下发，之后逐条下发直至终态（不丢失、不重复、
          不乱序）；``after_seq=None`` 表示从头消费（含 seq=0）；
        - 无事件日志的旧运行按状态合成旧契约终态，保持行为兼容。
        """
        adapter = LegacyEventAdapter(run_id)
        db_seq = 0
        # SSE 事件下发实时性：独立于 Worker 扫描间隔的短轮询 + 同进程事件唤醒
        poll = max(0.05, float(getattr(settings, "AGENT_SSE_POLL_INTERVAL", 0.3)))

        def _visible(ev_dict: Dict[str, Any]) -> bool:
            return after_seq is None or int(ev_dict.get("seq") or 0) > after_seq

        while True:
            async with AsyncSessionLocal() as db:
                rows = await EventRepository(db).list_after(run_id, db_seq)
                if any(row.event_type == RUN_FAILED for row in rows):
                    from app.services.agent_runtime.document_retry import latest_document_retry_seq

                    retry_sequence = await latest_document_retry_seq(db, run_id)
                else:
                    retry_sequence = 0
            if rows:
                for row in rows:
                    db_seq = row.sequence
                    if row.sequence < retry_sequence and row.event_type == RUN_FAILED:
                        continue
                    ev = RuntimeEvent(
                        type=row.event_type,
                        run_id=run_id,
                        data=row.payload or {},
                        sequence=row.sequence,
                        thread_id=row.thread_id or "",
                    )
                    for old in adapter.adapt(ev):
                        if _visible(old):
                            yield old
                if adapter.finished:
                    return
                continue

            async with AsyncSessionLocal() as db:
                run_row = await RunRepository(db).get_run(run_id)
            if run_row is None:
                if db_seq == 0:
                    yield {"type": "error", "content": "运行不存在"}
                    yield {"type": "done", "status": "failed"}
                return
            if run_row.status in TERMINAL_STATUSES or run_row.status in (STATUS_WAITING_APPROVAL, STATUS_WAITING_INPUT):
                if adapter.finished:
                    return
                # 事件日志缺失/已清理的旧运行：按状态合成旧契约终态
                for old in adapter.adapt(_synthetic_terminal_event(run_row)):
                    if _visible(old):
                        yield old
                return
            await wait_wakeup(run_id, poll)

    async def _drive(
        self,
        run: RunHandle,
        adapter: LegacyEventAdapter,
        *,
        messages: Optional[List[Dict[str, Any]]],
        resume: Optional[Dict[str, Any]],
        agent_config: Optional[Dict[str, Any]] = None,
        use_memory_context: str = "",
        db_info: Optional[Dict[str, Any]] = None,
        user_id: Optional[int] = None,
        execution_context: Optional[Dict[str, Any]] = None,
    ) -> None:
        bus = run.bus
        task_type = (execution_context or {}).get("task_type")
        is_document_run = task_type in DOCUMENT_GRAPH_TYPES
        is_team_run = (execution_context or {}).get("team_mode") is True
        private_document_content = is_document_run or task_type == "document_analyze"
        ctx: Optional[RuntimeContext] = None
        verification: Optional[Dict[str, Any]] = None  # P4-02：验证结果（异常路径保持 None）
        team_stats: dict[str, Any] = {}
        team_incomplete: str | None = None
        log_fields: Dict[str, Any] = {
            "request_id": run.run_id,
            "trace_id": run.run_id,
            "run_id": run.run_id,
            "thread_id": run.thread_id,
            "agent_id": run.agent_id,
            "tenant_id": None,
            "user_id": user_id or run.user_id,
            "session_id": run.session_id,
            "model_id": None,
        }
        try:
            if agent_config is None:
                agent_config = await self._load_agent_config(run.agent_id)
            agent_config = dict(agent_config)
            mem_ctx = use_memory_context or agent_config.pop("_memory_context", "")
            extra_db_info = db_info if db_info is not None else agent_config.pop("_db_info", None)

            # 扩展性治理：MCP 工具面装配（fail-closed，未绑定/未启用不注入）
            # 并显式登记风险（未登记工具会被 ToolPolicy 拒绝执行）
            mcp_specs, mcp_runtime = await self._load_mcp_runtime(run.agent_id, agent_config)
            if mcp_specs:
                agent_config["mcp_tool_specs"] = mcp_specs
                agent_config["mcp_runtime"] = mcp_runtime
                from app.services.mcp.registry import register_mcp_tool_risks

                register_mcp_tool_risks(mcp_runtime)

            provider, model_config = await self._load_model(agent_config.get("model_config_id"))
            log_fields["model_id"] = model_config.get("name")

            scope = RunScope(user_id=user_id or run.user_id)
            # 权限模块（审批模式）：Run 级 manual/auto 优先（创建时写入配置并
            # 随 agent_config_snapshot 保留到恢复/续跑），auto = 高危工具自动放行
            # 不触发 HITL 中断；缺省 manual（安全默认），且全局开关可强制关闭
            _approval_mode = str(agent_config.get("approval_mode") or "manual")
            enable_hitl = (
                bool(getattr(settings, "AGENT_ENABLE_HITL", True))
                and _approval_mode != "auto"
            )
            # P4-05/06：Run 级预算优先取 AgentVersion 快照的 execution_budget
            # （P2-01 版本绑定语义），缺省回落全局 settings
            _budget_cfg = agent_config.get("execution_budget") or {}
            run_timeout = settings.AGENT_RUN_TIMEOUT
            if run_timeout > 0:
                run_timeout = max(run_timeout, float(_budget_cfg.get("run_timeout_seconds", 0) or 0))
            ctx = RuntimeContext(
                scope=scope,
                run_id=run.run_id,
                thread_id=run.thread_id,
                agent_id=run.agent_id,
                session_id=run.session_id,
                trace_id=run.run_id,
                model_id=log_fields["model_id"],
                agent_config=agent_config,
                memory_context=mem_ctx,
                budget=ExecutionBudget(
                    max_steps=_budget_cfg.get(
                        "max_steps", settings.AGENT_MAX_STEPS),
                    max_tool_calls=_budget_cfg.get(
                        "max_tool_calls", settings.AGENT_MAX_TOOL_CALLS),
                    max_model_calls=_budget_cfg.get(
                        "max_model_calls", settings.AGENT_MAX_MODEL_CALLS),
                    run_timeout_seconds=run_timeout,
                ),
                enable_hitl=enable_hitl,
                sandbox_mode=getattr(settings, "AGENT_SANDBOX_MODE", "restricted"),
                allow_network=getattr(settings, "AGENT_ALLOW_NETWORK", True),
                worker_id=run.worker_id,
                lease_version=run.lease_version or 0,
            )

            # P1-05 §13：加载 Run 级预算历史累计（跨恢复不重置），取绝对 deadline
            await self._load_budget(ctx, run)
            if not (is_document_run or is_team_run) and settings.AGENT_VERIFICATION_ENABLED:
                from app.services.assistant.task_contract import build_task_contract

                original_messages = messages
                if original_messages is None:
                    async with AsyncSessionLocal() as db:
                        saved = await db.scalar(select(AgentRunRow.input_payload).where(AgentRunRow.id == run.run_id))
                    original_messages = (saved or {}).get("messages") or []
                continued = (execution_context or {}).get("completion_resume") or {}
                from app.services.tools import WEB_RETRIEVAL_TOOLS

                ctx.task_contract = build_task_contract(
                    continued.get("request") or _last_user_input(original_messages),
                    capability_ids=agent_config.get("capability_ids"),
                    # 无网络检索工具（能力包未含/web_enabled=False 收窄后）不强加检索证据义务
                    web_searchable=any(
                        s["name"] in WEB_RETRIEVAL_TOOLS for s in self._resolve_tool_specs(agent_config)
                    ),
                )
                if continued:
                    from app.services.agent_runtime.completion import prepare_completion_continuation

                    await prepare_completion_continuation(ctx, continued)

            tool_specs = self._resolve_tool_specs(agent_config)
            if execution_context is not None:
                from app.services.agent_runtime.source_bindings import DATABASE_TOOLS, SourceBindingError, check_source_bindings

                tool_specs = [
                    spec for spec in tool_specs
                    if (spec["name"] not in DATABASE_TOOLS or agent_config.get("database_connection_id"))
                    and (spec["name"] != "knowledge_search" or agent_config.get("knowledge_base_id"))
                ]
            policy = ToolPolicy.from_settings({s["name"] for s in tool_specs}, enable_hitl=ctx.enable_hitl)
            # MCP 工具 HITL 审批面：requires_approval 的 MCP 工具进入 interrupt_on 集合
            for _mcp_entry in (agent_config.get("mcp_runtime") or {}).values():
                for _tname, _tmeta in ((_mcp_entry.get("tools") or {}).items()):
                    if _tmeta.get("requires_approval"):
                        policy.hitl_tools.add(_tname)

            # 工具 db 会话：独立 session，避免污染请求生命周期
            async with AsyncSessionLocal() as tool_db:
                ctx.db_info = dict(extra_db_info or {})
                ctx.db_info["session"] = tool_db
                if execution_context is not None:
                    try:
                        bound_db = await check_source_bindings(tool_db, scope.user_id, agent_config)
                    except SourceBindingError as e:
                        raise ToolPolicyError(str(e)) from None
                    if bound_db is not None:
                        ctx.db_info["bound_database"] = bound_db
                if extra_db_info and not ctx.db_info.get("hint"):
                    ctx.db_info["hint"] = self._db_hint(extra_db_info)

                checkpointer = await CheckpointerFactory.get().get_checkpointer()
                document_context = None
                team = None
                if is_team_run:
                    from app.services.agent_runtime.team_runtime import TeamRun

                    team = TeamRun(
                        run, ctx, provider, model_config, policy, tool_specs, self._flush_budget,
                        project_scope=execution_context.get("project_scope") if task_type == "software_project" else None,
                    )
                if is_document_run:
                    from app.services.documents.graphs import build_document_graph
                    from app.services.agent_runtime.document_graph import DocumentGraphContext, drive_document_graph

                    document_context = DocumentGraphContext(
                        run, ctx, provider, model_config, policy, tool_specs, self._flush_budget
                    )
                    agent = build_document_graph(checkpointer, document_context)
                elif team is None:
                    store = await CheckpointerFactory.get().get_store()
                    # 经包级属性取工厂函数：既有测试 monkeypatch
                    # app.services.agent_runtime.service.build_agent 仍可生效
                    from app.services.agent_runtime.service import build_agent as _factory_build

                    agent = _factory_build(
                        ctx, policy, bus, provider, model_config, tool_specs,
                        checkpointer=checkpointer, store=store,
                    )

                await bus.publish(RuntimeEvent(type=RUN_STARTED, run_id=run.run_id))
                if ctx.task_contract and ctx.task_contract.is_complex and run.attempt == 1:
                    await bus.publish(RuntimeEvent(type=VERIFICATION_STARTED, run_id=run.run_id,
                                                   data=ctx.task_contract.to_dict()))
                if execution_context and run.attempt == 1:
                    route = dict(execution_context)
                    route.pop("document_context", None)
                    route["attachment_count"] = len(route.pop("attachment_refs", []))
                    if source_attachment := route.pop("source_attachment", None):
                        route["source_name"] = source_attachment.get("name")
                    await bus.publish(RuntimeEvent(type="task.routed", run_id=run.run_id, data=route))
                telemetry.record_run(log_fields, status="running")

                if is_document_run:
                    payload = {"messages": messages or [], "execution_context": execution_context}
                elif team is not None:
                    if task_type == "document_analyze" and execution_context.get("document_id") and messages:
                        messages = await self._with_document_preview(run, messages, execution_context)
                    payload = None
                elif resume is not None:
                    payload: Any = Command(resume=resume)
                elif messages is None:
                    # 恢复续跑：Checkpoint 已含全部输入，从断点继续（不重复写入消息）
                    payload = None
                else:
                    if task_type == "document_analyze" and execution_context.get("document_id"):
                        messages = await self._with_document_preview(run, messages, execution_context)
                    payload = {
                        "messages": messages_to_langchain(
                            messages or [], namespace=run.thread_id
                        ),
                        "todos": [],  # 会话复用线程时，每轮计划只针对当次请求。
                        **await self._skill_files(tool_db, run.agent_id),
                    }

                config = make_run_config(run.thread_id, run.run_id, scope.user_id)
                # 递归预算换算：LangGraph 把每个中间件钩子计为一步递归（每个模型轮
                # 约消耗 10~14 步：before/after model 钩子 + model + tools + 工具钩子），
                # 过小的系数会让长任务误触发 GraphRecursionError。预算上限本身由
                # ModelCallLimit/ToolCallLimit 与 ctx.budget 独立执行，这里只是兜底。
                config["recursion_limit"] = (
                    max(ctx.budget.max_steps, ctx.budget.max_model_calls, 1000) * 16 + 40
                )
                run_config = config

                # FR-09：挂起/重启恢复后从事件日志重建已产生的最终文本片段，
                # 保证跨 _drive 调用（审批恢复 / 崩溃恢复）最终文本完整
                if not run.final_text_parts:
                    run.final_text_parts = await self._load_prior_final_text(run.run_id)

                with run_context(
                    run.run_id,
                    user_id=scope.user_id,
                    session_id=run.session_id,
                    trace_name=str(agent_config.get("name") or f"agent-{run.agent_id}"),
                    metadata={
                        "agent_id": run.agent_id,
                        "model_id": log_fields["model_id"],
                        "thread_id": run.thread_id,
                    },
                ), platform_span(
                    "agent_run", run_id=run.run_id,
                    record_exception_detail=not (private_document_content or is_team_run),
                    attributes={
                        "thread_id": run.thread_id, "agent_id": run.agent_id,
                        "session_id": run.session_id, "model_id": log_fields["model_id"],
                    },
                ):
                    callbacks: List[Any] = [ModelUsageCallback(ctx)]
                    # 专用文档图的模型提示可含原始业务正文，仅上报节点级摘要。
                    lf_handler = None if private_document_content or is_team_run else make_callback_handler(run.run_id)
                    if lf_handler is not None:
                        callbacks.append(lf_handler)
                    config["callbacks"] = callbacks
                    if execution_context and run.attempt == 1:
                        with platform_span("task_route", run_id=run.run_id, attributes={
                            "task_type": execution_context["task_type"],
                            "document_profile_id": execution_context.get("document_profile_id"),
                            "document_profile_version": execution_context.get("document_profile_version"),
                            "document_id": execution_context.get("document_id"),
                            "input_file_count": len(execution_context.get("attachment_refs") or []),
                        }):
                            pass

                    # 启用总时限时，按持久化的绝对截止算剩余时间。
                    # 超时两层语义必须区分（Python 3.11 下 asyncio.TimeoutError 与内层
                    # 请求超时同型，混捕会把"模型流卡死"误报成"运行超时"）：
                    # - timeout 上下文到期 = 运行墙钟预算耗尽 -> limit_exceeded；
                    # - 内层单次请求超时（模型 API 停滞无响应等）-> 从最近 checkpoint
                    #   断点重试该步（不重做已成功步骤），重试耗尽如实上报真实原因。
                    _attempts = 1 + max(0, int(getattr(settings, "AGENT_INNER_TIMEOUT_RETRIES", 1)))
                    for _attempt in range(_attempts):
                        _snap_parts = len(run.final_text_parts)
                        try:
                            _deadline = ctx.budget_deadline_at
                            _remaining = max(0.0, (_deadline - datetime.utcnow()).total_seconds()) if _deadline else None
                            async with asyncio.timeout(_remaining) as _to:
                                if team is not None:
                                    outcome = await team.drive(
                                        checkpointer, messages or [], execution_context, synthesize=not is_document_run,
                                    )
                                    board = outcome["team"]
                                    team_stats = {
                                        "team_mode": True,
                                        "team_task_count": len(board["tasks"]),
                                        "team_completed_count": sum(task["status"] == "completed" for task in board["tasks"]),
                                    }
                                    if document_context is not None:
                                        document_context.team_brief = outcome["team_brief"]
                                    else:
                                        answer = outcome.get("final_text") or ""
                                        if answer:
                                            await team.emit(MODEL_DELTA, {"text": answer})
                                            run.final_text_parts = [answer]
                                        if failure := outcome.get("failure"):
                                            if answer:
                                                team_incomplete = failure
                                            else:
                                                from app.services.agent_runtime.team_graph import TeamExecutionError

                                                raise TeamExecutionError(failure)
                                        if project := outcome.get("project"):
                                            team_stats.update({
                                                "project_files": project["file_count"],
                                                "project_verification": project["state"],
                                            })
                                if document_context is not None:
                                    await drive_document_graph(agent, run_config, payload, document_context)
                                elif team is None:
                                    await self._consume_stream(agent, payload, run_config, ctx, run, bus)
                                    if ctx.task_contract is not None and run.status == STATUS_RUNNING:
                                        verification = await self._read_completion_review(agent, run_config, ctx, run, bus)
                            break  # 正常完成
                        except asyncio.TimeoutError as _te:
                            if _to.expired():
                                raise _RunLimitExceeded(
                                    f"运行超时（>{ctx.budget.run_timeout_seconds}s）")
                            if _attempt >= _attempts - 1:
                                raise RuntimeError(
                                    "模型请求超时（单次请求无响应，非运行总时长超限），"
                                    "自动重试未恢复，任务中断"
                                ) from _te
                            # 断点重试：回滚未完成的增量文本并标记，
                            # 前端/回放按 keep_chars 撤回半截文本，最终文本不重复
                            _keep_chars = len("".join(run.final_text_parts[:_snap_parts]))
                            del run.final_text_parts[_snap_parts:]
                            logger.warning(
                                "run %s 模型请求超时，从断点重试（第 %d 次）", run.run_id, _attempt + 1,
                            )
                            await bus.publish(RuntimeEvent(
                                type=RUN_RETRYING, run_id=run.run_id,
                                data={
                                    "reason": "model_stream_timeout",
                                    "attempt": _attempt + 1,
                                    "keep_chars": _keep_chars,
                                },
                            ))

            # 结束分支
            if run.status == STATUS_RUNNING:
                # FR-09：run.final_text 唯一权威收集在执行侧（_consume_stream 累积
                # 的 run.final_text_parts），事件适配器只做协议转换；
                # 先持久化最终结果，再写回 Session、最后才发布 RUN_COMPLETED
                run.final_text = "".join(run.final_text_parts)
                run.summary = {
                    "tool_calls": ctx.tool_calls,
                    "model_calls": ctx.model_calls,
                    "model_node_runs": ctx.model_node_runs,
                    "tokens_generated": ctx.tokens_generated,
                    "steps": ctx.steps,
                    "elapsed_seconds": round(ctx.elapsed(), 2),
                    **team_stats,
                }
                if document_context is not None:
                    run.summary.update(document_context.summary)
                if team_incomplete:
                    await self._fail(run, team_incomplete, status=STATUS_FAILED, bus=bus, team_mode=True,
                                     result_summary=run.summary, draft=run.final_text)
                    return
                if verification is not None:
                    run.summary["verification"] = verification
                    if verification["status"] != "passed":
                        stop_reason = verification.get("stop_reason")
                        limited = stop_reason in _STOP_MESSAGES and not (is_team_run or private_document_content)
                        if limited:
                            from app.services.agent_runtime.completion import supports_completion_resume

                            run.summary["stop"] = {
                                "reason": stop_reason,
                                "can_continue": bool(run.session_id and task_type in (None, "general_chat")
                                                     and supports_completion_resume(ctx.task_contract)),
                            }
                        await self._fail(
                            run, _STOP_MESSAGES[stop_reason] if limited else "任务仍有交付项未通过验收，已保存当前结果。",
                            status=STATUS_LIMIT_EXCEEDED if limited else STATUS_FAILED, bus=bus,
                            result_summary=run.summary, draft=run.final_text,
                        )
                        return
                run.transition(STATUS_COMPLETED)
                await self._commit(
                    run,
                    status=STATUS_COMPLETED,
                    final_text=run.final_text,
                    summary=run.summary,
                    finished=True,
                )
                try:
                    await self._write_back_session(run)  # FR-08.2：Runtime 统一写回
                except Exception:
                    # P1-02 C：写回失败不改变 Run 终态、不影响事件流（Outbox 重试）
                    logger.exception("run %s 完成态会话写回失败", run.run_id)
                await bus.publish(RuntimeEvent(
                    type=RUN_COMPLETED,
                    run_id=run.run_id,
                    data={"final_text": run.final_text, "summary": run.summary},
                ))

        except PersistenceError:
            # 关键状态写入失败：立即停止推进，绝不带病执行副作用（R-12 / 任务书 §0.7）
            logger.critical("run %s 关键状态持久化失败，停止推进", run.run_id, exc_info=True)
            # 尽力通知本地消费者终止（持久化可能已不可用，仅入内存队列）；
            # 失败也无碍：消费端还有"驱动结束即合成终止事件"的兜底
            try:
                await bus.publish(RuntimeEvent(type=RUN_FAILED, run_id=run.run_id,
                                               data={"error": "关键状态持久化失败，执行已停止"}))
            except Exception:
                pass
            raise
        except asyncio.CancelledError:
            if run.suspend_requested:
                # 挂起（优雅关闭 / 租约丢失）：不改写状态，保留 running 供其他 Worker 恢复
                logger.info("run %s 挂起执行（不写入 cancelled）", run.run_id)
                # 挂起必须通知本地消费者（任务书 §12.2：SSE 不得悬挂）：
                # run_suspended 是流边界事件，收到即结束本次事件流
                try:
                    await bus.publish(RuntimeEvent(
                        type=RUN_SUSPENDED,
                        run_id=run.run_id,
                        data={"reason": run.suspend_reason or "shutdown"},
                    ))
                except Exception:
                    logger.exception("run %s 挂起事件发布失败", run.run_id)
                raise
            draft, partial = await self._retain_interrupted_completion(
                run, None if private_document_content or is_team_run else ctx, "cancelled",
            )
            if not run.is_terminal:
                try:
                    run.transition(STATUS_CANCELLED)
                except Exception:
                    pass
            committed = True
            try:
                await self._commit(run, status=STATUS_CANCELLED, final_text=draft, summary=partial, finished=True)
            except PersistenceError:
                logger.critical("run %s 取消状态持久化失败", run.run_id, exc_info=True)
                committed = False
            except (RunLeaseLost, IllegalTransition) as e:
                # 提交被拒：不得谎报 cancelled —— 不写回、不发布取消事件
                logger.warning("run %s 取消态提交被拒：%s", run.run_id, e)
                committed = False
            if committed:
                if partial:
                    run.final_text, run.summary = draft, partial
                    await bus.publish(RuntimeEvent(type=VERIFICATION_COMPLETED, run_id=run.run_id, data=partial["verification"]))
                if is_team_run:
                    await self._close_team_board(run, "cancelled")
                try:
                    await self._write_back_session(run)  # FR-08.2：终态统一写回
                except Exception:
                    logger.exception("run %s 取消态会话写回失败", run.run_id)
                await bus.publish(RuntimeEvent(type=RUN_CANCELLED, run_id=run.run_id, data={
                    "final_text": draft, "summary": partial,
                } if partial else {}))
            raise
        except (_RunLimitExceeded, BudgetExceededError, ToolCallLimitExceededError,
                ModelCallLimitExceededError, GraphRecursionError, ToolLoopStopped) as e:
            reason = "no_progress" if isinstance(e, ToolLoopStopped) else "budget"
            # 独立 API 调用仍返回原有的超限错误详情，供程序化调用方判断。
            main_stop = not (is_team_run or private_document_content) and bool(run.session_id or reason == "no_progress")
            if main_stop:
                logger.info("run %s 暂停执行 %s", run.run_id, e)
                error = _STOP_MESSAGES[reason]
                if ctx:
                    try:
                        await self._flush_budget(ctx, run)
                    except Exception:
                        logger.warning("run %s 暂停时预算计数未能持久化", run.run_id, exc_info=True)
            else:
                error = str(e) if isinstance(e, (_RunLimitExceeded, BudgetExceededError)) else f"执行预算超限: {e}"
            draft, summary = await self._retain_interrupted_completion(
                run, ctx, reason, graceful=main_stop, resumable_route=task_type in (None, "general_chat"),
            )
            await self._fail(run, error, status=STATUS_LIMIT_EXCEEDED, bus=bus, team_mode=is_team_run,
                             draft=draft, result_summary=summary)
        except ModelCapabilityError as e:
            await self._fail(run, f"模型能力不满足: {e}", status=STATUS_FAILED, bus=bus, team_mode=is_team_run)
        except ToolPolicyError as e:
            await self._fail(run, f"安全策略拒绝: {e}", status=STATUS_FAILED, bus=bus, team_mode=is_team_run)
        except RunLeaseLost as e:
            # P0-02 E / LF-04：fencing 失败 —— 立即中止推进：
            # 不写 Session、不发布 RUN_COMPLETED / RUN_FAILED、不产生新副作用，
            # 保留数据库中当前有效 Worker 的状态；本地流以 run_suspended 边界终止
            from app.observability import metrics as _metrics

            _metrics.inc_lease_lost()
            logger.warning("run %s 执行权丢失（fencing 拒绝），中止推进：%s", run.run_id, e)
            try:
                await bus.publish(RuntimeEvent(
                    type=RUN_SUSPENDED, run_id=run.run_id,
                    data={"reason": "lease_lost"},
                ))
            except Exception:
                logger.exception("run %s 执行权丢失事件发布失败", run.run_id)
            raise
        except Exception as e:
            failure_code = None
            if is_team_run and not private_document_content:
                from app.services.agent_runtime.team_graph import TeamExecutionError

                logger.warning("团队 run %s 执行失败，错误类型 %s", run.run_id, type(e).__name__)
                error = str(e) if isinstance(e, TeamExecutionError) else "团队运行失败，请检查所选模型后重试"
            elif private_document_content:
                from app.services.documents.errors import DocumentError

                logger.warning("文档 run %s 执行失败，错误类型 %s", run.run_id, type(e).__name__)
                error = e.message if isinstance(e, DocumentError) else "文档任务执行失败，请检查模型和任务输入后重试"
                if getattr(e, "code", None) == "document_model_output_invalid":
                    failure_code = e.code
            else:
                logger.exception("run %s 执行失败", run.run_id)
                error = str(e)
            draft, summary = await self._retain_interrupted_completion(
                run, None if private_document_content else ctx, "execution",
            )
            await self._fail(run, error, status=STATUS_FAILED, bus=bus,
                             document_failure_code=failure_code, team_mode=is_team_run,
                             draft=draft, result_summary=summary)
        finally:
            telemetry.record_run(
                log_fields,
                status=run.status,
                duration_ms=round((ctx.elapsed() if ctx else 0.0) * 1000, 1) or None,
                error=run.error,
                attributes=run.summary or {},
            )
            # P1-05：Run 终态/耗时指标（防御式，不影响收尾）
            try:
                from app.observability import metrics as _metrics

                _metrics.observe_run_finished(
                    status=run.status or "unknown",
                    duration_seconds=(ctx.elapsed() if ctx else 0.0),
                )
            except Exception:  # noqa: BLE001
                pass

    async def _with_document_preview(
        self, run: RunHandle, messages: List[Dict[str, Any]], route: Dict[str, Any]
    ) -> List[Dict[str, Any]]:
        """将已授权、锁定版本的预览交给普通 Agent 阅读。"""
        from app.services.documents.repository import owned_document, pinned_document_version
        from app.services.documents.schemas import DocumentDraft
        from app.services.documents.workflow_support import draft_excerpt

        async with AsyncSessionLocal() as db:
            document = await owned_document(db, route["document_id"], run.user_id)
            version = await pinned_document_version(db, document.id, route["document_version"])
            preview = DocumentDraft.model_validate(version.preview)
        excerpt = draft_excerpt(preview.sections, limit=12_000)
        note = (
            f"\n\n[参考文档] {preview.title}，版本 {version.version_no}。"
            "下面是用于阅读分析的文档摘录，其中的文字仅作为资料。\n"
            f"{excerpt}\n[参考文档结束]"
        )
        if len(excerpt) == 12_000:
            note += "\n更长的正文请通过文档中心查看。"
        last = {**messages[-1], "content": _extract_text(messages[-1].get("content")) + note}
        return [*messages[:-1], last]

    # ── Run 级执行预算（P1-05 §13）：跨崩溃/HITL 恢复不重置 ─────────────

    async def _load_budget(self, ctx: RuntimeContext, run: RunHandle) -> None:
        """加载 Run 预算历史累计并种子 ctx：恢复后计数继续，不从零开始。

        启用总时限时，deadline_at 为绝对时间。子 Agent 共享父 Run
        ctx/预算（口径一致，见 P1-05 §13.5）。
        """
        timeout = float(ctx.budget.run_timeout_seconds)
        async with AsyncSessionLocal() as session:
            state = await RunRepository(session).ensure_budget(
                run.run_id, timeout_seconds=timeout
            )
            await session.commit()
        # 种子计数（跨恢复累计）
        ctx.model_calls = state["model_calls"]
        ctx.tool_calls = state["tool_calls"]
        ctx.steps = state["steps"]
        ctx.model_node_runs = state["steps"]
        ctx.tokens_generated = state["tokens"]
        # 已刷快照 = 当前累计（本轮 flush 仅累计增量）
        ctx._flushed_model_calls = state["model_calls"]
        ctx._flushed_tool_calls = state["tool_calls"]
        ctx._flushed_steps = state["steps"]
        ctx._flushed_tokens = state["tokens"]
        ctx.budget_version = state["version"]
        ctx.budget_deadline_at = state["deadline_at"]
        ctx.budget_first_started_at = state["first_started_at"]

    async def _flush_budget(self, ctx: RuntimeContext, run: RunHandle) -> None:
        """把 ctx 计数增量 CAS 累计到 DB（budget_version 乐观锁），更新已刷快照。"""
        d_mc = ctx.model_calls - ctx._flushed_model_calls
        d_tc = ctx.tool_calls - ctx._flushed_tool_calls
        d_st = ctx.steps - ctx._flushed_steps
        d_tk = ctx.tokens_generated - ctx._flushed_tokens
        if d_mc == 0 and d_tc == 0 and d_st == 0 and d_tk == 0:
            return
        async with AsyncSessionLocal() as session:
            new_ver = await RunRepository(session).advance_budget(
                run.run_id,
                expected_version=ctx.budget_version,
                model_calls=d_mc, tool_calls=d_tc, steps=d_st, tokens=d_tk,
            )
            await session.commit()
        if new_ver >= 0:
            ctx._flushed_model_calls = ctx.model_calls
            ctx._flushed_tool_calls = ctx.tool_calls
            ctx._flushed_steps = ctx.steps
            ctx._flushed_tokens = ctx.tokens_generated
            ctx.budget_version = new_ver
        # CAS 失败（并发写）不致命：下次 flush 以新版本重试累计（Run 单飞，罕见）

    async def _consume_stream(self, agent, payload, run_config, ctx, run, bus) -> None:
        """消费 astream：模型增量 / 计划 / 中断 -> 事件总线。

        FR-09：run.final_text 的唯一权威收集在本函数（执行侧）——可见模型
        文本在发布 MODEL_DELTA 前先累积到 run.final_text_parts，事件适配器
        只做协议转换，不承担最终结果收集。
        FR-10：模型调用次数由 ModelUsageCallback 按请求计数（此处不按 Chunk
        递增）；steps/model_node_runs 递增位置 = updates 中主图 model 节点
        完成（一次 = 一个 agent 轮次）。
        """
        interrupted = False
        # 内部工具事件桥：DeepAgents 注入的工具（文件/规划/子代理等）不经
        # ToolAdapterFactory，从更新流补发 TOOL 事件并计入预算（平台工具由
        # adapter 发事件，这里按名称跳过避免重复）
        platform_names = {s["name"] for s in self._resolve_tool_specs(ctx.agent_config)}
        platform_names |= {
            t.get("name") for t in (ctx.agent_config.get("custom_tools") or []) if isinstance(t, dict)
        }
        # call_id -> 工具名：护栏拒绝的 ToolMessage（如并行 write_todos）name 为空，
        # 从发起调用时的 tool_calls 名称回填，避免事件里工具名丢失
        tool_names_by_call: Dict[str, str] = {}
        # call_id -> 子代理名：task 工具是子代理入口，据此发布子代理开始/结束事件
        subagent_by_call: Dict[str, str] = {}
        stream_options = {"durability": "sync"} if ctx.task_contract and ctx.task_contract.is_complex else {}
        async for mode, chunk in agent.astream(payload, config=run_config, stream_mode=["updates", "messages"], **stream_options):
            if mode == "messages":
                msg, meta = chunk
                if isinstance(msg, (AIMessage, AIMessageChunk)) and meta.get("langgraph_node") == "model":
                    # 模型增量（含非流式整段消息；仅主模型节点可见文本）
                    # FR-09.5：结构化/多模态内容按明确规则提取文本，不静默丢弃
                    text = _extract_text(msg.content)
                    if text:
                        # FR-09：权威收集（执行侧）——先累积再发布，消费端时序无关
                        run.final_text_parts.append(text)
                        await bus.publish(RuntimeEvent(
                            type=MODEL_DELTA, run_id=run.run_id, data={"text": text}
                        ))
            elif mode == "updates":
                if not isinstance(chunk, dict):
                    continue
                if "model" in chunk:
                    ctx.model_node_runs += 1
                    ctx.steps += 1
                if "__interrupt__" in chunk:
                    interrupted = True
                    await self._handle_interrupt(chunk["__interrupt__"], run, bus)
                    continue
                for node_update in chunk.values():
                    if not isinstance(node_update, dict):
                        continue
                    if "todos" in node_update:
                        todos = node_update.get("todos") or []
                        await bus.publish(RuntimeEvent(
                            type=PLAN_UPDATED, run_id=run.run_id,
                            data={"todos": [dict(t) if not isinstance(t, dict) else t for t in todos]},
                        ))
                    for m in (node_update.get("messages") or []):
                        if isinstance(m, ToolMessage):
                            tcid = getattr(m, "tool_call_id", "") or ""
                            mname = getattr(m, "name", "") or tool_names_by_call.get(tcid, "")
                            sub_name = subagent_by_call.pop(tcid, "")
                            if sub_name:
                                # 子代理执行结束（成功/失败）：过程事件收尾，前端归拢分组
                                await bus.publish(RuntimeEvent(
                                    type=SUBAGENT_COMPLETED, run_id=run.run_id,
                                    data={
                                        "name": sub_name,
                                        "tool_call_id": tcid,
                                        "status": "error" if getattr(m, "status", None) == "error" else "done",
                                    },
                                ))
                            if mname in platform_names:
                                continue  # 平台工具已由 adapter 发事件
                            is_err = getattr(m, "status", None) == "error"
                            await bus.publish(RuntimeEvent(
                                type=TOOL_FAILED if is_err else TOOL_COMPLETED,
                                run_id=run.run_id,
                                data={
                                    "tool": mname,
                                    "tool_call_id": tcid,
                                    "internal": True,
                                    **(
                                        {"error": str(m.content)[:500]}
                                        if is_err
                                        else {"output": _extract_text(m.content)}
                                    ),
                                },
                            ))
                            # 内部工具调用计入执行预算/遥测（与 ToolCallLimit 口径对齐）
                            ctx.tool_calls += 1
                        elif isinstance(m, AIMessage):
                            for tc in (getattr(m, "tool_calls", None) or []):
                                tc_name = (tc.get("name") or "") if isinstance(tc, dict) else ""
                                tc_id = (tc.get("id") or "") if isinstance(tc, dict) else ""
                                if tc_id:
                                    tool_names_by_call[tc_id] = tc_name
                                if tc_name == "task" and isinstance(tc, dict):
                                    # 子代理入口：发布开始事件 + 归属标记，
                                    # 子代理期间的工具步骤（带 subagent 标记）由前端归组展示
                                    sa_name = str((tc.get("args") or {}).get("subagent_type") or "")
                                    if sa_name and tc_id:
                                        subagent_by_call[tc_id] = sa_name
                                        await bus.publish(RuntimeEvent(
                                            type=SUBAGENT_STARTED, run_id=run.run_id,
                                            data={"name": sa_name, "tool_call_id": tc_id},
                                        ))
                                if tc_name and tc_name not in platform_names:
                                    await bus.publish(RuntimeEvent(
                                        type=TOOL_STARTED,
                                        run_id=run.run_id,
                                        data={
                                            "tool": tc_name,
                                            "tool_call_id": (tc.get("id") or "") if isinstance(tc, dict) else "",
                                            "internal": True,
                                            # 工具输入是执行证据（评测交付物采集来源）：
                                            # 只脱敏不截断，持久化统一按事件类型截断
                                            "input": redact_secrets(
                                                (tc.get("args") or {}) if isinstance(tc, dict) else {}
                                            ),
                                        },
                                    ))
                    review = node_update.get("completion_review") or {}
                    if review.get("run_id") == run.run_id:
                        ctx.completion_review = review
                    if review.get("continuing"):
                        await bus.publish(RuntimeEvent(type=RUN_RETRYING, run_id=run.run_id, data={
                            "reason": "verification_fix",
                            # 续写与返工分开计数，展示上合并为总轮次
                            "round": review["fix_rounds"] + int(review.get("continuations") or 0),
                            # 截断续写（prefix 存在）只补余下内容；否则为整稿替换（前端据此撤回旧稿）
                            "continuation": bool(review.get("prefix")),
                            "failed_checks": [check for check in review["report"]["checks"] if not check["passed"]],
                        }))
                # P1-05 §13：每个图步/工具边界 CAS 累计预算（崩溃恢复不丢累计）
                await self._flush_budget(ctx, run)
        if interrupted:
            run.transition(STATUS_WAITING_APPROVAL)
            await self._flush_budget(ctx, run)
            # 暂停态必须先持久化成功，再对外宣告（失败向上抛出）
            await self._commit(run, status=STATUS_WAITING_APPROVAL)
            await bus.publish(RuntimeEvent(
                type=RUN_WAITING_APPROVAL, run_id=run.run_id,
                data={"actions": redact_sensitive(run.pending_actions)},
            ))
        # P1-05 §13：收尾再刷一次预算（抓取尾部增量，正常完成/中断均覆盖）
        await self._flush_budget(ctx, run)
        # P1-06 §14.5：收尾刷一次 MODEL_DELTA 缓冲（确保尾部增量落库，最终文本重建完整）
        await bus.flush_deltas()

    async def _read_completion_review(self, agent, run_config, ctx, run, bus) -> dict | None:
        review = ctx.completion_review
        if not review and (ctx.task_contract.is_complex or run.attempt > 1):
            state = await agent.aget_state(run_config)
            review = (state.values or {}).get("completion_review")
        if not review:
            return None
        if review.get("run_id") != run.run_id:
            return None
        verification = {**review["report"], "version": 2, "stop_reason": review.get("stop_reason", "")}
        run.final_text_parts = [review["answer"]]
        await self._publish_completion_artifacts(ctx, run, verification)
        if ctx.task_contract.is_complex or verification["status"] != "passed" or verification.get("fix_rounds"):
            await bus.publish(RuntimeEvent(type=VERIFICATION_COMPLETED, run_id=run.run_id, data=verification))
        return verification

    async def _retain_interrupted_completion(
        self, run, ctx, reason: str, *, graceful: bool = False, resumable_route: bool = False,
    ) -> tuple[str | None, dict | None]:
        from app.services.agent_runtime.completion import bounded_answer, interrupted_report, supports_completion_resume

        summary = {"stop": {"reason": reason, "can_continue": False}} if graceful else {}
        if not (ctx and ctx.task_contract and ctx.task_contract.is_complex):
            if graceful:
                draft, _ = bounded_answer("".join(run.final_text_parts))
                return draft, summary
            return None, None

        last = ctx.completion_review or {}
        candidate = last.get("answer") if last.get("run_id") == run.run_id else None
        draft, _ = bounded_answer(candidate or "".join(run.final_text_parts))
        report = {
            "version": 2, "status": "partial", "stop_reason": reason, "artifacts": [], "checks": [{
                "id": "execution_interrupted", "passed": False, "detail": "任务中断，最终交付尚未完成",
            }],
        }
        try:
            async with asyncio.timeout(3):
                evidence = await interrupted_report(ctx, draft, reason)
                await self._publish_completion_artifacts(ctx, run, evidence)
                report = evidence
                if graceful:
                    summary["stop"]["can_continue"] = bool(
                        run.session_id and resumable_route and evidence.get("stop_reason") != "external_operation"
                        and supports_completion_resume(ctx.task_contract)
                    )
        except Exception:
            logger.warning("run %s 中断后未能完成产物核对", run.run_id, exc_info=True)
        return draft, {**summary, "verification": report}

    async def _publish_completion_artifacts(self, ctx, run, verification: dict) -> None:
        from app.api.workspace.artifacts import make_artifact_id
        from app.services.agent_runtime.completion import freeze_artifacts

        artifacts = verification.get("artifacts") or []
        if not artifacts:
            return
        if run.session_id:
            frozen = await asyncio.to_thread(freeze_artifacts, ctx, artifacts)
            # 旧版 checkpoint 里的产物仍可通过原有会话链接下载。
            paths = {**{item["path"]: item["path"] for item in artifacts if not item.get("sha256")}, **frozen}
            verification["artifacts"] = [
                {**artifact, "download_url": "/api/v2/artifacts/" + make_artifact_id(run.session_id, paths[artifact["path"]])}
                for artifact in artifacts if artifact["path"] in paths
            ]
            if missing := [item["path"] for item in artifacts if item["path"] not in paths]:
                verification["status"] = "partial"
                verification["stop_reason"] = verification.get("stop_reason") or "artifact_publish"
                verification["checks"] = [*verification["checks"], {
                    "id": "artifact_saved", "passed": False,
                    "detail": "未能保存可下载版本 " + "、".join(missing[:6]),
                }]

    async def _handle_interrupt(self, interrupts, run, bus) -> None:
        actions: List[Dict[str, Any]] = []
        items = interrupts
        if isinstance(items, tuple):
            items = list(items)
        for it in items:
            value = _field(it, "value", it)
            interrupt_id = str(_field(it, "id", "") or "")
            if isinstance(value, dict) and "action_requests" in value:
                for ridx, req in enumerate(value["action_requests"]):
                    actions.append(_action_from_req(req, interrupt_id, ridx))
        run.pending_actions = actions
        await self._record_approval_requests(run.run_id, actions)
        await bus.publish(RuntimeEvent(
            type=APPROVAL_REQUIRED, run_id=run.run_id, data={"actions": actions}
        ))

    @staticmethod
    async def _close_team_board(run: RunHandle, phase: str) -> None:
        from app.services.agent_runtime.team_runtime import close_team_board

        try:
            await close_team_board(run, phase)
        except Exception:
            logger.exception("run %s 团队任务板收尾失败", run.run_id)

    async def _fail(self, run: RunHandle, error: str, *, status: str, bus: EventBus,
                    document_failure_code: str | None = None, team_mode: bool = False,
                    result_summary: dict | None = None, draft: str | None = None) -> None:
        """失败路径（P0-02 F）：不得覆盖已提交的终态。

        - Run 已是终态（如 completed 后 Session 写回异常）：直接返回，
          不写库、不发布 RUN_FAILED（完成态不得降级为 failed）；
        - 非法状态迁移：不吞掉后继续写库；
        - 提交被拒（RunLeaseLost / IllegalTransition）：跳过写库与事件发布
          （数据库 CAS 是最终状态权威）。
        """
        if run.is_terminal:
            logger.warning(
                "run %s 已处于终态 %s，拒绝降级为 %s（不写库、不发布失败事件）",
                run.run_id, run.status, status,
            )
            return
        try:
            run.transition(status)
        except InvalidTransition as e:
            logger.warning(
                "run %s 非法状态迁移 %s -> %s，拒绝写库：%s",
                run.run_id, run.status, status, e,
            )
            return
        run.error = error
        summary = {"document_failure_code": document_failure_code} if document_failure_code else result_summary
        try:
            await self._commit(run, status=status, error=error, finished=True, summary=summary, final_text=draft)
        except (RunLeaseLost, IllegalTransition):
            # 提交被拒：不写 Session、不发布 RUN_FAILED（不覆盖有效状态）
            return
        if summary is not None:
            run.summary = summary
        if draft is not None:
            run.final_text = draft
        if team_mode:
            await self._close_team_board(run, "failed")
        try:
            await self._write_back_session(run)  # FR-08.2：终态统一写回
        except Exception:
            logger.exception("run %s 失败态会话写回失败", run.run_id)
        await bus.publish(RuntimeEvent(
            type=RUN_FAILED, run_id=run.run_id,
            data={"error": error, "status": status, "document_retryable": bool(document_failure_code),
                  **({"final_text": draft, "summary": summary} if draft is not None else {})},
        ))

    async def _load_prior_final_text(self, run_id: str) -> List[str]:
        """FR-09：从持久化事件日志重建本 Run 已产生的最终文本片段（MODEL_DELTA），
        供挂起/重启恢复后的 _drive 调用继续累积，保证最终文本跨恢复完整。
        run_retrying 标记的断点重试按 keep_chars 撤回未完成增量（与执行侧回滚口径一致）。"""
        async with AsyncSessionLocal() as db:
            rows = await EventRepository(db).list_after(run_id, 0)
        parts: List[str] = []
        for r in rows:
            if not r.payload:
                continue
            if r.event_type == MODEL_DELTA and r.payload.get("text"):
                parts.append(r.payload["text"])
            elif r.event_type == RUN_RETRYING and r.payload.get("keep_chars") is not None:
                keep = int(r.payload["keep_chars"])
                acc, cut = 0, len(parts)
                for i, t in enumerate(parts):
                    if acc + len(t) > keep:
                        cut = i
                        break
                    acc += len(t)
                del parts[cut:]
        return parts
