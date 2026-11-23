"""MCP 管理 API v2（扩展性治理：MCP 管理模块）。

- 读：任意登录用户；写（创建/修改/测试/删除）：仅超管；
- 连接测试（/test）真实建立 MCP 会话执行 initialize + tools/list，发现结果
  缓存到 discovered_tools（客观数据，不做任何加工判断）；
- MCP 工具命名空间 ``mcp__<server>__<tool>``，执行期 fail-closed：
  未启用 / 未绑定 Agent / 未发现的工具不进工具面，风险未登记即拒绝执行。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_superuser, get_current_user, get_db
from app.models.mcp_server import (
    SIDE_EFFECT_NON_IDEMPOTENT,
    SIDE_EFFECT_READ_ONLY,
    TRANSPORT_STDIO,
    TRANSPORT_STREAMABLE_HTTP,
    MCPServer,
)
from app.models.user import User

router = APIRouter(prefix="/mcp", tags=["mcp-v2"])

_VALID_TRANSPORTS = {TRANSPORT_STDIO, TRANSPORT_STREAMABLE_HTTP}
_VALID_SIDE_EFFECTS = {SIDE_EFFECT_READ_ONLY, SIDE_EFFECT_NON_IDEMPOTENT}


class McpServerCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    description: str = ""
    transport: str = TRANSPORT_STDIO
    command: Optional[str] = None
    args: List[str] = Field(default_factory=list)
    env: Dict[str, str] = Field(default_factory=dict)
    url: Optional[str] = None
    headers: Dict[str, str] = Field(default_factory=dict)
    enabled: bool = True
    require_approval: bool = True
    side_effect_class: str = SIDE_EFFECT_NON_IDEMPOTENT
    tool_settings: Dict[str, Any] = Field(default_factory=dict)
    agent_ids: List[int] = Field(default_factory=list)


class McpServerUpdate(BaseModel):
    description: Optional[str] = None
    transport: Optional[str] = None
    command: Optional[str] = None
    args: Optional[List[str]] = None
    env: Optional[Dict[str, str]] = None
    url: Optional[str] = None
    headers: Optional[Dict[str, str]] = None
    enabled: Optional[bool] = None
    require_approval: Optional[bool] = None
    side_effect_class: Optional[str] = None
    tool_settings: Optional[Dict[str, Any]] = None
    agent_ids: Optional[List[int]] = None


def _row_to_dict(s: MCPServer) -> Dict[str, Any]:
    return {
        "id": s.id,
        "name": s.name,
        "description": s.description or "",
        "transport": s.transport,
        "command": s.command,
        "args": list(s.args or []),
        "env": dict(s.env or {}),
        "url": s.url,
        "headers": dict(s.headers or {}),
        "enabled": bool(s.enabled),
        "require_approval": bool(s.require_approval),
        "side_effect_class": s.side_effect_class,
        "tool_settings": dict(s.tool_settings or {}),
        "discovered_tools": list(s.discovered_tools or []),
        "agent_ids": list(s.agent_ids or []),
        "last_test_at": s.last_test_at.isoformat() if s.last_test_at else None,
        "last_test_error": s.last_test_error,
        "created_at": s.created_at.isoformat() if s.created_at else None,
        "updated_at": s.updated_at.isoformat() if s.updated_at else None,
    }


def _validate_payload(transport: str, command: Optional[str], url: Optional[str]) -> None:
    if transport not in _VALID_TRANSPORTS:
        raise HTTPException(status_code=400, detail=f"不支持的 transport: {transport}")
    if transport == TRANSPORT_STDIO and not (command or "").strip():
        raise HTTPException(status_code=400, detail="stdio 传输必须配置 command")
    if transport == TRANSPORT_STREAMABLE_HTTP and not (url or "").strip():
        raise HTTPException(status_code=400, detail="streamable_http 传输必须配置 url")


def _connection_of(row: MCPServer):
    from app.services.mcp.client import McpConnectionConfig

    return McpConnectionConfig(
        transport=row.transport,
        command=row.command or "",
        args=list(row.args or []),
        env=dict(row.env or {}),
        url=row.url or "",
        headers=dict(row.headers or {}),
    )


@router.get("/servers")
async def list_servers(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    rows = (await db.execute(select(MCPServer).order_by(MCPServer.id))).scalars().all()
    return {"items": [_row_to_dict(s) for s in rows]}


@router.post("/servers")
async def create_server(
    payload: McpServerCreate,
    current_user: User = Depends(get_current_superuser),
    db: AsyncSession = Depends(get_db),
):
    _validate_payload(payload.transport, payload.command, payload.url)
    if payload.side_effect_class not in _VALID_SIDE_EFFECTS:
        raise HTTPException(status_code=400, detail=f"不支持的 side_effect_class: {payload.side_effect_class}")
    exists = (await db.execute(
        select(MCPServer).where(MCPServer.name == payload.name)
    )).scalar_one_or_none()
    if exists:
        raise HTTPException(status_code=400, detail=f"MCP 服务名已存在: {payload.name}")
    row = MCPServer(**payload.model_dump(), created_by=current_user.id)
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return _row_to_dict(row)


@router.get("/servers/{server_id}")
async def get_server(
    server_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    row = (await db.execute(select(MCPServer).where(MCPServer.id == server_id))).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="MCP 服务不存在")
    return _row_to_dict(row)


@router.put("/servers/{server_id}")
async def update_server(
    server_id: int,
    payload: McpServerUpdate,
    current_user: User = Depends(get_current_superuser),
    db: AsyncSession = Depends(get_db),
):
    row = (await db.execute(select(MCPServer).where(MCPServer.id == server_id))).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="MCP 服务不存在")
    data = payload.model_dump(exclude_unset=True)
    if "transport" in data or "command" in data or "url" in data:
        _validate_payload(
            data.get("transport", row.transport),
            data.get("command", row.command),
            data.get("url", row.url),
        )
    if "side_effect_class" in data and data["side_effect_class"] not in _VALID_SIDE_EFFECTS:
        raise HTTPException(status_code=400, detail=f"不支持的 side_effect_class: {data['side_effect_class']}")
    for key, value in data.items():
        setattr(row, key, value)
    await db.commit()
    await db.refresh(row)
    return _row_to_dict(row)


@router.delete("/servers/{server_id}")
async def delete_server(
    server_id: int,
    current_user: User = Depends(get_current_superuser),
    db: AsyncSession = Depends(get_db),
):
    row = (await db.execute(select(MCPServer).where(MCPServer.id == server_id))).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="MCP 服务不存在")
    await db.delete(row)
    await db.commit()
    return {"ok": True}


@router.post("/servers/{server_id}/test")
async def test_server(
    server_id: int,
    current_user: User = Depends(get_current_superuser),
    db: AsyncSession = Depends(get_db),
):
    """连通性测试：真实 initialize + tools/list，结果落库（客观数据）。"""
    from app.services.mcp.client import test_connection

    row = (await db.execute(select(MCPServer).where(MCPServer.id == server_id))).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="MCP 服务不存在")
    result = await test_connection(_connection_of(row))
    row.last_test_at = datetime.utcnow()
    row.last_test_error = result.get("error")
    if result.get("ok"):
        row.discovered_tools = result.get("tools") or []
    await db.commit()
    await db.refresh(row)
    return {
        "ok": result.get("ok"),
        "error": result.get("error"),
        "server_info": result.get("server_info"),
        "tools": result.get("tools") or [],
        "saved_tools": len(row.discovered_tools or []),
    }
