"""Team Graph / Team Project Graph 执行器适配器。

两者都复用现有 ``TeamRun``，只把 Checkpoint 线程切换为步骤级，避免与直接
Team Run 的状态串扰（任务书 §8.2）。
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from app.services.agent_runtime.completion import _collect_evidence
from app.services.agent_runtime.lifecycle import (
    STATUS_WAITING_APPROVAL, STATUS_WAITING_INPUT,
)
from app.services.agent_runtime.repositories import PersistenceError
from app.services.agent_runtime.repositories.errors import RunLeaseLost
from app.services.agent_runtime.tools.policy import BudgetExceededError, ToolPolicyError

from ..schemas import (
    WorkArtifactRef, WorkCheck, WorkPlanRecord, WorkStep, WorkStepOutcome,
    WorkVerificationResult,
)
from .base import (
    StepRuntimeEnv, build_step_prompt, collect_step_artifacts, evidence_refs,
    summarize_outcome,
)

logger = logging.getLogger(__name__)


class _TeamAdapterBase:
    async def _run_team(
        self,
        step: WorkStep,
        record: WorkPlanRecord,
        env: StepRuntimeEnv,
        *,
        attempt: int,
        project: bool,
    ) -> WorkStepOutcome:
        from app.services.agent_runtime.team_runtime import TeamRun
        from app.services.agent_runtime.team_graph import TeamExecutionError

        run = env.run
        ctx = env.ctx
        thread = f"work:{run.run_id}:step:{step.id}" if attempt <= 1 else (
            f"work:{run.run_id}:step:{step.id}:attempt:{attempt}"
        )
        previous_step = ctx.work_step_id
        ctx.work_step_id = step.id
        base_parts = list(run.final_text_parts)
        try:
            team = TeamRun(
                run, ctx, env.provider, env.model_config, env.policy, env.tool_specs,
                env.service._flush_budget,
                project_scope=(env.route.get("project_scope") or "fullstack") if project else None,
                thread_id=thread,
            )
            messages = [{"role": "user", "content": build_step_prompt(step, record, env)}]
            route = {
                **env.route,
                "document_context": {"request": step.objective},
                "project_scope": team.project_scope,
            }
            outcome = await team.drive(env.checkpointer, messages, route, synthesize=True)
            if run.status in (STATUS_WAITING_APPROVAL, STATUS_WAITING_INPUT):
                return WorkStepOutcome(
                    step_id=step.id, status="waiting_approval", paused=True,
                    summary="团队步骤等待人工输入", checkpoint_ref=thread,
                )
            board = outcome.get("team") or {}
            answer = str(outcome.get("final_text") or "")
            if answer:
                run.final_text_parts = [answer]
                env.step_outputs[step.id] = answer
            failure = str(outcome.get("failure") or "")
            project_info = outcome.get("project") or {}
            artifact_refs: list[WorkArtifactRef] = []
            if project_info.get("download_url"):
                artifact_refs.append(WorkArtifactRef(
                    path=f"project:{run.run_id}:project.zip",
                    name=str(project_info.get("filename") or "project.zip"),
                    size=int(project_info.get("size") or 0),
                    download_url=str(project_info.get("download_url") or ""),
                ))
            artifact_refs.extend(await collect_step_artifacts(run.run_id, step.id))
            checks = [
                WorkCheck(id="answer_present", passed=bool(answer.strip()),
                          detail="" if answer.strip() else "步骤没有产出最终说明"),
                WorkCheck(
                    id="team_completed", passed=board.get("phase") == "completed",
                    detail=str(board.get("summary") or failure)[:600],
                ),
            ]
            if project:
                checks.append(WorkCheck(
                    id="project_passed", passed=project_info.get("state") == "passed",
                    detail=str(project_info.get("state") or failure)[:600],
                ))
                for item in (project_info.get("checks") or [])[:24]:
                    if isinstance(item, dict) and item.get("id"):
                        checks.append(WorkCheck(
                            id=str(item["id"])[:80], passed=bool(item.get("passed")),
                            detail=str(item.get("detail") or "")[:600],
                        ))
            failed = [item for item in checks if not item.passed]
            status = "passed" if not failed else "partial"
            if not answer.strip() or board.get("phase") == "failed" or (project and project_info.get("state") != "passed"):
                status = "failed"
            _evidence, _artifacts, external = await _collect_evidence(ctx)
            verification = WorkVerificationResult(
                status=status, checks=checks,
                evidence_refs=evidence_refs(_evidence),
                artifact_refs=artifact_refs,
                repairable=status == "failed" and not external,
                stop_reason="external_operation" if external else "",
            )
            step_status = "completed" if status in ("passed", "partial") else "failed"
            return WorkStepOutcome(
                step_id=step.id, status=step_status,
                summary=summarize_outcome(step, verification, artifact_refs),
                final_text=answer[:60_000],
                artifact_refs=artifact_refs,
                evidence_refs=verification.evidence_refs,
                verification=verification,
                error_code="" if step_status == "completed" else "team_step_failed",
                retryable=status == "failed" and not external,
                checkpoint_ref=thread,
            )
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError:
            raise
        except (RunLeaseLost, PersistenceError):
            raise
        except TeamExecutionError as exc:
            return WorkStepOutcome(
                step_id=step.id, status="failed", summary=str(exc)[:600],
                error_code="team_execution_error", retryable=True, checkpoint_ref=thread,
            )
        except (BudgetExceededError, ToolPolicyError) as exc:
            return WorkStepOutcome(
                step_id=step.id, status="failed",
                summary=f"团队步骤被预算或安全策略终止：{str(exc)[:300]}",
                error_code="budget_exceeded" if isinstance(exc, BudgetExceededError) else "policy_rejected",
                retryable=False, checkpoint_ref=thread,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("团队步骤 %s 执行失败: %s", step.id, exc, exc_info=True)
            return WorkStepOutcome(
                step_id=step.id, status="failed",
                summary=f"团队步骤执行失败：{type(exc).__name__}: {str(exc)[:300]}",
                error_code="team_step_error", retryable=True, checkpoint_ref=thread,
            )
        finally:
            run.final_text_parts = base_parts
            ctx.work_step_id = previous_step


class TeamAdapter(_TeamAdapterBase):
    async def execute(self, step, record, env, *, resume=None, attempt=1):
        return await self._run_team(step, record, env, attempt=attempt, project=False)


class ProjectAdapter(_TeamAdapterBase):
    async def execute(self, step, record, env, *, resume=None, attempt=1):
        return await self._run_team(step, record, env, attempt=attempt, project=True)
