"""为可恢复的文档失败重新排队，继续使用原 Run 与 Checkpoint。"""

from datetime import datetime, timedelta

from sqlalchemy import delete, func, select

from app.core.config import settings
from app.core.database import AsyncSessionLocal
from app.models.agent import AgentRun, AgentRunEvent, SessionResultOutbox
from app.services.documents.errors import DocumentError

from .events import EventBus, RuntimeEvent, notify_wakeup
from .lifecycle import STATUS_FAILED, STATUS_PENDING, STATUS_RUNNING
from .repositories import SessionRepository
from .task_router import DOCUMENT_GRAPH_TYPES

DOCUMENT_RETRY_QUEUED = "document_retry_queued"
_MAX_MANUAL_RETRIES = 3


def document_retry_count(row: AgentRun) -> int:
    return int((row.input_payload or {}).get("document_retry_count") or 0)


def document_retryable(row: AgentRun) -> bool:
    route = (row.input_payload or {}).get("execution_context") or {}
    if (row.status != STATUS_FAILED or not row.thread_id or row.cancel_requested
            or route.get("task_type") not in DOCUMENT_GRAPH_TYPES
            or document_retry_count(row) >= _MAX_MANUAL_RETRIES):
        return False
    return ((row.summary or {}).get("document_failure_code") == "document_model_output_invalid"
            or str(row.error or "").rstrip("。") == "模型未返回符合文档规范的结构化内容")


async def latest_document_retry_seq(db, run_id: str) -> int:
    return int(await db.scalar(select(func.max(AgentRunEvent.sequence)).where(
        AgentRunEvent.run_id == run_id, AgentRunEvent.event_type == DOCUMENT_RETRY_QUEUED,
    )) or 0)


async def retry_document_run(runtime, run_id: str, user_id: int, *, expected_sequence: int | None = None) -> dict:
    async with AsyncSessionLocal() as db:
        run = await db.get(AgentRun, run_id)
        if run is None or run.user_id != user_id:
            raise DocumentError(404, "运行不存在")
        checkpoint = None
        if document_retryable(run):
            checkpoint = await runtime.get_thread_state(run.thread_id)

    async with AsyncSessionLocal() as db:
        run = await db.scalar(select(AgentRun).where(
            AgentRun.id == run_id, AgentRun.user_id == user_id,
        ).with_for_update())
        if run is None:
            raise DocumentError(404, "运行不存在")
        if run.status in {STATUS_PENDING, STATUS_RUNNING} and document_retry_count(run):
            boundary = await latest_document_retry_seq(db, run_id)
            return {"run_id": run_id, "status": run.status, "after_seq": max(0, boundary - 1)}
        if not document_retryable(run):
            raise DocumentError(409, "当前任务状态无法继续，请编辑原请求后重新发起")
        if not checkpoint or checkpoint.get("error"):
            raise DocumentError(409, "该任务的断点暂不可用，请编辑原请求后重新发起")
        now = datetime.utcnow()
        if run.worker_id and run.lease_expires_at and run.lease_expires_at > now:
            raise DocumentError(409, "上一次执行仍在收尾，请稍后再继续")
        sequence = int(run.last_event_seq or 0)
        last_failed = await db.scalar(select(func.max(AgentRunEvent.sequence)).where(
            AgentRunEvent.run_id == run_id, AgentRunEvent.event_type == "run_failed",
        ))
        if not last_failed or int(last_failed) != sequence or (expected_sequence is not None and expected_sequence != sequence):
            raise DocumentError(409, "执行结果已更新，请刷新对话后继续")
        if run.session_id and not await SessionRepository(db).claim_execution(
            run.session_id, run_id, runtime.worker.lease_seconds, durable=True,
        ):
            raise DocumentError(409, "此会话正在执行其他任务，请等它完成")
        count = document_retry_count(run) + 1
        run.input_payload = {**(run.input_payload or {}), "document_retry_count": count}
        run.status = STATUS_PENDING
        run.error = None
        run.finished_at = None
        run.final_text = None
        run.worker_id = None
        run.lease_expires_at = None
        run.lease_version += 1
        timeout = settings.AGENT_RUN_TIMEOUT
        run.budget_deadline_at = now + timedelta(seconds=timeout) if timeout > 0 else None
        # 原消息保留。新终态重新投递并就地更新同一个助手消息。
        await db.execute(delete(SessionResultOutbox).where(SessionResultOutbox.run_id == run_id))
        await EventBus.persist_in_transaction(RuntimeEvent(
            type=DOCUMENT_RETRY_QUEUED, run_id=run_id, thread_id=run.thread_id,
            data={"reason": "从已保存的文档进度继续", "manual": True, "attempt": count},
        ), db)
        await db.commit()
    notify_wakeup(run_id)
    return {"run_id": run_id, "status": STATUS_PENDING, "after_seq": sequence}
