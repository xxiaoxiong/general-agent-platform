"""Run 生命周期：创建/幂等/取消/状态/恢复检查（自 service.py 机械拆分，逻辑零改动）。"""
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


class LifecycleMixin:

    # ── 公共 API ──────────────────────────────────────────────────────────

    async def start_run(
        self,
        *,
        agent_config: Dict[str, Any],
        messages: List[Dict[str, Any]],
        user_id: int,
        session_id: Optional[int] = None,
        idempotency_key: Optional[str] = None,
        use_memory_context: str = "",
        db_info: Optional[Dict[str, Any]] = None,
        endpoint: str = "chat",
        user_message: Optional[Dict[str, Any]] = None,
    ) -> Tuple[str, AsyncGenerator[Dict[str, Any], None]]:
        """启动运行并返回 (run_id, 事件流)：run_id 可先行写入响应头（X-Run-Id）。"""
        run, _adapter, stream = await self._start(
            agent_config=agent_config,
            messages=messages,
            user_id=user_id,
            session_id=session_id,
            idempotency_key=idempotency_key,
            use_memory_context=use_memory_context,
            db_info=db_info,
            endpoint=endpoint,
            user_message=user_message,
        )
        return run.run_id, stream

    async def start_run_with_session(
        self,
        *,
        agent_config: Dict[str, Any],
        messages: List[Dict[str, Any]],
        user_id: int,
        session_id: Optional[int] = None,
        idempotency_key: Optional[str] = None,
        use_memory_context: str = "",
        db_info: Optional[Dict[str, Any]] = None,
        endpoint: str = "chat",
        user_message: Optional[Dict[str, Any]] = None,
        execution_context: Optional[Dict[str, Any]] = None,
        request_hash: Optional[str] = None,
    ) -> Tuple[str, Optional[int], AsyncGenerator[Dict[str, Any], None]]:
        """启动运行并返回 (run_id, session_id, 事件流)。

        session_id 为业务事务内解析后的会话 ID（未指定时由服务创建），
        供 API 写入 X-Session-Id 响应头。
        """
        run, _adapter, stream = await self._start(
            agent_config=agent_config,
            messages=messages,
            user_id=user_id,
            session_id=session_id,
            idempotency_key=idempotency_key,
            use_memory_context=use_memory_context,
            db_info=db_info,
            endpoint=endpoint,
            user_message=user_message,
            execution_context=execution_context,
            request_hash=request_hash,
        )
        return run.run_id, run.session_id, stream

    async def lookup_idempotent(
        self,
        user_id: Optional[int],
        idempotency_key: str,
        request_hash: str,
    ):
        """幂等预查（P1-01：收敛到 Service/Repository，API 不再裸查）。

        - 同 Key 同请求摘要：返回已有 Run 行（调用方复用，不重复执行/追加消息）；
        - 同 Key 不同请求摘要：抛 ``IdempotencyConflict``（API 映射 HTTP 409）。
        """
        async with AsyncSessionLocal() as db:
            existing = await RunRepository(db).get_by_idempotency_key(
                user_id, idempotency_key
            )
            if existing is None:
                return None
            return RunRepository._check_reuse(existing, request_hash)

    async def idempotent_replay_stream(
        self, run_id: str
    ) -> AsyncGenerator[Dict[str, Any], None]:
        """幂等命中的 SSE 回放流（P1-01）：按持久化事件重建旧契约事件。

        流式请求重试命中幂等时必须保持 SSE 协议（FR-01），不得改为普通
        JSON；回放以 ``done`` 终止（运行未终结时按当前状态合成终止事件，
        后续进度可经 /runs/{run_id}/events 获取）。
        """
        ended = False
        async for legacy in self.replay_events(run_id):
            ended = ended or legacy.get("type") == "done"
            yield legacy
        if not ended:
            async with AsyncSessionLocal() as db:
                row = await RunRepository(db).get_run(run_id)
            yield {"type": "done", "status": row.status if row is not None else "failed"}

    async def run_to_completion(
        self,
        *,
        agent_config: Dict[str, Any],
        messages: List[Dict[str, Any]],
        user_id: int,
        session_id: Optional[int] = None,
        idempotency_key: Optional[str] = None,
        use_memory_context: str = "",
        db_info: Optional[Dict[str, Any]] = None,
        endpoint: str = "chat",
        user_message: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """非流式执行：返回 {status, content, steps, run_id, session_id}。"""
        run, _adapter, stream = await self._start(
            agent_config=agent_config,
            messages=messages,
            user_id=user_id,
            session_id=session_id,
            idempotency_key=idempotency_key,
            use_memory_context=use_memory_context,
            db_info=db_info,
            endpoint=endpoint,
            user_message=user_message,
        )
        steps: List[Dict[str, Any]] = []
        content = ""
        status = run.status
        async for legacy in stream:
            if legacy.get("type") == "final":
                content = legacy.get("content") or ""
            if legacy.get("type") == "done":
                status = legacy.get("status") or status
                continue
            steps.append(legacy)
        return {
            "status": status,
            "content": content,
            "steps": steps,
            "run_id": run.run_id,
            "session_id": run.session_id,
        }

    async def cancel_run(self, run_id: str, user_id: int) -> Dict[str, Any]:
        from app.services.agent_runtime.run_input import cancel_waiting_input

        if await cancel_waiting_input(run_id, user_id):
            return {"ok": True, "status": STATUS_CANCELLED}
        run = self.registry.get(run_id)
        if run is None:
            # 无内存句柄：按数据库状态取消（pending 走无 Lease 专用 CAS）
            async with AsyncSessionLocal() as db:
                repo = RunRepository(db)
                row = await repo.get_run(run_id)
                if row is None or row.user_id not in (None, user_id):
                    return {"ok": False, "error": "运行不存在"}
                if row.status in TERMINAL_STATUSES:
                    return {"ok": True, "status": row.status}
                row_status, row_session_id = row.status, row.session_id
                # CAS 写入取消意图（P1-04 §12：DB 是取消意图唯一真相源）
                await repo.request_cancel(run_id, user_id)
                await db.commit()
                if row_status == STATUS_PENDING:
                    ok = await repo.cancel_without_lease(run_id)
                    if ok and row_session_id:
                        # 无 Lease 取消也要释放 Session 执行权（P1-02）
                        await SessionRepository(db).release_execution(
                            row_session_id, run_id
                        )
                    await db.commit()
                    if ok:
                        return {"ok": True, "status": STATUS_CANCELLED}
                    return {"ok": False, "error": "运行状态已变更（可能已被 Worker 领取），无法直接取消"}
            # running 且无内存句柄（Worker 崩溃/他机持有）：已写取消意图，
            # 由持有 Worker 心跳轮询发现并取消（在途外部工具不伪造取消完成）
            return {"ok": True, "status": "cancelling"}
        if run.user_id != user_id:
            return {"ok": False, "error": "无权取消该运行"}
        if run.is_terminal:
            return {"ok": True, "status": run.status}
        # CAS 写入取消意图（DB 是取消意图唯一真相源），随后尽快终止
        async with AsyncSessionLocal() as db:
            await RunRepository(db).request_cancel(run_id, user_id)
            await db.commit()
        if run.task and not run.task.done():
            # _drive 的 CancelledError 分支负责落库 cancelled（fencing 校验）
            run.task.cancel()
            return {"ok": True, "status": "cancelling"}
        # 无执行权（pending / 等待审批 / 待核对）：无 Lease 专用 CAS（LF-06），
        # 不得用无条件更新覆盖正在执行的 Run
        async with AsyncSessionLocal() as db:
            ok = await RunRepository(db).cancel_without_lease(
                run_id,
                from_statuses=(
                    STATUS_PENDING,
                    STATUS_WAITING_APPROVAL,
                    STATUS_NEEDS_RECONCILIATION,
                ),
            )
            if ok and run.session_id:
                # 无 Lease 取消也要释放 Session 执行权（P1-02）
                await SessionRepository(db).release_execution(
                    run.session_id, run_id
                )
            await db.commit()
        if ok:
            try:
                run.transition(STATUS_CANCELLED)
            except InvalidTransition:
                pass
            return {"ok": True, "status": STATUS_CANCELLED}
        return {"ok": False, "error": "运行状态已变更，无法直接取消"}

    async def get_run(self, run_id: str, user_id: int) -> Optional[Dict[str, Any]]:
        """查询运行状态（P1-04）：**恒读数据库**，Registry 仅执行热数据，不作状态真相源。

        Worker 崩溃后 Registry 可能残留陈旧内存视图；数据库是状态唯一真相源，
        Registry 仅补充执行热数据（未决动作）。DB 写入与读取一致性见验收测试。
        """
        async with AsyncSessionLocal() as db:
            from sqlalchemy import select
            from app.models.agent import AgentRun

            row = (await db.execute(select(AgentRun).where(AgentRun.id == run_id))).scalar_one_or_none()
            if row is None or (row.user_id not in (None, user_id)):
                return None
            result: Dict[str, Any] = {
                "run_id": row.id,
                "status": row.status,
                "agent_id": row.agent_id,
                "session_id": row.session_id,
                "thread_id": row.thread_id,
                "error": row.error,
                "cancel_requested": bool(row.cancel_requested),
                "goal_mode": bool((row.agent_config_snapshot or {}).get("goal_mode")),
                "created_at": row.created_at.isoformat() if row.created_at else None,
                "finished_at": row.finished_at.isoformat() if row.finished_at else None,
                "last_event_seq": row.last_event_seq or 0,
                "pending_input": None,
            }
            if row.status == STATUS_LIMIT_EXCEEDED and (stop := (row.summary or {}).get("stop")):
                result["summary"] = {"stop": stop}
            execution_context = (row.input_payload or {}).get("execution_context")
            if execution_context is not None:
                from app.services.agent_runtime.document_retry import document_retryable

                result["document_retryable"] = document_retryable(row)
                # 附件工作区位置和指纹是执行快照的一部分，状态接口只返回路线和固定版本。
                result["execution_context"] = {
                    key: value for key, value in execution_context.items()
                    if key not in {"attachment_refs", "source_attachment", "document_context"}
                }
            if row.status == STATUS_WAITING_INPUT:
                from app.services.agent_runtime.run_input import pending_input_payload

                result["pending_input"] = await pending_input_payload(db, run_id)
            # Langfuse Trace 关联（确定性派生；未配置时为 None，前端隐藏入口）
            from app.observability.langfuse_tracing import is_enabled as _lf_enabled
            if _lf_enabled():
                from app.observability.langfuse_tracing import run_trace_id as _lf_trace_id
                result["langfuse_trace_id"] = _lf_trace_id(row.id)
                result["langfuse_trace_url"] = trace_url(row.id)
        # Registry 仅补充执行热数据（未决动作），状态真相仍是 DB
        handle = self.registry.get(run_id)
        if handle is not None:
            result["pending_actions"] = redact_sensitive(handle.pending_actions)
        return result

    async def get_thread_state(self, thread_id: str) -> Optional[Dict[str, Any]]:
        """从 checkpoint 读取恢复所需的元数据及未完成中断。"""
        from langgraph.pregel._task_status import read_task_statuses

        config = {"configurable": {"thread_id": thread_id}}
        try:
            cp = await CheckpointerFactory.get().get_checkpointer()
            record = await cp.aget_tuple(config)
        except Exception as e:
            return {"error": str(redact_secrets(str(e)))[:500]}
        if record is None:
            return None
        checkpoint = record.checkpoint
        writes = record.pending_writes or []
        # 已完成节点的历史 interrupt 可能与输出同时出现在写入日志中。
        interrupts = [interrupt for status in read_task_statuses(writes).values() for interrupt in status.pending_interrupts]
        values = checkpoint.get("channel_values") or {}
        versions = checkpoint.get("channel_versions") or {}
        seen = checkpoint.get("versions_seen") or {}
        # 这里只报告持久化的候选分支和 Send 节点；编译执行图负责计算最终调度顺序。
        next_nodes = [name for channel in values if channel.startswith("branch:to:")
                      and (name := channel.removeprefix("branch:to:"))
                      and versions.get(channel) != seen.get(name, {}).get(channel)]
        next_nodes.extend(name for send in values.get("__pregel_tasks", ()) or ()
                          if isinstance(name := _field(send, "node"), str))
        return {
            "thread_id": thread_id,
            "checkpoint_id": checkpoint.get("id"),
            "run_id": record.metadata.get("run_id"),
            "next": list(dict.fromkeys(next_nodes)),
            "has_pending_writes": bool(writes),
            "has_interrupt": bool(interrupts),
            "pending_actions": self._extract_snapshot_actions(interrupts),
        }

    @staticmethod
    def _extract_snapshot_actions(interrupts: list[Any]) -> List[Dict[str, Any]]:
        """从 Checkpoint 快照提取未决 HITL 动作（恢复审批所需）。

        动作带稳定 action_id（FR-06）：重启后重提取与中断时记录同一 ID。
        """
        actions: List[Dict[str, Any]] = []
        for item in interrupts:
            value = _field(item, "value", item)
            interrupt_id = str(_field(item, "id", "") or "")
            if isinstance(value, dict) and isinstance(requests := value.get("action_requests"), list):
                for index, request in enumerate(requests):
                    if isinstance(request, dict):
                        actions.append(redact_secrets(_action_from_req(request, interrupt_id, index)))
        return actions

    async def _current_batch_action_ids(self, run_id: str) -> List[str]:
        """当前 Interrupt 批次的动作 ID（按 action_requests 顺序 = 登记 id 顺序）。

        历史批次动作均已决策（否则无法推进到本批次），故当前批次恰为
        **尚未决策**（decision IS NULL）的审批动作；登记顺序与图内
        action_requests 位置对齐（``_record_approval_requests`` 按动作顺序插入）。
        """
        from sqlalchemy import select
        from app.models.agent import AgentApproval

        async with AsyncSessionLocal() as db:
            rows = (await db.execute(
                select(AgentApproval.action_id)
                .where(AgentApproval.run_id == run_id, AgentApproval.decision.is_(None))
                .order_by(AgentApproval.id)
            )).scalars().all()
        return [str(a) for a in rows if a]

    async def _build_resume_decisions(
        self, run_id: str, batch_action_ids: List[str]
    ) -> Optional[List[Dict[str, Any]]]:
        """构造 Command(resume) 的 decisions 数组（P1-03 A/B）。

        - 只按**当前 Interrupt 批次**（``batch_action_ids``）的动作顺序构造，
          禁止把历史批次决策带入；
        - 数组与 action_requests 按位置对齐、数量必须一致（langchain batched）；
        - 每个批次动作必须有明确决策，否则返回 None（决策不完整，不得恢复）。
        """
        if not batch_action_ids:
            # waiting_approval 必有未决动作；空批次属异常，保守拒绝恢复
            return None
        from sqlalchemy import select
        from app.models.agent import AgentApproval

        async with AsyncSessionLocal() as db:
            rows = (await db.execute(
                select(AgentApproval).where(AgentApproval.run_id == run_id)
            )).scalars().all()
        by_action = {r.action_id: r for r in rows if r.action_id}
        graph_decisions: List[Dict[str, Any]] = []
        for aid in batch_action_ids:
            r = by_action.get(aid)
            if r is None or r.decision is None:
                return None  # 批次动作缺少决策：不完整，不得恢复
            graph_decisions.append({"type": r.decision, "message": r.message or ""})
        return graph_decisions

    # ── 内部：启动与驱动 ──────────────────────────────────────────────────

    async def _start(
        self,
        *,
        agent_config: Dict[str, Any],
        messages: List[Dict[str, Any]],
        user_id: int,
        session_id: Optional[int],
        idempotency_key: Optional[str] = None,
        use_memory_context: str = "",
        db_info: Optional[Dict[str, Any]] = None,
        endpoint: str = "chat",
        user_message: Optional[Dict[str, Any]] = None,
        execution_context: Optional[Dict[str, Any]] = None,
        request_hash: Optional[str] = None,
    ) -> Tuple[RunHandle, LegacyEventAdapter, AsyncGenerator[Dict[str, Any], None]]:
        """创建 Run（DB pending）→ Worker CAS Claim → 执行；返回句柄、适配器与旧契约事件流。

        幂等（P1-01 收敛 Service/Repository）：同一幂等键同一请求摘要返回已有
        Run（绝不重复执行、不重复追加用户消息）；同一幂等键不同请求摘要抛
        IdempotencyConflict（API 映射 409）。新 Session 创建、用户消息追加与
        Run 创建同一事务提交，并发幂等重试不产生孤立 Session。
        """
        snapshot = {k: v for k, v in (agent_config or {}).items() if not k.startswith("_")}
        input_payload = {
            "messages": messages,
            "use_memory_context": use_memory_context,
            "db_info": db_info,
        }
        if execution_context is not None:
            input_payload["execution_context"] = execution_context
        audit_origin = run_audit_origin()
        if audit_origin is not None:
            input_payload["audit_origin"] = audit_origin

        row, created, resolved_session_id = await self._begin_run(
            agent_config=agent_config,
            user_id=user_id,
            session_id=session_id,
            idempotency_key=idempotency_key,
            request_hash=request_hash or compute_request_hash(
                endpoint=endpoint,
                user_id=user_id,
                agent_id=agent_config.get("id"),
                session_id=session_id,
                user_input=(user_message or {}).get("content")
                or _last_user_input(messages),
                agent_config=agent_config,
            ),
            input_payload=input_payload,
            snapshot=snapshot,
            user_message=user_message,
        )
        run_id = row.id
        thread_id = row.thread_id

        run = self.registry.attach(
            run_id=run_id,
            agent_id=agent_config["id"],
            session_id=resolved_session_id,
            user_id=user_id,
            thread_id=thread_id,
            idempotency_key=idempotency_key,
            status=STATUS_PENDING if created else row.status,
        )
        adapter = LegacyEventAdapter(run_id)

        if not created:
            # 幂等命中：绝不重复创建/重复执行
            if run.task is not None and not run.task.done():
                return run, adapter, self._bus_stream(run, adapter)
            return run, adapter, self._replay_db_stream(run_id, run, adapter)

        assert self.worker is not None
        claimed = await self.worker.claim_run(run_id)
        if claimed is None:
            # 极小概率被其他 Worker 先行领取（或 Session 执行权被占）：
            # 退化为数据库回放流，不重复执行
            return run, adapter, self._replay_db_stream(run_id, run, adapter)

        run.lease_version = claimed.lease_version
        run.worker_id = self.worker.worker_id
        run.lease_expires_at = claimed.lease_expires_at
        run.attempt = claimed.attempt or 1
        run.transition(STATUS_RUNNING)
        self.worker.supervise(
            run,
            adapter,
            RunJob(
                run_id=run_id,
                user_id=user_id,
                messages=messages,
                agent_config=agent_config,
                use_memory_context=use_memory_context,
                db_info=db_info,
                execution_context=execution_context,
                audit_origin=audit_origin,
            ),
        )
        return run, adapter, self._bus_stream(run, adapter)

    async def _begin_run(
        self,
        *,
        agent_config: Dict[str, Any],
        user_id: int,
        session_id: Optional[int],
        idempotency_key: Optional[str],
        request_hash: Optional[str],
        input_payload: Dict[str, Any],
        snapshot: Dict[str, Any],
        user_message: Optional[Dict[str, Any]] = None,
    ) -> Tuple[AgentRunRow, bool, Optional[int]]:
        """业务事务（P1-01/P1-02）：幂等预查 + Session 创建 + 用户消息追加
        + Run 创建 + Session 执行权 Claim，单事务原子提交。

        - 幂等命中（同 Key 同摘要）：返回原 Run，不触碰 Session/消息；
        - 同 Key 不同摘要：``IdempotencyConflict``（API 映射 409）；
        - 并发同 Key：后到者在 Run 唯一约束上收敛为复用，同一事务回滚，
          不产生孤立 Session、不重复追加用户消息；
        - Session 执行权 CAS 失败：整事务回滚并抛 ``SessionBusy``（明确冲突）。
        """
        async with AsyncSessionLocal() as db:
            repo = RunRepository(db)
            sess_repo = SessionRepository(db)

            # 1) 幂等命中：同摘要复用原 Run，绝不重复执行/追加用户消息
            if idempotency_key:
                existing = await repo.get_by_idempotency_key(user_id, idempotency_key)
                if existing is not None:
                    checked = RunRepository._check_reuse(existing, request_hash)
                    return checked, False, checked.session_id

            # 2) Session 定位 / 创建（与 Run 创建同事务）
            resolved_session_id = session_id
            if resolved_session_id is None and user_message is not None:
                new_sess = AgentSession(
                    agent_id=agent_config["id"],
                    user_id=user_id,
                    messages=[],
                )
                db.add(new_sess)
                await db.flush()
                resolved_session_id = new_sess.id

            run_id = uuid.uuid4().hex
            execution = input_payload.get("execution_context") or {}
            document_run = execution.get("task_type") in DOCUMENT_GRAPH_TYPES
            # 3) 用户消息追加（稳定 message_id + version 乐观锁合并；与 Run 原子）
            if user_message is not None and resolved_session_id is not None:
                content = str(user_message.get("content") or "")
                await sess_repo.append_messages(
                    resolved_session_id,
                    [{
                        # 自动模式决策发生在 Run 内：沿用调用方稳定 message_id，
                        # 便于决策后补写 document_run_id 标记
                        "message_id": user_message.get("message_id") or uuid.uuid4().hex,
                        "role": user_message.get("role") or "user",
                        "content": content,
                        "created_at": datetime.utcnow().isoformat(),
                        **({"attachments": user_message["attachments"]} if user_message.get("attachments") else {}),
                        **({"document_run_id": run_id} if document_run else {}),
                    }],
                    title=_derive_title(content),
                )

            # 4) Run 创建（幂等唯一约束兜底并发；thread_id 随解析后的 Session 生成）
            # P2-01：Run 必须绑定不可变发布版本（无版本时自动发布 v1），
            # 执行期只读该版本快照，不随 Agent 行后续修改变化
            from app.services.assistant.versions import ensure_version

            version = await ensure_version(db, agent_config["id"], created_by=user_id)
            thread_id = f"agent-{agent_config['id']}-document-run-{run_id}" if document_run else make_thread_id(
                agent_config["id"], resolved_session_id, uuid.uuid4().hex
            )
            row, created = await repo.create_run(
                run_id=run_id,
                agent_id=agent_config["id"],
                agent_version_id=version.id if version is not None else None,
                session_id=resolved_session_id,
                user_id=user_id,
                thread_id=thread_id,
                idempotency_key=idempotency_key,
                request_hash=request_hash,
                input_payload=input_payload,
                agent_config_snapshot=snapshot,
            )
            if not created:
                # 并发幂等重试收敛（create_run 冲突路径已 rollback 本事务）：
                # 返回既有 Run，不重复执行、Session 创建/消息追加随回滚撤销
                return row, False, row.session_id

            # 5) Session 执行权原子 Claim（P1-02 A：单 Session 单活跃 Run）
            if resolved_session_id is not None:
                ok = await sess_repo.claim_execution(
                    resolved_session_id, row.id, self.worker.lease_seconds,
                    durable=document_run or execution.get("team_mode") is True,
                )
                if not ok:
                    raise SessionBusy(
                        f"session {resolved_session_id} 有正在执行的 Run，请稍后重试"
                    )

            await db.commit()
            return row, True, resolved_session_id

    async def _apply_hitl_wait_policy(self, run_id: str) -> None:
        """HITL 等待是否计入 Run Timeout（P1-05 §13.3）：由配置显式决定。

        - ``AGENT_HITL_WAIT_COUNTS_TO_TIMEOUT=True``（默认）：等待计入超时，
          deadline 为绝对时间、含等待，不顺延；
        - ``=False``：等待不计入超时，恢复时按等待时长顺延 deadline（只耗
          执行时间，不耗人工等待）。
        顺延后清除等待起点，避免重复顺延。
        """
        if getattr(settings, "AGENT_HITL_WAIT_COUNTS_TO_TIMEOUT", True):
            return
        async with AsyncSessionLocal() as session:
            repo = RunRepository(session)
            row = await repo.get_run(run_id)
            wait_since = getattr(row, "hitl_wait_started_at", None) if row else None
            if wait_since is not None:
                if settings.AGENT_RUN_TIMEOUT > 0:
                    waited = (datetime.utcnow() - wait_since).total_seconds()
                    await repo.shift_deadline(run_id, extra_seconds=waited)
                await repo.clear_hitl_wait(run_id)
            await session.commit()

    async def _recovery_check(
        self, row: Any
    ) -> Tuple[str, str, List[Dict[str, Any]], Dict[str, Any]]:
        """恢复决策（attempt > 1 的重新领取必须先判断可否安全继续，P0-04 §6.2 A）。

        返回 (mode, reason, pending_actions, metadata)：
        - fresh：无 Checkpoint 且无结果未知的副作用，用输入快照执行；
        - continue：有 Checkpoint 且无未决中断，从断点续跑（重放复用原
          tool_call_id / 幂等键）；
        - waiting_approval：Checkpoint 停在 HITL 中断，回到待审批；
        - needs_reconciliation：结果未知的副作用 / Checkpoint 损坏，禁止自动重放。

        决策唯一来源是 ``classification.recovery_decision``（§6.2 A 决策表）：
        - 无 Checkpoint + 结果未知的 external_idempotent/non_idempotent 副作用
          → 阻断（禁止重新推理生成新调用 ID）；
        - Checkpoint 不可读/损坏 → 安全阻断 + recovery_error，绝不 fresh restart。

        所有恢复决策写入日志与 ``agent_runs.recovery_metadata``（阻断原因同时
        写入 ``recovery_error``），满足 §6.3 可审计要求。
        """
        if (getattr(row, "attempt", 0) or 0) <= 1:
            return RECOVERY_FRESH, "", [], {}

        route = (getattr(row, "input_payload", None) or {}).get("execution_context") or {}
        document_run = route.get("task_type") in DOCUMENT_GRAPH_TYPES
        team_run = route.get("team_mode") is True
        has_document_inputs = False
        async with AsyncSessionLocal() as db:
            entries = await ToolExecutionRepository(db).list_unresolved(row.id)
            if document_run:
                from app.services.agent_runtime.run_input import has_input_history

                has_document_inputs = await has_input_history(db, row.id)

        state = await self.get_thread_state(row.thread_id) if document_run or not team_run else None
        if team_run:
            from app.services.agent_runtime.team_runtime import team_thread_id

            team_state = await self.get_thread_state(team_thread_id(row.id))
            if team_state and (team_state.get("error") or (state is None and not has_document_inputs)):
                state = team_state
        checkpoint_error: Optional[str] = None
        checkpoint_owner = state.get("run_id") if state else None
        other_run = bool(checkpoint_owner and checkpoint_owner != row.id and not (document_run or team_run))
        if state is None or other_run:
            # 同一会话共用执行线程，新 Run 在写入第一个快照前崩溃时不能续跑上一轮的工具。
            checkpoint_state = CHECKPOINT_MISSING
        elif state.get("error"):
            checkpoint_state = CHECKPOINT_UNREADABLE
            checkpoint_error = str(state["error"])
        else:
            checkpoint_state = CHECKPOINT_OK

        decision = recovery_decision(checkpoint_state, entries)
        mode, reason = decision.mode, decision.reason
        actions: List[Dict[str, Any]] = []
        if document_run and has_document_inputs and checkpoint_state == CHECKPOINT_MISSING:
            mode = RECOVERY_NEEDS_RECONCILIATION
            reason = "文档任务的 Checkpoint 丢失，需核对已提交输入后再继续"
        elif mode == RECOVERY_CONTINUE and not document_run and state.get("has_interrupt"):
            mode = RECOVERY_WAITING_APPROVAL
            actions = state.get("pending_actions") or []
            reason = "Checkpoint 停在 HITL 中断，回到待审批"

        metadata: Dict[str, Any] = {
            "mode": mode,
            "reason": reason,
            "checkpoint_state": checkpoint_state,
            "checkpoint_error": checkpoint_error,
            "pending": decision.pending,
            "decided_at": datetime.utcnow().isoformat() + "Z",
        }
        if other_run:
            metadata["checkpoint_run_id"] = checkpoint_owner
        logger.info(
            "run %s 恢复决策: mode=%s checkpoint=%s pending=%d reason=%s",
            row.id, mode, checkpoint_state, len(decision.pending), reason or "-",
        )

        lease = RunExecutionLease(
            run_id=row.id,
            worker_id=row.worker_id,
            lease_version=row.lease_version,
            lease_expires_at=row.lease_expires_at,
        )
        async with AsyncSessionLocal() as db:
            await RunRepository(db).record_recovery(
                row.id,
                lease,
                metadata=metadata,
                recovery_error=reason if mode == RECOVERY_NEEDS_RECONCILIATION else None,
            )
            await db.commit()
        return mode, reason, actions, metadata

    async def _run_thread_id(self, run_id: str) -> Optional[str]:
        from sqlalchemy import select
        from app.models.agent import AgentRun

        async with AsyncSessionLocal() as db:
            return (await db.execute(
                select(AgentRun.thread_id).where(AgentRun.id == run_id)
            )).scalar_one_or_none()


