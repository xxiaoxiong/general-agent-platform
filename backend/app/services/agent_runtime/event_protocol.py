"""Run 事件 v2 协议（任务书 §7.1）：新主协议信封与事件回放。

内部事件（events.py RuntimeEvent，持久化于 agent_run_events）映射为 v2 信封：

    {
      "event": "tool.started",
      "run_id": "run-id",
      "sequence": 12,
      "timestamp": "2026-11-13T12:00:00Z",
      "trace_id": "trace-id",
      "data": {...}
    }

SSE 使用标准 ``id:`` / ``event:`` / ``data:`` 行；``sequence`` 取持久化序号
（agent_runs.last_event_seq 原子分配），是 Last-Event-ID 续传的稳定依据。

流边界（§12.2：SSE 消费者必须能终止）：run.completed / run.failed /
run.cancelled / run.suspended 为终止事件；run.status_changed 带
waiting_approval / needs_reconciliation 状态时同样终止本次流。
"""
from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any, AsyncGenerator, Dict, Optional

from app.core.config import settings

# 内部事件类型 -> v2 事件名（§7.1）
V2_EVENT_NAMES: Dict[str, str] = {
    "run_started": "run.started",
    "state_changed": "run.status_changed",
    "plan_updated": "plan.updated",
    "model_delta": "assistant.delta",
    "tool_started": "tool.started",
    "tool_completed": "tool.completed",
    "tool_failed": "tool.failed",
    "subagent_started": "subagent.started",
    "subagent_completed": "subagent.completed",
    "approval_required": "approval.required",
    "run_waiting_approval": "run.status_changed",
    "run_waiting_input": "run.status_changed",
    "run_completed": "run.completed",
    "run_failed": "run.failed",
    "run_cancelled": "run.cancelled",
    "run_suspended": "run.suspended",
    # Phase 4 预留（Verification / Retry-Recovery）：
    "run_retrying": "run.retrying",
    "document_retry_queued": "run.retrying",
    "run_recovering": "run.recovering",
    "verification_started": "verification.started",
    "verification_completed": "verification.completed",
    "artifact_created": "artifact.created",
    "run_cancelling": "run.cancelling",
    # 执行中补充要求（通用能力）
    "run_steered": "run.steered",
    "run_steering_applied": "run.steering_applied",
    # Decision Architecture：模型建议 + 服务端最终接受结果（与 task.routed 分离）
    "task_decision": "task.decision",
}

# 终止事件（消费者收到即结束本次事件流）
V2_TERMINAL_EVENTS = frozenset(
    {"run.completed", "run.failed", "run.cancelled", "run.suspended"}
)

# run.status_changed 中终止流的状态（暂停等待人工输入）
_V2_BOUNDARY_STATUSES = frozenset({"waiting_approval", "waiting_input", "needs_reconciliation"})


def _iso(ts: Optional[datetime]) -> str:
    if ts is None:
        return datetime.utcnow().isoformat() + "Z"
    return ts.isoformat() + ("Z" if ts.tzinfo is None else "")


