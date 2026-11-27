"""Document Graph 执行器适配器：Word 生成/编辑由既有文档工作流承接。"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from app.services.agent_runtime.completion import _collect_evidence
from app.services.agent_runtime.document_graph import DocumentGraphContext, drive_document_graph
from app.services.agent_runtime.lifecycle import (
    STATUS_WAITING_APPROVAL, STATUS_WAITING_INPUT,
)
from app.services.agent_runtime.persistence import make_run_config
from app.services.agent_runtime.repositories import PersistenceError
from app.services.agent_runtime.repositories.errors import RunLeaseLost
from app.services.agent_runtime.tools.policy import BudgetExceededError, ToolPolicyError
from app.services.documents.errors import DocumentError
from app.services.documents.graphs import build_document_graph

from ..schemas import (
    WorkCheck, WorkPlanRecord, WorkStep, WorkStepOutcome, WorkVerificationResult,
)
from .base import (
    StepRuntimeEnv, collect_step_artifacts, prior_outputs_text, summarize_outcome,
)

logger = logging.getLogger(__name__)


class DocumentAdapter:
    async def execute(
        self,
        step: WorkStep,
        record: WorkPlanRecord,
        env: StepRuntimeEnv,
        *,
        resume: dict[str, Any] | None = None,
        attempt: int = 1,
    ) -> WorkStepOutcome:
        run = env.run
        ctx = env.ctx
        thread = f"work:{run.run_id}:step:{step.id}" if attempt <= 1 else (
            f"work:{run.run_id}:step:{step.id}:attempt:{attempt}"
        )
        previous_step = ctx.work_step_id
        ctx.work_step_id = step.id
        base_parts = list(run.final_text_parts)
        try:
            prior_text = prior_outputs_text(step, record, env)
            request = self._document_request(step, record, env)
            route = {
                "task_type": "document_generate",
                "task_mode": "document_generate",
                "intent": "document_generate",
                "deliverable_format": "docx",
                "decision_goal": record.plan.objective,
                "reason": "WorkPlan 文档交付步骤",
                "document_context": {
                    "request": request,
                    "draft": prior_text[:160_000],
                    "export_existing": False,
                    "autonomous": True,
                    "template_paths": [],
                },
            }
            messages = [
                {"role": "user", "content": request},
                *([{"role": "assistant", "content": prior_text}] if prior_text else []),
            ]
            context = DocumentGraphContext(
                run, ctx, env.provider, env.model_config, env.policy, env.tool_specs,
                env.service._flush_budget,
            )
            graph = build_document_graph(env.checkpointer, context)
            config = make_run_config(thread, run.run_id, env.user_id)
            config["recursion_limit"] = 400
            from app.services.agent_runtime.context import ModelUsageCallback

            config["callbacks"] = [ModelUsageCallback(ctx)]
            snapshot = await graph.aget_state(config)
            payload: Any = {"messages": messages, "execution_context": route}
            if snapshot.interrupts or snapshot.next:
                payload = None
            elif snapshot.values:
                payload = None
            run.final_text_parts = []
            await drive_document_graph(graph, config, payload, context)
            if run.status in (STATUS_WAITING_APPROVAL, STATUS_WAITING_INPUT):
                return WorkStepOutcome(
                    step_id=step.id, status="waiting_approval", paused=True,
                    summary="文档步骤等待人工输入", checkpoint_ref=thread,
                )
            final_text = "".join(run.final_text_parts)
            state = await graph.aget_state(config)
            validation = (state.values or {}).get("validation") or {}
            artifact_refs = await collect_step_artifacts(run.run_id, step.id)
            evidence, _artifacts, external = await _collect_evidence(ctx)
            checks = [
                WorkCheck(
                    id="answer_present", passed=bool(final_text.strip()),
                    detail="" if final_text.strip() else "文档步骤没有产出最终说明",
                ),
                WorkCheck(
                    id="document_validation",
                    passed=bool(validation.get("passed", run.summary.get("document_validation_passed", False))),
                    detail="；".join(
                        str(item.get("message") or item.get("id") or "")
                        for item in (validation.get("issues") or [])[:4]
                        if isinstance(item, dict)
                    )[:600],
                ),
                WorkCheck(
                    id="artifact_published", passed=bool(artifact_refs),
                    detail="" if artifact_refs else "文档未生成可下载版本",
                ),
            ]
            if external:
                checks.append(WorkCheck(
                    id="external_operation", passed=False,
                    detail="文档步骤执行了外部副作用，需要人工核对",
                ))
            failed = [item for item in checks if not item.passed]
            status = "passed" if not failed else "partial"
            if not artifact_refs:
                status = "failed"
            verification = WorkVerificationResult(
                status=status, checks=checks,
                evidence_refs=[f"tool:{item.tool_name}" for item in evidence][:16],
                artifact_refs=artifact_refs,
                repairable=status == "failed" and not external,
                stop_reason="external_operation" if external else "",
            )
            step_status = "completed" if status in ("passed", "partial") else "failed"
            return WorkStepOutcome(
                step_id=step.id, status=step_status,
                summary=summarize_outcome(step, verification, artifact_refs),
                final_text=final_text[:60_000],
                artifact_refs=artifact_refs,
                evidence_refs=verification.evidence_refs,
                verification=verification,
                error_code="" if step_status == "completed" else "document_verification_failed",
                retryable=status == "failed" and not external,
                checkpoint_ref=thread,
            )
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError:
            raise
        except (RunLeaseLost, PersistenceError):
            raise
        except DocumentError as exc:
            logger.info("文档步骤 %s 失败: %s", step.id, exc.message)
            return WorkStepOutcome(
                step_id=step.id, status="failed", summary=exc.message[:600],
                error_code=getattr(exc, "code", "") or "document_error",
                retryable=int(getattr(exc, "status_code", 500) or 500) >= 500,
                checkpoint_ref=thread,
            )
        except (BudgetExceededError, ToolPolicyError) as exc:
            return WorkStepOutcome(
                step_id=step.id, status="failed",
                summary=f"文档步骤被预算或安全策略终止：{str(exc)[:300]}",
                error_code="budget_exceeded" if isinstance(exc, BudgetExceededError) else "policy_rejected",
                retryable=False, checkpoint_ref=thread,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("文档步骤 %s 执行失败: %s", step.id, exc, exc_info=True)
            return WorkStepOutcome(
                step_id=step.id, status="failed",
                summary=f"文档步骤执行失败：{type(exc).__name__}: {str(exc)[:300]}",
                error_code="document_step_error", retryable=True, checkpoint_ref=thread,
            )
        finally:
            run.final_text_parts = base_parts
            ctx.work_step_id = previous_step

    @staticmethod
    def _document_request(step: WorkStep, record: WorkPlanRecord, env: StepRuntimeEnv) -> str:
        parts = [f"总体目标：{record.plan.objective}", f"本步骤：{step.objective}"]
        for item in record.plan.constraints[:6]:
            parts.append(f"约束：{item}")
        original = ""
        for message in reversed(env.messages or []):
            if isinstance(message, dict) and message.get("role") == "user":
                original = str(message.get("content") or "").split("\n\n[附件]", 1)[0].strip()
                break
        if original:
            parts.append(f"用户原始要求：{original[:4000]}")
        return "\n\n".join(parts)
