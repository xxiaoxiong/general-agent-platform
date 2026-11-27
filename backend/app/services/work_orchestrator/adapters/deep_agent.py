"""DeepAgents 执行器适配器（general/research/data_analysis/database/coding/business_action）。

复用现有 Agent 工厂、工具策略、Tool Ledger、Checkpoint、HITL 与预算；
不复制 RuntimeContext / RunWorker / EventBus。
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from app.services.agent_runtime.completion import _collect_evidence
from app.services.agent_runtime.context import ModelUsageCallback, messages_to_langchain
from app.services.agent_runtime.lifecycle import (
    STATUS_WAITING_APPROVAL, STATUS_WAITING_INPUT,
)
from app.services.agent_runtime.repositories import PersistenceError
from app.services.agent_runtime.repositories.errors import RunLeaseLost
from app.services.agent_runtime.tools.policy import (
    BudgetExceededError, ToolPolicyError,
)

from ..schemas import (
    WorkArtifactRef, WorkPlanRecord, WorkStep, WorkStepOutcome,
)
from .base import (
    StepRuntimeEnv, build_step_messages, collect_step_artifacts, load_step_text,
    summarize_outcome, verify_deep_step,
)

logger = logging.getLogger(__name__)


class DeepAgentAdapter:
    """全部通用能力域都由同一个 DeepAgents 引擎承接，只是执行配置不同。"""

    async def execute(
        self,
        step: WorkStep,
        record: WorkPlanRecord,
        env: StepRuntimeEnv,
        *,
        resume: dict[str, Any] | None = None,
        attempt: int = 1,
    ) -> WorkStepOutcome:
        from app.services.agent_runtime.service import build_agent
        from app.services.agent_runtime.persistence import make_run_config as _cfg
        from app.observability.langfuse_tracing import make_callback_handler
        from langgraph.types import Command

        run = env.run
        ctx = env.ctx
        thread = f"work:{run.run_id}:step:{step.id}" if attempt <= 1 else (
            f"work:{run.run_id}:step:{step.id}:attempt:{attempt}"
        )
        previous_step = ctx.work_step_id
        previous_contract = ctx.task_contract
        previous_review = ctx.completion_review
        base_parts = list(run.final_text_parts)
        ctx.work_step_id = step.id
        # 步骤级验收由编排层确定性完成；步骤内部不再挂 Run 级 CompletionMiddleware，
        # 避免把整体 TaskContract 的返工循环带进单一步骤。
        ctx.task_contract = None
        ctx.completion_review = None
        step_text = ""
        try:
            agent = build_agent(
                ctx, env.policy, env.bus, env.provider, env.model_config,
                env.tool_specs, checkpointer=env.checkpointer, store=env.store,
            )
            config = _cfg(thread, run.run_id, env.user_id)
            callbacks: list[Any] = [ModelUsageCallback(ctx)]
            lf_handler = make_callback_handler(run.run_id)
            if lf_handler is not None:
                callbacks.append(lf_handler)
            config["callbacks"] = callbacks
            config["recursion_limit"] = max(
                ctx.budget.max_steps, ctx.budget.max_model_calls, 100, 1
            ) * 16 + 40

            state = await agent.aget_state(config)
            step_messages = build_step_messages(step, record, env)
            if state.interrupts and resume:
                payload: Any = Command(resume=resume)
                run.final_text_parts = [await load_step_text(run.run_id, step.id)]
            elif state.interrupts:
                # 恢复检查已把 Run 放回待审批；这里不自动重放
                return WorkStepOutcome(
                    step_id=step.id, status="waiting_approval", paused=True,
                    summary="步骤等待人工审批", checkpoint_ref=thread,
                )
            elif state.values or state.next:
                payload = None
                run.final_text_parts = [await load_step_text(run.run_id, step.id)]
            else:
                run.final_text_parts = []
                skills: dict[str, Any] = {}
                try:
                    from app.core.database import AsyncSessionLocal

                    async with AsyncSessionLocal() as db:
                        skills = await env.service._skill_files(db, run.agent_id)
                except Exception:  # noqa: BLE001 技能装载失败不阻断步骤
                    logger.warning("步骤 %s 技能装载失败（忽略）", step.id, exc_info=True)
                payload = {
                    "messages": messages_to_langchain(step_messages, namespace=thread),
                    "todos": [],
                    **skills,
                }

            await env.service._consume_stream(agent, payload, config, ctx, run, env.bus)
            if run.status in (STATUS_WAITING_APPROVAL, STATUS_WAITING_INPUT):
                await env.bus.flush_deltas()
                return WorkStepOutcome(
                    step_id=step.id, status="waiting_approval", paused=True,
                    summary="步骤等待审批或补充输入", checkpoint_ref=thread,
                )
            step_text = "".join(run.final_text_parts)
            evidence, artifacts, external = await _collect_evidence(ctx)
            verification = verify_deep_step(
                step=step, env=env, final_text=step_text,
                evidence=evidence, external_handled=external,
            )
            artifact_refs = [
                WorkArtifactRef(
                    path=str(item.get("path") or ""),
                    name=str(item.get("name") or ""),
                    size=int(item.get("size") or 0),
                    sha256=str(item.get("sha256") or ""),
                )
                for item in artifacts if item.get("path")
            ]
            artifact_refs.extend(await collect_step_artifacts(run.run_id, step.id))
            verification.artifact_refs = artifact_refs
            status = "completed" if verification.status in ("passed", "partial") else "failed"
            return WorkStepOutcome(
                step_id=step.id,
                status=status,
                summary=summarize_outcome(step, verification, artifact_refs),
                final_text=step_text[:60_000],
                artifact_refs=artifact_refs,
                evidence_refs=verification.evidence_refs,
                verification=verification,
                error_code="" if status == "completed" else "step_verification_failed",
                retryable=status == "failed" and verification.repairable,
                checkpoint_ref=thread,
            )
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError:
            raise
        except (RunLeaseLost, PersistenceError):
            raise
        except (BudgetExceededError, ToolPolicyError) as exc:
            logger.info("步骤 %s 被预算/策略终止: %s", step.id, exc)
            return WorkStepOutcome(
                step_id=step.id, status="failed",
                summary=f"步骤被预算或安全策略终止：{str(exc)[:300]}",
                error_code="budget_exceeded" if isinstance(exc, BudgetExceededError) else "policy_rejected",
                retryable=False, checkpoint_ref=thread,
            )
        except Exception as exc:  # noqa: BLE001 步骤失败不丢弃已完成步骤
            logger.warning("步骤 %s 执行失败: %s", step.id, exc, exc_info=True)
            return WorkStepOutcome(
                step_id=step.id, status="failed",
                summary=f"步骤执行失败：{type(exc).__name__}: {str(exc)[:300]}",
                error_code="step_execution_error", retryable=True, checkpoint_ref=thread,
            )
        finally:
            step_text = step_text or "".join(run.final_text_parts)
            if step_text:
                env.step_outputs[step.id] = step_text
            run.final_text_parts = base_parts
            ctx.work_step_id = previous_step
            ctx.task_contract = previous_contract
            ctx.completion_review = previous_review
