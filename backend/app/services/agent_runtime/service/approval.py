"""HITL 审批决策与人工核对（自 service.py 机械拆分，逻辑零改动）。"""
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


class ApprovalMixin:

    async def _record_approval_requests(self, run_id: str, actions: List[Dict[str, Any]]) -> None:
        """审批请求先持久化（FR-06.2）：action_id 幂等，重启重放不产生重复记录。"""
        from sqlalchemy import select
        from app.models.agent import AgentApproval

        try:
            async with AsyncSessionLocal() as db:
                for a in actions:
                    action_id = str(a.get("action_id") or hashlib.sha256(
                        (str(a.get("tool")) + json.dumps(
                            a.get("args") or {}, ensure_ascii=False, sort_keys=True, default=str
                        )).encode("utf-8")
                    ).hexdigest()[:40])
                    existing = (await db.execute(
                        select(AgentApproval).where(
                            AgentApproval.run_id == run_id,
                            AgentApproval.action_id == action_id,
                        )
                    )).scalar_one_or_none()
                    if existing is not None:
                        continue  # 幂等：重放/重启不重复登记
                    db.add(AgentApproval(
                        run_id=run_id,
                        tool_name=str(a.get("tool") or "unknown"),
                        action_summary=json.dumps(a.get("args") or {}, ensure_ascii=False)[:2000],
                        action_id=action_id,
                        tool_call_id=a.get("tool_call_id"),
                        args_hash=a.get("args_hash"),
                        action_payload=a.get("args") or None,
                        decision_status="pending",
                    ))
                await db.commit()
        except Exception as e:
            raise PersistenceError(f"run {run_id} 审批记录写入失败: {e}") from e

    @staticmethod
    def _match_approval_row(rows: List[Any], dec: Dict[str, Any], idx: int = 0) -> Optional[Any]:
        """决策与审批动作匹配：**仅按 action_id 精确匹配**（P1-03 B）。

        禁止按下标跨批次猜测审批对象；未携带 action_id 的决策无法安全定位到
        具体动作（广播语义由调用方 ``_expand_decisions`` 处理），此处返回 None。
        """
        action_id = dec.get("action_id")
        if action_id is None:
            return None
        for r in rows:
            if r.action_id == action_id:
                return r
        return None

    @staticmethod
    def _expand_decisions(decisions: List[Dict[str, Any]], rows: List[Any]) -> List[Dict[str, Any]]:
        """把广播决策展开为逐动作决策（P1-03 B）。

        - 携带 action_id：精确对应一个动作（否则保持原样，由匹配阶段判定未知动作）；
        - 未携带 action_id：广播到当前所有**未决策**动作（便捷 "approve all"），
          不按数组下标猜测历史动作，避免跨批次误配。
        """
        expanded: List[Dict[str, Any]] = []
        for dec in decisions:
            if dec.get("action_id") is not None:
                expanded.append(dec)
                continue
            targets = [r.action_id for r in rows if r.decision is None and r.action_id]
            if not targets:
                expanded.append(dec)  # 无未决动作：保留原决策交由匹配阶段报未知动作
            for aid in targets:
                expanded.append({**dec, "action_id": aid})
        return expanded

    async def _check_decisions(self, run_id: str, decisions: List[Dict[str, Any]]) -> str:
        """审批决策校验（不落库）。返回 'ok'（可受理）| 'idempotent'（重复一致提交）。

        - 未知决策类型 / 未知审批动作 -> ApprovalInvalid
        - 同一动作改判 -> ApprovalConflict（F-11：冲突必须显式拒绝，不得静默覆盖）
        """
        valid_types = {"approve", "reject"}
        if not decisions:
            raise ApprovalInvalid("审批决策为空")
        from sqlalchemy import select
        from app.models.agent import AgentApproval

        async with AsyncSessionLocal() as db:
            rows = (await db.execute(
                select(AgentApproval).where(AgentApproval.run_id == run_id).order_by(AgentApproval.id)
            )).scalars().all()
        verdicts: List[str] = []
        for i, dec in enumerate(self._expand_decisions(decisions, rows)):
            dtype = str(dec.get("type") or "").strip()
            if dtype not in valid_types:
                raise ApprovalInvalid(f"未知决策类型: {dtype!r}（仅支持 approve / reject）")
            row = self._match_approval_row(rows, dec, i)
            if row is None:
                raise ApprovalInvalid(f"未知审批动作: {dec.get('action_id') or f'#{i + 1}'}")
            if row.decision is not None:
                if row.decision == dtype:
                    verdicts.append("idempotent")
                else:
                    raise ApprovalConflict(
                        f"审批动作 {row.action_id or row.id} 已决策为 {row.decision}，"
                        f"与新决策 {dtype} 冲突"
                    )
            else:
                verdicts.append("record")
        return "idempotent" if verdicts and all(v == "idempotent" for v in verdicts) else "ok"

    async def check_decisions(
        self,
        run_id: str,
        decisions: List[Dict[str, Any]],
        deciding_user_id: int,
    ) -> str:
        """审批前置校验（不落库，API 层映射 HTTP 403/400/409）。"""
        run = self.registry.get(run_id)
        owner: Optional[int] = run.user_id if run is not None else None
        if run is None:
            from sqlalchemy import select
            from app.models.agent import AgentRun

            async with AsyncSessionLocal() as db:
                row = (await db.execute(
                    select(AgentRun).where(AgentRun.id == run_id)
                )).scalar_one_or_none()
            if row is None:
                raise ApprovalInvalid("运行不存在或不在待审批状态")
            owner = row.user_id
        if owner not in (None, deciding_user_id):
            raise ApprovalForbidden("无权审批该运行")
        return await self._check_decisions(run_id, decisions)

    async def _record_approval_decision(self, run_id: str, decisions: List[Dict[str, Any]], user_id: int) -> None:
        """审批决策先持久化（FR-04.6 / P1-03 C）：数据库 CAS 条件更新。

        - 决策写入用条件 UPDATE（``WHERE action_id=? AND decision IS NULL``），
          并发审批同一动作时只有一个决策成功；
        - 重复一致决策幂等（不重复写入、不重复执行）；不同决策抛
          ``ApprovalConflict``（F-11：冲突必须显式拒绝，不得静默覆盖）；
        - 关键写入失败必须向上抛出：决策未落库不得发送恢复命令。
        """
        from sqlalchemy import select, update
        from app.models.agent import AgentApproval

        try:
            async with AsyncSessionLocal() as db:
                rows = (await db.execute(
                    select(AgentApproval).where(AgentApproval.run_id == run_id).order_by(AgentApproval.id)
                )).scalars().all()
                for i, dec in enumerate(self._expand_decisions(decisions, rows)):
                    row = self._match_approval_row(rows, dec, i)
                    if row is None:
                        raise ApprovalInvalid(f"未知审批动作: {dec.get('action_id') or f'#{i + 1}'}")
                    dtype = str(dec.get("type"))
                    # CAS：仅当 decision IS NULL 时写入，并发竞争时最多一个成功
                    res = await db.execute(
                        update(AgentApproval)
                        .where(AgentApproval.id == row.id, AgentApproval.decision.is_(None))
                        .values(
                            decision=dtype,
                            message=dec.get("message"),
                            decided_by=user_id,
                            decided_at=datetime.utcnow(),
                            decision_status="approved" if dtype == "approve" else "rejected",
                            updated_at=datetime.utcnow(),
                        ),
                        execution_options={"synchronize_session": False},
                    )
                    if res.rowcount == 1:
                        continue  # 本次决策写入成功
                    # CAS 未命中：已有决策。相同=幂等，不同=冲突
                    cur = (await db.execute(
                        select(AgentApproval.decision).where(AgentApproval.id == row.id)
                    )).scalar_one_or_none()
                    if cur == dtype:
                        continue  # 重复一致决策：幂等，不重复写入
                    raise ApprovalConflict(
                        f"审批动作 {row.action_id or row.id} 已决策为 {cur}，与新决策 {dtype} 冲突"
                    )
                await db.commit()
        except (ApprovalInvalid, ApprovalConflict):
            raise
        except Exception as e:
            raise PersistenceError(f"run {run_id} 审批决策写入失败: {e}") from e

    async def reconcile_run(
        self,
        run_id: str,
        *,
        tool_call_id: Optional[str] = None,
        idempotency_key: Optional[str] = None,
        disposition: str = "unresolved",
        external_operation_id: Optional[str] = None,
        note: Optional[str] = None,
        reconciled_by: Optional[int] = None,
    ) -> Dict[str, Any]:
        """人工核对（P1-03 §11 D）：``needs_reconciliation`` 执行的管理员处置。

        - 三处置 ``confirmed_succeeded`` / ``confirmed_not_executed`` / ``unresolved``；
        - 条件状态迁移（仅从 ``needs_reconciliation`` 出发，专用迁移口）、审计留痕
          （核对人/时间/处置/外部 operation ID/说明）；
        - **Checkpoint 丢失时不得自动重新推理**生成新工具调用：本方法从不驱动图执行；
          ``confirmed_not_executed``（允许安全恢复）在 Checkpoint 丢失时仅标记可重试，
          并返回 ``auto_recovery_allowed=False``，要求明确的人工重启策略或创建新 Run。
        """
        if disposition not in (
            "confirmed_succeeded", "confirmed_not_executed", "unresolved"
        ):
            raise ApprovalInvalid(
                f"未知核对处置: {disposition!r}"
                "（仅支持 confirmed_succeeded / confirmed_not_executed / unresolved）"
            )
        from sqlalchemy import select
        from app.models.agent import AgentToolExecution
        from app.services.agent_runtime.repositories.tool_execution_repository import ToolExecutionRepository

        async with AsyncSessionLocal() as db:
            repo = ToolExecutionRepository(db)
            rec: Optional[Any] = None
            if tool_call_id:
                rec = await repo.get(run_id, tool_call_id)
            elif idempotency_key:
                rec = (await db.execute(
                    select(AgentToolExecution).where(
                        AgentToolExecution.run_id == run_id,
                        AgentToolExecution.idempotency_key == idempotency_key,
                    )
                )).scalars().first()
            if rec is None:
                raise ApprovalInvalid("未找到待核对的工具执行记录")
            was_needs_recon = rec.status == "needs_reconciliation"
            pre_status = rec.status
            rec_call_id = rec.tool_call_id
            rec_idem = rec.idempotency_key
            hit = await repo.reconcile(
                rec.id,
                disposition=disposition,
                external_operation_id=external_operation_id,
                note=note,
                reconciled_by=reconciled_by,
            )
            await db.commit()
            result_status = pre_status if not hit else {
                "confirmed_succeeded": "succeeded",
                "confirmed_not_executed": "prepared",
                "unresolved": "needs_reconciliation",
            }[disposition]

        # Checkpoint 状态（仅作"可否安全自动恢复"判定，绝不触发重新推理）
        state = await self.get_thread_state(
            (await self._run_thread_id(run_id)) or ""
        )
        checkpoint_lost = state is None or bool(state.get("error"))
        # 仅 confirmed_not_executed 会导向恢复/重试；Checkpoint 丢失则不得自动恢复
        auto_recovery_allowed = (
            disposition == "confirmed_not_executed" and not checkpoint_lost
        )
        return {
            "run_id": run_id,
            "tool_call_id": rec_call_id,
            "idempotency_key": rec_idem,
            "disposition": disposition,
            "matched": hit,
            "was_needs_reconciliation": was_needs_recon,
            "status": result_status,
            "external_operation_id": external_operation_id,
            "checkpoint_lost": checkpoint_lost,
            "auto_recovery_allowed": auto_recovery_allowed,
            "message": (
                "已记录核对处置（未触发任何重新推理）"
                + ("；Checkpoint 丢失，需明确人工重启策略或创建新 Run，不得自动重新推理"
                   if (disposition == "confirmed_not_executed" and checkpoint_lost) else "")
            ),
        }
