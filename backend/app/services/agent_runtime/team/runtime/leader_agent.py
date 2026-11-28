"""LeaderRunner：把团队负责人作为完整 DeepAgents Agent 运行。

- 同一 Thread 贯穿规划、复核、综合，保留上下文；
- 规划阶段通过 Team Tools 在 Task Board 创建任务（不是一次性 JSON 包装）；
- 复核/综合阶段通过 Team Tools 读取真实成员成果（Artifact 引用）；
- 预算按 Leader Profile 累计，用量同步到父 Run。
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from ...tools.policy import BudgetExceededError
from ..states import MEMBER_COMPLETED, MEMBER_FAILED, MEMBER_INTERRUPTED, MEMBER_READY, MEMBER_RUNNING, MEMBER_WAITING
from .member_context import MemberExecutionContext
from .member_manager import MemberRuntimeManager
from .member_profile import MemberProfile

logger = logging.getLogger("ai_plat.team")


class LeaderRunner:
    def __init__(
        self,
        *,
        manager: MemberRuntimeManager,
        team_run_id: str,
        objective: str,
        assistant_prompt: str = "",
        team_config: Optional[dict[str, Any]] = None,
    ):
        self.manager = manager
        self.team_run_id = team_run_id
        self.objective = objective
        self.assistant_prompt = assistant_prompt
        self.profile = MemberProfile.leader(team_config=team_config)
        self._member: Optional[dict[str, Any]] = None

    async def ensure(self) -> dict[str, Any]:
        if self._member is None:
            self._member = await self.manager.ensure_member(
                role="团队负责人",
                profile=self.profile,
                is_leader=True,
            )
        return self._member

    @property
    def member(self) -> dict[str, Any]:
        return self._member or {}

    async def invoke(
        self,
        stage: str,
        instructions: str,
        user: str,
        *,
        stage_model_limit: Optional[int] = None,
        stage_tool_limit: Optional[int] = None,
    ) -> tuple[str, bool]:
        """返回 (text, truncated)。异常原样抛出，由 Controller 决定失败语义。"""
        member = await self.ensure()
        if member["status"] in {MEMBER_COMPLETED, MEMBER_FAILED, "cancelled"}:
            # 负责人 Thread 已结束但团队仍需继续：恢复为 ready（显式重排）
            await self.manager._set_member_status(
                member["member_id"], MEMBER_READY,
                sources=frozenset({MEMBER_COMPLETED, MEMBER_FAILED, "cancelled"}),
            )
            member = await self.manager.get_member(member["member_id"]) or member
        await self.manager._set_member_status(
            member["member_id"], MEMBER_RUNNING,
            sources=frozenset({MEMBER_READY, MEMBER_WAITING, MEMBER_INTERRUPTED}),
            started_at=None,
        )
        execution = MemberExecutionContext(
            team_run_id=self.team_run_id,
            member_id=member["member_id"],
            role="团队负责人",
            thread_id=member["thread_id"],
            task_id=None,
            objective=self.objective,
            assistant_prompt=self.assistant_prompt,
            task_brief="",
            profile=self.profile,
        )
        usage = {"model_calls": 0, "tool_calls": 0, "tokens": 0}
        # 阶段 Thread 隔离：规划保留完整上下文；复核/综合使用独立、稳定的阶段线程，
        # 避免规划阶段的未完成 Checkpoint 被后续阶段误续跑（综合事实来自 Task Board/Artifact）。
        stage_thread = (
            member["thread_id"] if stage == "planning"
            else f"{member['thread_id']}:{stage}"
        )
        try:
            outcome = await self.manager._run_agent(
                member=member,
                execution=execution,
                profile=self.profile,
                is_leader=True,
                usage=usage,
                resume_input=f"【{stage}】\n{instructions}\n\n{user}",
                stage_model_limit=stage_model_limit,
                stage_tool_limit=stage_tool_limit,
                thread_id=stage_thread,
            )
        except Exception as exc:  # noqa: BLE001
            from ...repositories.errors import PersistenceError
            from langgraph.errors import GraphBubbleUp

            target = (
                MEMBER_INTERRUPTED
                if isinstance(exc, (PersistenceError, GraphBubbleUp, BudgetExceededError))
                else MEMBER_FAILED
            )
            await self.manager._finish_member(
                member["member_id"], target, usage,
                last_error=f"{type(exc).__name__}: {exc}"[:2000],
            )
            await self.manager._flush_usage(usage)
            await self.manager._emit_member(member["member_id"], reason=stage)
            raise
        await self.manager._finish_member(
            member["member_id"], MEMBER_READY, usage, last_error=None,
        )
        await self.manager._flush_usage(usage)
        await self.manager._emit_member(member["member_id"], reason=stage)
        return outcome.text, outcome.truncated

    async def cancel(self, reason: str = "团队已取消") -> None:
        if self._member is not None:
            await self.manager.cancel(self._member["member_id"], reason)

    async def rotate_thread(self, reason: str = "") -> str:
        """规划阶段被预算/限流中断后切换新的 Checkpoint 代际。

        旧线程可能停留在“待继续”的规划节点，若后续综合直接续跑会复用未完成
        的规划输出。成员身份不变，仅 Thread 进入新代际；综合/复核从新线程开始，
        成员成果仍通过 Task Board / Artifact 引用。
        """
        import uuid

        from app.core.database import AsyncSessionLocal

        from ..repositories import MemberRepository

        member = await self.ensure()
        new_thread = f"{member['thread_id']}:r{uuid.uuid4().hex[:8]}"
        async with AsyncSessionLocal() as db:
            await MemberRepository(db).update_fields(
                self.team_run_id, member["member_id"],
                thread_id=new_thread, last_error=(reason[:2000] or None),
            )
            await db.commit()
        self._member = await self.manager.get_member(member["member_id"])
        return new_thread
