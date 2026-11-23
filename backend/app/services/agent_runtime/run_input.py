"""持久化文档工作流的人工输入请求，管理等待与续跑之间的状态。"""
from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import datetime
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import AsyncSessionLocal
from app.models.agent import AgentRun
from app.models.document import RunInputRequest

from .events import EventBus, RUN_CANCELLED, RUN_WAITING_INPUT, STATE_CHANGED, RuntimeEvent, notify_wakeup
from .lifecycle import STATUS_CANCELLED, STATUS_RUNNING, STATUS_WAITING_INPUT, RunHandle
from .repositories import RunRepository, SessionRepository
from .repositories.errors import RunLeaseLost

_MAX_SCHEMA_BYTES = 64 * 1024
_MAX_INPUT_BYTES = 192 * 1024


class RunInputError(ValueError):
    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code


def _canonical(data: Any) -> bytes:
    return json.dumps(data, ensure_ascii=False, sort_keys=True, allow_nan=False).encode("utf-8")


def _request_id(run_id: str, interrupt_id: str, ordinal: int) -> str:
    return hashlib.sha256(f"{run_id}:{interrupt_id}:{ordinal}".encode()).hexdigest()


def _payload(request: RunInputRequest) -> dict[str, Any]:
    return {
        "request_id": request.id,
        "prompt": request.prompt,
        "input_schema": request.input_schema,
        "required_fields": request.required_fields,
    }


async def pending_input_payload(db: AsyncSession, run_id: str) -> dict[str, Any] | None:
    request = (await db.execute(
        select(RunInputRequest).where(
            RunInputRequest.run_id == run_id, RunInputRequest.status == "pending"
        ).order_by(RunInputRequest.ordinal.desc()).limit(1)
    )).scalar_one_or_none()
    return _payload(request) if request else None


async def has_input_history(db: AsyncSession, run_id: str) -> bool:
    return (await db.execute(
        select(RunInputRequest.id).where(RunInputRequest.run_id == run_id).limit(1)
    )).scalar_one_or_none() is not None


def _parse_interrupt(value: Any) -> tuple[int, str, dict[str, Any], list[str]]:
    if not isinstance(value, dict) or value.get("kind") != "document_input":
        raise ValueError("文档工作流返回了无法识别的输入请求")
    ordinal, prompt = value.get("ordinal"), value.get("prompt")
    schema = value.get("input_schema")
    required = value.get("required_fields")
    if (
        type(ordinal) is not int or not 1 <= ordinal <= 10_000
        or not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 4000
        or not isinstance(schema, dict) or schema.get("type") != "object"
        or not isinstance(required, list) or not all(isinstance(key, str) for key in required)
    ):
        raise ValueError("文档工作流的输入请求结构无效")
    properties = schema.get("properties")
    declared_required = schema.get("required") or []
    if not (
        isinstance(properties, dict) and isinstance(declared_required, list)
        and all(isinstance(key, str) for key in declared_required)
    ):
        raise ValueError("输入请求字段结构无效")
    required = list(dict.fromkeys(required + declared_required))
    if any(key not in properties for key in required):
        raise ValueError("输入请求的必填字段未声明")
    schema = {"additionalProperties": False, **schema, "required": required}
    if len(_canonical(schema)) > _MAX_SCHEMA_BYTES:
        raise ValueError("输入请求的字段定义过大")
    Draft202012Validator.check_schema(schema)
    return ordinal, prompt, schema, required


async def response_for_interrupt(
    run_id: str, interrupt_id: str, value: Any
) -> Any:
    ordinal, _prompt, _schema, _required = _parse_interrupt(value)
    async with AsyncSessionLocal() as db:
        request = await db.get(RunInputRequest, _request_id(run_id, interrupt_id, ordinal))
        if request is not None and request.status == "submitted":
            return request.response
    return None


async def pause_for_input(run: RunHandle, interrupt_id: str, value: Any) -> None:
    ordinal, prompt, schema, required = _parse_interrupt(value)
    request_id = _request_id(run.run_id, interrupt_id, ordinal)
    lease = run.execution_lease()
    if lease is None:
        raise RunLeaseLost("文档输入暂停缺少有效执行租约")
    async with AsyncSessionLocal() as db:
        repo = RunRepository(db)
        if await repo.verify_running_lease(lease):
            raise asyncio.CancelledError
        await repo.update_status(
            run.run_id, status=STATUS_WAITING_INPUT, lease=lease, expected_status=STATUS_RUNNING
        )
        request = RunInputRequest(
            id=request_id, run_id=run.run_id, ordinal=ordinal, status="pending",
            prompt=prompt, input_schema=schema, required_fields=required,
        )
        db.add(request)
        required_event = RuntimeEvent(
            type="input.required", run_id=run.run_id, thread_id=run.thread_id,
            data=_payload(request),
        )
        boundary = RuntimeEvent(
            type=RUN_WAITING_INPUT, run_id=run.run_id, thread_id=run.thread_id,
            data={"status": STATUS_WAITING_INPUT, "request_id": request_id},
        )
        for event in (required_event, boundary):
            event.sequence = await EventBus.persist_in_transaction(event, db)
        await db.commit()
    run.transition(STATUS_WAITING_INPUT)
    for event in (required_event, boundary):
        run.bus.notify_persisted(event)


