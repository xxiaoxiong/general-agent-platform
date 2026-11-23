"""会话写回与 Outbox 交付（自 service.py 机械拆分，逻辑零改动）。"""
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


class DeliveryMixin:

    async def _write_back_session(self, run: RunHandle) -> None:
        """FR-08.2 + P1-02 C：Assistant 最终结果经 Outbox 写回 Session。

        Run 终态与写回解耦：终态提交时已同事务入队 ``SessionResultOutbox``，
        本方法执行实际写回——按 ``source_run_id`` 去重 + ``version`` 乐观锁合并，
        写回与 ``mark_delivered`` 同事务提交（重试不会重复追加 Assistant 消息）。
        写回失败仅增加 ``attempts`` 并保留 pending（Outbox Worker 重试，服务
        重启后可恢复），绝不改变 Run 终态。写回失败向上抛出（Completion 路径
        调用方先已持久化 Run 结果，失败态路径调用方仅记日志）。
        """
        with platform_span("session_writeback", run_id=run.run_id,
                           attributes={"session_id": run.session_id}):
            await self._deliver_outbox(run.run_id)

    async def _deliver_outbox(self, run_id: str) -> None:
        """按 Outbox 记录执行一次 Session 写回（幂等、可重试）。"""
        async with AsyncSessionLocal() as db:
            outbox = await SessionOutboxRepository(db).get_by_run(run_id)
            row = await RunRepository(db).get_run(run_id)

        if outbox is not None and outbox.status == OUTBOX_DELIVERED:
            return  # 已写回：幂等跳过
        if row is not None and row.status not in TERMINAL_STATUSES:
            return
        session_id = row.session_id if row is not None else (
            outbox.session_id if outbox is not None else None
        )
        if session_id is None:
            if outbox is not None:
                async with AsyncSessionLocal() as db:
                    await SessionOutboxRepository(db).mark_delivered(run_id)
                    await db.commit()
            return

        steps: List[Dict[str, Any]] = []
        async for legacy in self.replay_events(run_id):
            if legacy.get("type") != "done":
                steps.append(legacy)

        try:
            async with AsyncSessionLocal() as db:
                if row is not None:
                    live = (await db.execute(select(AgentRunRow.status, AgentRunRow.attempt).where(
                        AgentRunRow.id == run_id,
                    ).with_for_update())).one_or_none()
                    if live is None or (live.status, live.attempt) != (row.status, row.attempt):
                        return
                await SessionRepository(db).append_messages(
                    session_id,
                    [{
                        "message_id": uuid.uuid4().hex,
                        "role": "assistant",
                        "content": (row.final_text or "") if row is not None else "",
                        "steps": steps,
                        "source_run_id": run_id,
                        "created_at": datetime.utcnow().isoformat(),
                    }],
                    source_run_id=run_id,
                    replace_source=bool(row is not None and (row.input_payload or {}).get("document_retry_count")),
                )
                outbox_repo = SessionOutboxRepository(db)
                if await outbox_repo.get_by_run(run_id) is None:
                    await outbox_repo.enqueue(run_id, session_id)
                await outbox_repo.mark_delivered(run_id)
                await db.commit()
        except LookupError:
            # 会话已删除：跳过写回并标记完成，避免无限重试
            logger.warning("run %s 会话 %s 不存在，跳过写回", run_id, session_id)
            await self._settle_outbox(run_id, session_id, error=None)
        except Exception as e:
            # 写回失败：保留 pending 供重试（服务重启后可恢复），向上抛出
            await self._settle_outbox(run_id, session_id, error=str(e))
            raise

    async def _settle_outbox(
        self, run_id: str, session_id: Optional[int], *, error: Optional[str]
    ) -> None:
        async with AsyncSessionLocal() as db:
            outbox_repo = SessionOutboxRepository(db)
            if await outbox_repo.get_by_run(run_id) is None:
                await outbox_repo.enqueue(run_id, session_id)
            if error is None:
                await outbox_repo.mark_delivered(run_id)
            else:
                await outbox_repo.record_failure(run_id, error)
            await db.commit()

    async def deliver_pending_outbox(self, limit: int = 20) -> int:
        """Outbox Worker：恢复扫描 pending 写回任务。

        服务重启后未完成的写回可继续恢复；已 delivered 的按 source_run_id
        幂等跳过，重试不会重复追加 Assistant 消息。
        """
        async with AsyncSessionLocal() as db:
            rows = await SessionOutboxRepository(db).list_pending(limit)
            run_ids = [r.run_id for r in rows]
        delivered = 0
        for rid in run_ids:
            try:
                await self._deliver_outbox(rid)
                delivered += 1
            except Exception as e:
                logger.warning(
                    "outbox 写回 run %s 失败（保留 pending 供重试）：%s", rid, e
                )
        return delivered
