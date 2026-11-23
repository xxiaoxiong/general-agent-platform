"""Capabilities API v2（§7 /api/v2/capabilities）。

能力包是平台领域抽象（§2.5）；授权收口在 resolve_capabilities
（模型可建议能力，但不能自行授权）。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends

from app.api.deps import get_current_user
from app.models.user import User

router = APIRouter(prefix="/capabilities", tags=["capabilities-v2"])


@router.get("")
async def list_capability_packs(current_user: User = Depends(get_current_user)):
    from app.services.assistant.capability import list_capabilities

    return {"items": list_capabilities()}