async def _locked_run(db: AsyncSession, run_id: str) -> AgentRun | None:
    return (await db.execute(
        select(AgentRun).where(AgentRun.id == run_id).with_for_update()
    )).scalar_one_or_none()


def _validate_submission(schema: dict[str, Any], values: dict[str, Any]) -> bytes:
    try:
        encoded = _canonical(values)
    except (TypeError, ValueError, RecursionError):
        raise RunInputError(422, "提交的输入不是有效的 JSON 数据") from None
    if len(encoded) > _MAX_INPUT_BYTES:
        raise RunInputError(413, "提交的输入过大")
    # 禁止请求 schema 触发远程引用加载。
    validator = Draft202012Validator(
        schema, format_checker=FormatChecker(), registry=Registry()
    )
    error = next(validator.iter_errors(values), None)
    if error is not None:
        pointer = ".".join(str(segment) for segment in error.absolute_path)
        if error.validator == "required":
            missing = [key for key in error.validator_value if key not in error.instance]
            pointer = "、".join(missing)
            raise RunInputError(422, f"缺少必填字段：{pointer}")
        place = f"字段 {pointer}" if pointer else "提交内容"
        raise RunInputError(422, f"{place}不符合输入要求")
    return encoded


async def submit_run_input(
    run_id: str, user_id: int, request_id: str, values: dict[str, Any]
) -> dict[str, Any]:
    async with AsyncSessionLocal() as db:
        run = await _locked_run(db, run_id)
        if run is None or run.user_id != user_id:
            raise RunInputError(404, "运行不存在")
        request = await db.get(RunInputRequest, request_id)
        if request is None or request.run_id != run_id:
            raise RunInputError(404, "输入请求不存在")
        if request.status == "submitted":
            try:
                consistent = _canonical(request.response) == _canonical(values)
            except (TypeError, ValueError, RecursionError):
                consistent = False
            if not consistent:
                raise RunInputError(409, "该输入请求已提交不同的内容")
            return {"run_id": run_id, "status": run.status, "idempotent": True}
        if run.status != STATUS_WAITING_INPUT or run.cancel_requested or request.status != "pending":
            raise RunInputError(409, "运行已不在本次输入的等待状态")
        _validate_submission(request.input_schema, values)
        repo = RunRepository(db)
        if not await repo.queue_input_resume(run_id, user_id):
            raise RunInputError(409, "运行的等待状态已变更，请刷新后重试")
        request.response = values
        request.status = "submitted"
        request.submitted_at = datetime.utcnow()
        # 启用总时限时，为用户补充所耗的等待时间顺延一次。
        if settings.AGENT_RUN_TIMEOUT > 0:
            await repo.shift_deadline(
                run_id, extra_seconds=(request.submitted_at - request.created_at).total_seconds()
            )
        for event in (
            RuntimeEvent(type="input.received", run_id=run_id, thread_id=run.thread_id,
                         data={"request_id": request_id}),
            RuntimeEvent(type=STATE_CHANGED, run_id=run_id, thread_id=run.thread_id,
                         data={"status": STATUS_RUNNING, "request_id": request_id}),
        ):
            await EventBus.persist_in_transaction(event, db)
        await db.commit()
    notify_wakeup(run_id)
    return {"run_id": run_id, "status": STATUS_RUNNING, "idempotent": False}


async def cancel_waiting_input(run_id: str, user_id: int) -> bool:
    """原子取消 Waiting Input Run，和输入提交竞争时仅允许一个状态转移。"""
    async with AsyncSessionLocal() as db:
        owned = (await db.execute(
            select(AgentRun.session_id, AgentRun.thread_id).where(
                AgentRun.id == run_id, AgentRun.user_id == user_id,
                AgentRun.status == STATUS_WAITING_INPUT,
            ).with_for_update()
        )).one_or_none()
        if owned is None:
            return False
        repo = RunRepository(db)
        await repo.request_cancel(run_id, user_id)
        if not await repo.cancel_without_lease(run_id, from_statuses=(STATUS_WAITING_INPUT,)):
            return False
        await db.execute(
            update(RunInputRequest).where(
                RunInputRequest.run_id == run_id, RunInputRequest.status == "pending"
            ).values(status="cancelled")
        )
        if owned.session_id:
            await SessionRepository(db).release_execution(owned.session_id, run_id)
        await EventBus.persist_in_transaction(RuntimeEvent(
            type=RUN_CANCELLED, run_id=run_id, thread_id=owned.thread_id
        ), db)
        await db.commit()
    notify_wakeup(run_id)
    return True
