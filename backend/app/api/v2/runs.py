"""Runs API v2（§7）：Run 创建/状态/事件 SSE/取消/恢复。

产品语义（§8.4 Cancel / Resume / Reconnect）：
- POST /runs 创建 Run 并返回 run_id（X-Run-Id 响应头），执行在 Runtime
  Worker 中推进，事件持久化于 agent_run_events；
- GET /runs/{id}/events 以 v2 协议（§7.1）SSE 订阅/回放，
  ``Last-Event-ID``（或 after_seq）断线续传，不丢失不重复不乱序；
- 停止订阅与取消分离：前端停止只断开 SSE，取消必须显式调用 cancel；
- 审批决策携带稳定 action_id（FR-06），resume 后重新订阅事件流；
- 所有事件以持久化日志为事实来源（FR-07），API 与执行 Worker 可不同进程。
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Response
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.core.database import AsyncSessionLocal, get_db
from app.models.agent import Agent, AgentSession
from app.models.user import User

from .schemas import RunCancelRequest, RunCreate, RunResumeRequest

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/runs", tags=["runs-v2"])


# ── 辅助 ──────────────────────────────────────────────────────────────────────

async def _load_agent_config(db: AsyncSession, agent: Agent, *, use_memory: bool) -> dict:
    """由 Agent 行构建运行配置（P2-01：优先绑定不可变版本快照执行）。"""
    from app.assistant.versions import config_from_version, ensure_version

    version = await ensure_version(db, agent.id)
    if version is not None:
        cfg = dict(config_from_version(version))
    else:
        # 合成配置（无真实 Agent 行的场景不进 v2；防御性兜底）
        cfg = {
            "id": agent.id,
            "system_prompt": agent.system_prompt or "",
            "model_config_id": agent.model_config_id,
            "temperature": agent.temperature,
            "max_iterations": agent.max_iterations,
            "max_tokens": agent.max_tokens,
            "tools": list(agent.tools or []),
            "capability_ids": list(getattr(agent, "capability_ids", None) or []),
            "knowledge_base_id": agent.knowledge_base_id,
            "database_connection_id": agent.database_connection_id,
        }
    cfg["name"] = agent.name
    # P2-04：长期记忆走 Store MemoryMiddleware（/memory/AGENTS.md），不再走旧 SQLite MemoryManager
    cfg["memory_policy"] = {"use_memory_context": bool(use_memory)}
    return cfg


async def _load_db_info(database_connection_id: Optional[int]) -> Optional[dict]:
    """数据源注入（与 v1 chat 同口径）：预加载表列表，避免模型猜表名。"""
    if not database_connection_id:
        return None
    try:
        from app.models.database_connection import DatabaseConnection

        async with AsyncSessionLocal() as db:
            db_conn = (await db.execute(
                select(DatabaseConnection).where(DatabaseConnection.id == database_connection_id)
            )).scalar_one_or_none()
        if db_conn is None:
            return None
        db_info = {
            "name": db_conn.name,
            "db_type": db_conn.db_type,
            "database": db_conn.database,
            "host": db_conn.host,
            "tables": [],
        }
        import sqlalchemy as sa

        db_type = db_conn.db_type
        if db_type == "mysql":
            sync_url = f"mysql+pymysql://{db_conn.username}:{db_conn.password}@{db_conn.host}:{db_conn.port}/{db_conn.database}"
        elif db_type == "postgresql":
            sync_url = f"postgresql+psycopg2://{db_conn.username}:{db_conn.password}@{db_conn.host}:{db_conn.port}/{db_conn.database}"
        elif db_type == "sqlite":
            sync_url = f"sqlite:///{db_conn.database}"
        else:
            return db_info
        if db_conn.extra_params:
            sync_url += f"?{db_conn.extra_params}"
        sync_engine = sa.create_engine(sync_url, pool_pre_ping=True)
        try:
            db_info["tables"] = sa.inspect(sync_engine).get_table_names()[:50]
        finally:
            sync_engine.dispose()
        return db_info
    except Exception as e:  # noqa: BLE001
        logger.warning("加载数据库连接信息失败: %s", e)
        return None


async def _drain_legacy_stream(stream) -> None:
    """后台排空旧契约事件流（执行由 Worker 驱动；事件已持久化，v2 经 /events 消费）。"""
    try:
        async for _ in stream:
            pass
    except Exception:  # noqa: BLE001
        logger.exception("v2 后台事件流排空异常")


# ── Run 生命周期 ──────────────────────────────────────────────────────────────

@router.post("")
async def create_run(
    body: RunCreate,
    response: Response,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key"),
):
    """创建 Run（§7 POST /runs）。

    响应即刻返回 run_id（X-Run-Id）；事件流经 GET /runs/{id}/events 订阅。
    幂等（P1-01）：同 Key 同摘要复用原 Run；同 Key 不同摘要 409。
    """
    from app.services.agent_runtime import get_runtime_service
    from app.services.agent_runtime.service import compute_request_hash
    from app.services.agent_runtime.repositories import IdempotencyConflict, SessionBusy

    # 1) 会话定位（仅限当前用户）
    session: Optional[AgentSession] = None
    if body.conversation_id:
        res = await db.execute(
            select(AgentSession).where(
                AgentSession.id == body.conversation_id,
                AgentSession.user_id == current_user.id,
            )
        )
        session = res.scalar_one_or_none()
        if session is None:
            raise HTTPException(status_code=404, detail="会话不存在")

    # 2) 助手定位：会话绑定 > 显式指定 > 默认助手（UX-01）
    if session is not None:
        agent_id = session.agent_id
    elif body.assistant_id:
        agent_id = body.assistant_id
    else:
        from app.assistant.default_assistant import ensure_default_assistant

        default = await ensure_default_assistant(db, current_user.id, created_by=current_user.id)
        await db.commit()
        agent_id = default.id

    res = await db.execute(
        select(Agent).where(Agent.id == agent_id, Agent.is_active == True)  # noqa: E712
    )
    agent = res.scalar_one_or_none()
    if agent is None or agent.owner_id not in (None, current_user.id):
        raise HTTPException(status_code=404, detail="助手不存在")

    agent_config = await _load_agent_config(db, agent, use_memory=body.use_memory)
    # 权限模块：审批模式随 Run 创建写入配置（并落入 agent_config_snapshot，
    # 崩溃恢复/断点续跑后仍保持原模式）；非法取值一律回落 manual（安全默认）
    agent_config["approval_mode"] = (
        body.approval_mode if body.approval_mode in ("manual", "auto") else "manual"
    )
    # ensure_version 可能写入 agent_versions（flush 持写锁）：先提交释放，
    # 避免与 start_run_with_session 的独立会话写入互相阻塞（SQLite 必需）
    await db.commit()
    db_info = await _load_db_info(agent_config.get("database_connection_id"))
    user_message = {"role": "user", "content": body.message}
    messages = (list(session.messages or []) if session is not None else []) + [user_message]

    # 3) 幂等预查（P1-01）
    if idempotency_key:
        try:
            existing = await get_runtime_service().lookup_idempotent(
                current_user.id,
                idempotency_key,
                compute_request_hash(
                    endpoint="chat",
                    user_id=current_user.id,
                    agent_id=agent_id,
                    session_id=body.conversation_id,
                    user_input=body.message,
                    agent_config=agent_config,
                ),
            )
        except IdempotencyConflict as e:
            raise HTTPException(status_code=409, detail=str(e))
        if existing is not None:
            response.headers["X-Run-Id"] = existing.id
            return {
                "run_id": existing.id,
                "conversation_id": existing.session_id,
                "status": existing.status,
                "idempotent_reuse": True,
                "events_url": f"/api/v2/runs/{existing.id}/events",
            }

    # 4) 启动运行（Session 创建 + 用户消息追加 + Run 创建同事务原子提交）
    try:
        run_id, conversation_id, stream = await get_runtime_service().start_run_with_session(
            agent_config=agent_config,
            messages=messages,
            user_id=current_user.id,
            session_id=body.conversation_id,
            idempotency_key=idempotency_key,
            db_info=db_info,
            endpoint="chat",
            user_message=user_message,
        )
    except IdempotencyConflict as e:
        raise HTTPException(status_code=409, detail=str(e))
    except SessionBusy as e:
        raise HTTPException(status_code=409, detail=str(e))

    # 执行由 Worker 独立驱动；旧契约事件流后台排空（v2 事实来源是持久化事件日志）
    asyncio.ensure_future(_drain_legacy_stream(stream))

    response.headers["X-Run-Id"] = run_id
    return {
        "run_id": run_id,
        "conversation_id": conversation_id,
        "status": "running",
        "events_url": f"/api/v2/runs/{run_id}/events",
    }


@router.get("/{run_id}")
async def get_run_status(
    run_id: str,
    current_user: User = Depends(get_current_user),
):
    """Run 状态（断线后重连 / 轮询用；恒读数据库，§8.4 5）。"""
    info = await _get_run_or_404(run_id, current_user.id)
    return info


@router.get("/{run_id}/events")
async def stream_run_events(
    run_id: str,
    after_seq: Optional[int] = Query(None),
    last_event_id: Optional[str] = Header(None),
    current_user: User = Depends(get_current_user),
):
    """v2 事件流订阅/回放（§7.1）：标准 SSE id:/event:/data: 帧。

    - ``Last-Event-ID``（或 after_seq）之前的事件不重复下发（§8.4 4/6）；
    - 事实来源为 agent_run_events，API 与执行 Worker 不同进程也可消费；
    - 到达终止事件（run.completed/failed/cancelled/suspended 或暂停边界）即结束。
    """
    from app.services.agent_runtime.event_protocol import replay_events_v2, sse_format

    await _get_run_or_404(run_id, current_user.id)
    if last_event_id and last_event_id.strip().isdigit():
        after_seq = int(last_event_id.strip())
    if after_seq is not None:
        from app.observability import metrics

        metrics.inc_sse_reconnect()

    async def event_stream():
        async for envelope in replay_events_v2(run_id, after_seq):
            yield sse_format(envelope)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "X-Run-Id": run_id},
    )


@router.post("/{run_id}/cancel")
async def cancel_run(
    run_id: str,
    body: RunCancelRequest = None,
    current_user: User = Depends(get_current_user),
):
    """取消 Run（§8.4 3）：取消意图先记录，Worker 确认后进入终态。

    在途外部工具结果不确定时不伪造取消成功（返回 cancelling 由 Worker 收敛）。
    """
    from app.services.agent_runtime import get_runtime_service

    await _get_run_or_404(run_id, current_user.id)
    result = await get_runtime_service().cancel_run(run_id, current_user.id)
    if not result.get("ok"):
        raise HTTPException(status_code=409, detail=result.get("error", "取消失败"))
    status = result.get("status")
    if status == "cancelling":
        message = "取消请求已记录，Worker 确认后进入终态"
    elif status == "cancelled":
        message = "运行已取消"
    else:
        message = f"运行已处于终态（{status}），未执行取消"
    return {"run_id": run_id, "status": status, "message": message}


@router.post("/{run_id}/resume")
async def resume_run(
    run_id: str,
    body: RunResumeRequest,
    current_user: User = Depends(get_current_user),
):
    """HITL 审批后恢复（§8.4 8）：决策携带稳定 action_id。

    恢复执行由 Worker 推进；事件流经 GET /runs/{id}/events 继续订阅。
    重复一致决策幂等；冲突 409；非法 400；越权 403（FR-04/F-11）。
    """
    from app.services.agent_runtime import get_runtime_service
    from app.services.agent_runtime.service import (
        ApprovalConflict,
        ApprovalForbidden,
        ApprovalInvalid,
    )

    info = await _get_run_or_404(run_id, current_user.id)
    decisions = [
        {"type": d.type, "message": d.message, "action_id": d.action_id}
        for d in body.decisions
    ] or [{"type": "approve", "message": "", "action_id": None}]

    service = get_runtime_service()
    try:
        verdict = await service.check_decisions(run_id, decisions, current_user.id)
    except ApprovalForbidden as e:
        raise HTTPException(status_code=403, detail=str(e))
    except ApprovalInvalid as e:
        raise HTTPException(status_code=400, detail=str(e))
    except ApprovalConflict as e:
        raise HTTPException(status_code=409, detail=str(e))

    if info.get("status") != "waiting_approval":
        if verdict == "idempotent":
            return {
                "run_id": run_id,
                "status": info.get("status"),
                "idempotent": True,
                "message": "该审批决策已提交过，未重复执行",
            }
        raise HTTPException(status_code=409, detail="运行不在待审批状态")

    # 恢复驱动（决策校验/持久化/Claim 在 resume 流内完成；事件持久化后经 /events 消费）
    asyncio.ensure_future(_drain_legacy_stream(service.resume_run(run_id, decisions, current_user.id)))
    return {
        "run_id": run_id,
        "status": "running",
        "message": "已恢复执行",
        "events_url": f"/api/v2/runs/{run_id}/events",
    }


async def _get_run_or_404(run_id: str, user_id: int) -> dict:
    from app.services.agent_runtime import get_runtime_service

    info = await get_runtime_service().get_run(run_id, user_id)
    if not info:
        raise HTTPException(status_code=404, detail="运行不存在")
    return info