def _reshape_data(name: str, event_type: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    """内部 payload -> v2 data（§7.1 字段口径）。"""
    payload = payload or {}
    if name == "tool.started":
        data = {
            "tool_name": payload.get("tool"),
            "tool_call_id": payload.get("tool_call_id", ""),
            "input": payload.get("input"),
            "summary": f"正在执行 {payload.get('tool')}",
        }
        if payload.get("subagent"):
            data["subagent"] = payload.get("subagent")
        return data
    if name in ("tool.completed", "tool.failed"):
        data = {
            "tool_name": payload.get("tool"),
            "tool_call_id": payload.get("tool_call_id", ""),
            "summary": f"{payload.get('tool')} 执行{'完成' if name == 'tool.completed' else '失败'}",
        }
        if payload.get("subagent"):
            data["subagent"] = payload.get("subagent")
        if name == "tool.completed":
            data["output"] = payload.get("output")
            if payload.get("sources"):
                # 结构化来源（如 web_search 的搜索结果清单）
                data["sources"] = payload.get("sources")
        else:
            data["error"] = payload.get("error")
        if payload.get("replay"):
            data["replay"] = payload.get("replay")
        return data
    if name == "assistant.delta":
        return {"text": payload.get("text") or ""}
    if name == "subagent.started" or name == "subagent.completed":
        data = {"name": payload.get("name")}
        if payload.get("tool_call_id"):
            data["tool_call_id"] = payload.get("tool_call_id")
        if payload.get("status"):
            data["status"] = payload.get("status")
        return data
    if name == "plan.updated":
        return {"todos": payload.get("todos") or []}
    if name == "approval.required":
        return {"actions": payload.get("actions") or []}
    if name == "run.status_changed":
        # run_waiting_approval 的 payload 携带 actions；state_changed 携带 status
        data = {"status": payload.get("status") or "waiting_approval"}
        if payload.get("actions") is not None:
            data["actions"] = payload.get("actions")
        if payload.get("request_id") is not None:
            data["request_id"] = payload["request_id"]
        return data
    if name == "run.completed":
        data = {"final_text": payload.get("final_text")}
        if payload.get("summary") is not None:
            data["summary"] = payload.get("summary")
        return data
    if name == "run.failed":
        data = {"error": payload.get("error") or "运行失败", "status": payload.get("status", "failed")}
        if payload.get("document_retryable"):
            data["document_retryable"] = True
        if "final_text" in payload:
            data["final_text"] = payload["final_text"]
        if payload.get("summary") is not None:
            data["summary"] = payload["summary"]
        return data
    if name == "run.suspended":
        return {"reason": payload.get("reason"), "status": payload.get("status", "suspended")}
    return dict(payload)


def to_v2_event(
    *,
    run_id: str,
    sequence: int,
    event_type: str,
    payload: Optional[Dict[str, Any]] = None,
    timestamp: Optional[datetime] = None,
    trace_id: str = "",
) -> Dict[str, Any]:
    """内部事件行 -> v2 信封（§7.1）。"""
    name = V2_EVENT_NAMES.get(event_type, event_type)
    return {
        "event": name,
        "run_id": run_id,
        "sequence": sequence,
        "timestamp": _iso(timestamp),
        "trace_id": trace_id or run_id,
        "data": _reshape_data(name, event_type, payload or {}),
    }


def is_terminal(envelope: Dict[str, Any]) -> bool:
    """v2 事件是否为流边界（消费者应终止本次 SSE）。"""
    name = envelope.get("event")
    if name in V2_TERMINAL_EVENTS:
        return not (envelope.get("data") or {}).get("historical")
    if name == "run.status_changed":
        data = envelope.get("data") or {}
        return not data.get("historical") and data.get("status") in _V2_BOUNDARY_STATUSES
    return False


def synthetic_terminal_from_status(run_id: str, status: str, *, error: Optional[str] = None,
                                   final_text: Optional[str] = None, summary: Optional[dict] = None) -> Dict[str, Any]:
    """无事件日志的旧运行：按 run 状态合成 v2 终止事件（保持行为兼容）。"""
    mapping = {
        "completed": "run.completed",
        "failed": "run.failed",
        "limit_exceeded": "run.failed",
        "cancelled": "run.cancelled",
        "waiting_approval": "run.status_changed",
        "waiting_input": "run.status_changed",
        "needs_reconciliation": "run.status_changed",
    }
    name = mapping.get(status, "run.suspended")
    data: Dict[str, Any] = {"status": status}
    if error:
        data["error"] = error
    if final_text is not None:
        data["final_text"] = final_text
    if summary is not None:
        data["summary"] = summary
    return {
        "event": name,
        "run_id": run_id,
        "sequence": 0,
        "timestamp": _iso(None),
        "trace_id": run_id,
        "data": data,
    }


def sse_format(envelope: Dict[str, Any]) -> str:
    """v2 信封 -> 标准 SSE 帧（id: / event: / data:）。"""
    import json

    seq = envelope.get("sequence", 0)
    name = envelope.get("event", "message")
    data = json.dumps(envelope, ensure_ascii=False, default=str)
    return f"id: {seq}\nevent: {name}\ndata: {data}\n\n"


async def replay_events_v2(
    run_id: str,
    after_seq: Optional[int] = None,
    *,
    run_owner_id: Optional[int] = None,
) -> AsyncGenerator[Dict[str, Any], None]:
    """按持久化事件日志消费 v2 事件流（SSE 断线回放 + 跨进程订阅）。

    - 事实来源为 ``agent_run_events``（sequence 由 last_event_seq 原子分配）；
    - ``after_seq``（含 Last-Event-ID）之前的事件不重复下发；None = 从头回放；
    - 运行未终结时轮询新事件直至终止事件/终态合成（§12.2：消费者必然终止）。
    """
    from .repositories import EventRepository, RunRepository
    from .document_retry import latest_document_retry_seq
    from .events import WORKSPACE_PROJECT_REVIEWED, wait_wakeup
    from .lifecycle import (
        TERMINAL_STATUSES, STATUS_WAITING_APPROVAL, STATUS_WAITING_INPUT,
        STATUS_NEEDS_RECONCILIATION,
    )

    from app.core.database import AsyncSessionLocal

    db_seq = int(after_seq or 0)
    # SSE 事件下发要"实时"：轮询兜底间隔独立于 Worker 扫描间隔（AGENT_RUNTIME_POLL_INTERVAL），
    # 同进程场景由 EventBus 唤醒立即取事件，轮询仅是跨进程兜底
    poll = max(0.05, float(getattr(settings, "AGENT_SSE_POLL_INTERVAL", 0.3)))
    # resume 竞态宽限：等待 Claim（waiting_approval->running）的最长时长，
    # 超时说明恢复未实际发生（如决策校验失败），退回合成终止事件防消费端挂死
    resume_grace_deadline: Optional[float] = None
    # 终止事件合成宽限：终态刚提交而终止事件行未落库的间隙只等一轮唤醒
    synth_graced = False

    def _visible(envelope: Dict[str, Any]) -> bool:
        return after_seq is None or int(envelope.get("sequence") or 0) > after_seq

    while True:
        async with AsyncSessionLocal() as db:
            rows = await EventRepository(db).list_after(run_id, db_seq)
            retry_sequence = await latest_document_retry_seq(db, run_id) if any(
                row.event_type == "run_failed" for row in rows
            ) else 0
        if rows:
            for row in rows:
                db_seq = row.sequence
                if row.event_type == WORKSPACE_PROJECT_REVIEWED:
                    continue
                envelope = to_v2_event(
                    run_id=run_id,
                    sequence=row.sequence,
                    event_type=row.event_type,
                    payload=row.payload or {},
                    timestamp=row.created_at,
                    trace_id=run_id,
                )
                if envelope["event"] == "run.failed" and row.sequence < retry_sequence:
                    envelope["data"]["historical"] = True
                if _visible(envelope):
                    if (
                        envelope["event"] == "run.status_changed"
                        and envelope["data"].get("status") == STATUS_WAITING_INPUT
                    ):
                        from .run_input import pending_input_payload

                        async with AsyncSessionLocal() as db:
                            current_pending = await pending_input_payload(db, run_id)
                        if (
                            current_pending is None
                            or current_pending["request_id"] != envelope["data"].get("request_id")
                        ):
                            envelope["data"]["historical"] = True
                    yield envelope
                if is_terminal(envelope):
                    return
            continue

        async with AsyncSessionLocal() as db:
            run_row = await RunRepository(db).get_run(run_id)
        if run_row is None:
            if db_seq == 0 and after_seq is None:
                yield synthetic_terminal_from_status(run_id, "failed", error="运行不存在")
            return
        if run_row.status in TERMINAL_STATUSES or run_row.status in (
            STATUS_WAITING_APPROVAL, STATUS_WAITING_INPUT, STATUS_NEEDS_RECONCILIATION,
        ):
            last_seq = int(getattr(run_row, "last_event_seq", 0) or 0)
            resuming = after_seq is not None and last_seq > 0 and db_seq >= last_seq
            if resuming and run_row.status in (
                STATUS_WAITING_APPROVAL, STATUS_NEEDS_RECONCILIATION,
            ):
                # resume 后重新订阅的消费者：已消费过边界事件（db_seq >= last_event_seq），
                # 此处 run 状态可能仍是 Claim 前的 waiting_approval（POST /resume 先返回、
                # Claim 异步进行）。不得合成终止事件（否则消费端立即收尾、UI 卡在"待审批"），
                # 继续轮询等待恢复产生的新事件（§12.2 边界语义只终止"见证边界"的那次流）。
                import time as _time

                if resume_grace_deadline is None:
                    resume_grace_deadline = _time.monotonic() + 60.0
                if _time.monotonic() < resume_grace_deadline:
                    await wait_wakeup(run_id, poll)
                    continue
                # 宽限期外仍未恢复：按边界状态合成终止事件（防消费端挂死）
            # 终态已提交但终止事件行尚未落库的间隙：先给一轮唤醒窗口等待真实
            # 终止事件（§12.2 终止事件必须带最终回答/最终状态），仍缺失才
            # 按状态合成兜底（合成事件缺 final_text，仅防消费端挂死）
            if run_row.status == STATUS_WAITING_INPUT and db_seq >= last_seq and after_seq is not None:
                return
            if not synth_graced:
                synth_graced = True
                await wait_wakeup(run_id, poll)
                continue
            # 事件日志缺失/已清理：按状态合成终止事件
            envelope = synthetic_terminal_from_status(
                run_id, run_row.status, error=run_row.error,
                final_text=run_row.final_text, summary=run_row.summary,
            )
            envelope["sequence"] = db_seq + 1
            if _visible(envelope):
                yield envelope
            return
        await wait_wakeup(run_id, poll)
