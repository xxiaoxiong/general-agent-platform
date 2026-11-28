"""TeamCollaboration：把 Team Service / Task Board / Message Bus / Artifact Store /
Member Runtime / Leader Runner 组装为一个团队协作运行时。

职责边界：
- LangGraph Team Controller 只做控制流（规划/调度/复核/综合），不直接写 SQL；
- 这里提供业务服务与成员运行时，所有事实落库；
- 软件项目团队（team_project_graph）不强制迁移，仍走原工程图。
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from app.core.config import settings

from ..errors import TeamError
from ..services.artifact_store import ArtifactStore
from ..services.message_bus import MessageBusService
from ..services.task_board import TaskBoardService
from ..states import MEMBER_ACTIVE
from .capacity import ModelCapacityLimiter, get_capacity_limiter
from .leader_agent import LeaderRunner
from .member_directory import MemberDirectory
from .member_manager import MemberRuntimeManager
from .team_service import TeamService

logger = logging.getLogger("ai_plat.team")


class TeamCollaboration:
    def __init__(
        self,
        *,
        team_run_id: str,
        run_id: str,
        team: TeamService,
        board: TaskBoardService,
        messages: MessageBusService,
        artifacts: ArtifactStore,
        directory: MemberDirectory,
        members: MemberRuntimeManager,
        leader: LeaderRunner,
        max_members: int,
        max_parallel: int,
        max_tasks: int,
        team_config: dict[str, Any],
    ):
        self.team_run_id = team_run_id
        self.run_id = run_id
        self.team = team
        self.board = board
        self.messages = messages
        self.artifacts = artifacts
        self.directory = directory
        self.members = members
        self.leader = leader
        self.max_members = max_members
        self.max_parallel = max_parallel
        self.max_tasks = max_tasks
        self.team_config = team_config

    @property
    def leader_member_id(self) -> str:
        return self.members.leader_member_id

    async def snapshot(
        self,
        *,
        phase: str,
        running: Any = (),
        summary: str = "",
        lead: str = "团队负责人",
    ) -> dict[str, Any]:
        try:
            await self.team.update(self.team_run_id, phase=phase)
        except Exception:  # noqa: BLE001 阶段镜像失败不影响快照返回
            logger.debug("团队阶段持久化失败", exc_info=True)
        board = await self.board.snapshot(
            self.team_run_id, phase=phase, lead=lead,
            running=running, summary=summary, include_members=True,
        )
        return board

    async def finish(self, phase: str, *, summary: str = "") -> None:
        status = {
            "completed": "completed",
            "failed": "failed",
            "cancelled": "cancelled",
        }.get(phase, "running")
        await self.team.update(
            self.team_run_id,
            phase=phase,
            status=status,
            finished=status != "running",
        )

    async def cancel_members(self, reason: str = "团队已取消") -> None:
        for member in await self.directory.list_members(self.team_run_id):
            if member["status"] in MEMBER_ACTIVE:
                await self.members.cancel(member["member_id"], reason)

    async def wake_idle_members(self, *, max_count: int = 2) -> list[Any]:
        return await self.members.wake_idle_members(max_count=max_count)


async def create_team_collaboration(
    *,
    run: Any,
    ctx: Any,
    provider: dict[str, Any],
    model_config: dict[str, Any],
    policy: Any,
    tool_specs: list[dict[str, Any]],
    bus: Any,
    checkpointer: Any,
    store: Any = None,
    flush_budget: Any = None,
    objective: str,
    mode: str = "team",
    team_config: Optional[dict[str, Any]] = None,
    capacity: Optional[ModelCapacityLimiter] = None,
) -> TeamCollaboration:
    """组装团队协作运行时；初始化失败向上抛出（由调用方结束 Run）。"""
    team_config = dict(team_config or {})
    max_members = max(1, min(
        int(team_config.get("max_members") or settings.AGENT_TEAM_MAX_MEMBERS),
        int(settings.AGENT_TEAM_MAX_MEMBERS),
    ))
    max_parallel = max(1, min(
        int(team_config.get("max_parallel") or settings.AGENT_TEAM_MAX_PARALLEL),
        int(settings.AGENT_TEAM_MAX_PARALLEL),
        max_members,
    ))
    max_tasks = max(2, min(
        int(team_config.get("max_tasks") or settings.AGENT_TEAM_MAX_TASKS),
        int(settings.AGENT_TEAM_MAX_TASKS),
        max_members,
    ))
    team_service = TeamService()
    await team_service.ensure(
        team_run_id=run.run_id,
        objective=objective,
        mode=mode,
        max_members=max_members,
        max_parallel=max_parallel,
        config_snapshot={
            "team_config": _sanitize_config(team_config),
            "max_members": max_members,
            "max_parallel": max_parallel,
            "max_tasks": max_tasks,
            "runtime": "deepagents_member_runtime",
        },
    )
    board = TaskBoardService()
    messages = MessageBusService()
    artifacts = ArtifactStore()
    directory = MemberDirectory()
    limiter = capacity or get_capacity_limiter()
    import asyncio

    manager = MemberRuntimeManager(
        team_run_id=run.run_id,
        run_id=run.run_id,
        parent_ctx=ctx,
        provider=provider,
        model_config=model_config,
        policy=policy,
        tool_specs=tool_specs,
        bus=bus,
        board=board,
        messages=messages,
        artifacts=artifacts,
        directory=directory,
        checkpointer=checkpointer,
        store=store,
        flush_budget=flush_budget,
        capacity=limiter,
        usage_lock=asyncio.Lock(),
        team_config=team_config,
        leader_member_id="leader",
        run_handle=run,
    )
    leader = LeaderRunner(
        manager=manager,
        team_run_id=run.run_id,
        objective=objective,
        assistant_prompt=str(ctx.agent_config.get("system_prompt") or ""),
        team_config=team_config,
    )
    leader_member = await leader.ensure()
    await team_service.update(
        run.run_id, leader_member_id=leader_member["member_id"],
    )
    return TeamCollaboration(
        team_run_id=run.run_id,
        run_id=run.run_id,
        team=team_service,
        board=board,
        messages=messages,
        artifacts=artifacts,
        directory=directory,
        members=manager,
        leader=leader,
        max_members=max_members,
        max_parallel=max_parallel,
        max_tasks=max_tasks,
        team_config=team_config,
    )


def _sanitize_config(team_config: dict[str, Any]) -> dict[str, Any]:
    """团队配置快照：去掉疑似密钥字段，避免落库敏感信息。"""
    from ...events import redact_secrets

    try:
        return redact_secrets(team_config)
    except Exception:  # noqa: BLE001
        return {}
