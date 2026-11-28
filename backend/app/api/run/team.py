"""团队运行快照 API（断线重连 / 服务端事实恢复）。

前端事件流仍以 team.updated 为准；本接口用于重连后从数据库读取团队、
成员与任务板的当前快照，不依赖浏览器内存事件。
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.core.database import get_db
from app.models.agent import AgentRun
from app.models.user import User
from app.services.agent_runtime.team.runtime.team_service import TeamService
from app.services.agent_runtime.team.services.task_board import TaskBoardService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/runs", tags=["runs-v2"])


@router.get("/{run_id}/team")
async def get_team_snapshot(
    run_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """返回团队/成员/任务板快照（仅 Run 所有者可读，跨用户统一 404）。"""
    owns = await db.scalar(
        select(AgentRun.id).where(
            AgentRun.id == run_id,
            AgentRun.user_id == current_user.id,
        )
    )
    if owns is None:
        raise HTTPException(status_code=404, detail="运行不存在")
    try:
        team = await TeamService().get(run_id)
        if team is None:
            return {"team": None, "phase": None, "tasks": [], "members": []}
        board = await TaskBoardService().snapshot(
            run_id, phase=str(team.get("phase") or "working"),
        )
        return {"team": team, **board}
    except Exception:  # noqa: BLE001 团队表缺失/迁移未执行：按无团队快照处理
        logger.warning("run %s 团队快照读取失败", run_id, exc_info=True)
        raise HTTPException(status_code=409, detail="团队运行数据不可用（请检查数据库迁移）")
