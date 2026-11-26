"""Workspace API v2（§7）：默认助手 + 用户能力 + 工作区配置。

UX-01：新用户无需创建 Agent 即可对话——bootstrap 懒创建默认通用助手。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.core.config import settings
from app.core.database import get_db
from app.models.user import User

from app.api.common.serializers import assistant_to_dict

router = APIRouter(prefix="/workspace", tags=["workspace-v2"])


@router.get("/bootstrap")
async def workspace_bootstrap(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """登录后一次性获取：默认助手、可用能力包、工作区配置。

    默认助手按用户懒创建（幂等）；创建与读取同一请求内提交。
    """
    from app.services.assistant.capability import list_capabilities
    from app.services.assistant.default_assistant import ensure_default_assistant

    agent = await ensure_default_assistant(db, current_user.id, created_by=current_user.id)
    await db.commit()
    await db.refresh(agent)

    return {
        "user": {
            "id": current_user.id,
            "username": current_user.username,
            "is_superuser": bool(current_user.is_superuser),
        },
        "default_assistant": assistant_to_dict(agent),
        "capabilities": list_capabilities(),
        "workspace": {
            "file_upload_enabled": True,
            "memory_enabled": True,
            "hitl_enabled": True,
        },
        # Workspace Sandbox：前端据此显示 Preview / Services Tab 与项目选择
        "sandbox": {
            "enabled": _sandbox_enabled(current_user.id),
            "preview_domain": getattr(settings, "PREVIEW_BASE_DOMAIN", "localhost"),
            "default_profile": getattr(settings, "SANDBOX_DEFAULT_PROFILE", "standard"),
        },
    }


def _sandbox_enabled(user_id: int) -> bool:
    from app.services.sandbox.common import sandbox_enabled_for

    return sandbox_enabled_for(user_id)
