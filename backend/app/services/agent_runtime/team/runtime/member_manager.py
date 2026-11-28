"""MemberRuntimeManager：成员创建、启动、恢复、中断、取消、预算与事件。

- 每个成员使用稳定 Thread：``team:{team_run_id}:member:{member_id}``；
- 同一成员后续执行复用原 Thread；不同 Team Run / Member 完全隔离；
- 恢复区分“Agent Checkpoint 待执行（next 非空）”与“任务已提交”两类事实；
- 任务认领/完成/失败以 Task Board 为准（CAS + 租约），成员状态机独立持久化；
- 预算同时记在成员行与父 Run 上下文（成员级 + 团队级双层约束）。
"""
from __future__ import annotations

import asyncio
import hashlib
import inspect
import logging
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Awaitable, Callable, Optional

from langchain_core.messages import AIMessage, HumanMessage
from langgraph.errors import GraphBubbleUp

from app.core.database import AsyncSessionLocal

from ...context import ExecutionBudget, ModelUsageCallback, RuntimeContext
from ...events import TEAM_MEMBER_UPDATED, EventBus, RuntimeEvent, redact_secrets
from ...persistence import make_run_config
from ...repositories.errors import PersistenceError
from ...tools.policy import BudgetExceededError, ToolPolicy
from ..errors import MemberStateError, TaskConflict, TeamNotFound
from ..repositories import MemberRepository
from ..services.artifact_store import ArtifactStore
from ..services.message_bus import MessageBusService
from ..services.task_board import TaskBoardService
from ..states import (
    MEMBER_COMPLETED, MEMBER_CREATED, MEMBER_FAILED, MEMBER_INTERRUPTED,
    MEMBER_READY, MEMBER_RUNNING, MEMBER_WAITING,
)
from .capacity import ModelCapacityLimiter, get_capacity_limiter
from .member_context import MemberExecutionContext, MemberResult
from .member_directory import MemberDirectory
from .member_factory import MemberAgentFactory
from .member_profile import MemberProfile
from .model_routes import model_snapshot, team_model_key

logger = logging.getLogger("ai_plat.team")

_INTERRUPT_ERRORS = (PersistenceError, GraphBubbleUp)


def leader_thread_id(team_run_id: str) -> str:
    return f"team:{team_run_id}:member:leader"


def member_thread_id(team_run_id: str, member_id: str) -> str:
    return f"team:{team_run_id}:member:{member_id}"


def stable_member_id(team_run_id: str, task_id: Optional[str], role: str) -> str:
    digest = hashlib.sha1(
        f"{team_run_id}|{task_id or ''}|{role}".encode("utf-8")
    ).hexdigest()[:16]
    return f"mbr-{digest}"


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                text = block.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return "".join(parts)
    return str(content or "")


def _last_ai_text(result: Any) -> tuple[str, bool]:
    messages = result.get("messages") if isinstance(result, dict) else None
    if not messages:
        return "", False
    for message in reversed(messages):
        if isinstance(message, AIMessage):
            text = _content_text(message.content).strip()
            if not text:
                continue
            finish = str((message.response_metadata or {}).get("finish_reason") or "")
            return text, finish in {"length", "max_tokens", "max_output_tokens"}
    return "", False


@dataclass
class _RunOutcome:
    text: str
    truncated: bool
    resumed: bool
    member_ctx: RuntimeContext


class MemberRuntimeManager:
    def __init__(
        self,
        *,
        team_run_id: str,
        run_id: str,
        parent_ctx: RuntimeContext,
        provider: dict[str, Any],
        model_config: dict[str, Any],
        policy: ToolPolicy,
        tool_specs: list[dict[str, Any]],
        bus: EventBus,
        board: TaskBoardService,
        messages: MessageBusService,
        artifacts: ArtifactStore,
        directory: MemberDirectory,
        checkpointer: Any,
        store: Any = None,
        flush_budget: Optional[Callable[[RuntimeContext, Any], Awaitable[None]]] = None,
        agent_builder: Optional[Callable[..., Any]] = None,
        model_loader: Optional[Callable[[int], Awaitable[tuple[dict, dict, dict]]]] = None,
        capacity: Optional[ModelCapacityLimiter] = None,
        usage_lock: Optional[asyncio.Lock] = None,
        team_config: Optional[dict[str, Any]] = None,
        leader_member_id: str = "leader",
        run_handle: Any = None,
    ):
        self.team_run_id = team_run_id
        self.run_id = run_id
        self.parent_ctx = parent_ctx
        self.provider = provider
        self.model_config = model_config
        self.policy = policy
        self.tool_specs = tool_specs
        self.bus = bus
        self.board = board
        self.messages = messages
        self.artifacts = artifacts
        self.directory = directory
        self.checkpointer = checkpointer
        self.store = store
        self.flush_budget = flush_budget
        self.agent_builder = agent_builder
        self.model_loader = model_loader
        self.capacity = capacity or get_capacity_limiter()
        self.usage_lock = usage_lock or asyncio.Lock()
        self.team_config = team_config or {}
        self.leader_member_id = leader_member_id
        self.run_handle = run_handle
        self._factory = MemberAgentFactory(
            policy=policy,
            tool_specs=tool_specs,
            bus=bus,
            checkpointer=checkpointer,
            store=store,
            capacity=self.capacity,
        )

    # ── 成员身份 ─────────────────────────────────────────────────────────

    async def ensure_member(
        self,
        *,
        role: str,
        task_id: Optional[str] = None,
        parent_member_id: Optional[str] = None,
        profile: Optional[MemberProfile] = None,
        is_leader: bool = False,
        task: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        member_id = "leader" if is_leader else stable_member_id(
            self.team_run_id, task_id, role,
        )
        profile = profile or MemberProfile.from_task(
            task or {"role": role}, team_config=self.team_config,
        )
        provider, model_config, snapshot = await self._resolve_model(profile)
        async with AsyncSessionLocal() as db:
            repo = MemberRepository(db)
            row = await repo.get(self.team_run_id, member_id)
            if row is None:
                row = await repo.create(
                    member_id=member_id,
                    team_run_id=self.team_run_id,
                    role=role,
                    name=profile.name,
                    thread_id=member_thread_id(self.team_run_id, member_id) if not is_leader
                    else leader_thread_id(self.team_run_id),
                    profile_snapshot=profile.to_snapshot(),
                    model_snapshot=snapshot,
                    budget=profile.budget_snapshot(),
                    parent_member_id=parent_member_id,
                    task_id=task_id,
                    status=MEMBER_CREATED,
                )
            if row.status == MEMBER_CREATED:
                await repo.transition(
                    self.team_run_id, member_id,
                    target=MEMBER_READY, sources=frozenset({MEMBER_CREATED}),
                )
            await db.commit()
            fresh = await repo.get(self.team_run_id, member_id)
            return self._member_dict(fresh)

    async def list_members(self, team_run_id: Optional[str] = None) -> list[dict[str, Any]]:
        return await self.directory.list_members(team_run_id or self.team_run_id)

    async def get_member(self, member_id: str) -> Optional[dict[str, Any]]:
        async with AsyncSessionLocal() as db:
            row = await MemberRepository(db).get(self.team_run_id, member_id)
            return self._member_dict(row) if row else None

    async def cancel(self, member_id: str, reason: str = "团队已取消") -> bool:
        async with AsyncSessionLocal() as db:
            ok = await MemberRepository(db).transition(
                self.team_run_id, member_id,
                target="cancelled",
                sources=frozenset({
                    MEMBER_CREATED, MEMBER_READY, MEMBER_RUNNING, MEMBER_WAITING,
                    MEMBER_INTERRUPTED,
                }),
                last_error=reason[:2000],
                completed_at=datetime.utcnow(),
            )
            await db.commit()
        if ok:
            await self._emit_member(member_id, reason=reason)
        return ok

    # ── 成员执行 ─────────────────────────────────────────────────────────

    async def execute(
        self,
        task: dict[str, Any],
        *,
        objective: str,
        reference_context: str = "",
        assistant_prompt: str = "",
        predecessors: Optional[list[dict[str, Any]]] = None,
        acceptance: Optional[list[str]] = None,
        findings: Optional[list[dict[str, Any]]] = None,
        prior_result: str = "",
        profile: Optional[MemberProfile] = None,
        parent_member_id: Optional[str] = None,
    ) -> MemberResult:
        task_id = str(task.get("id") or "")
        role = str(task.get("role") or "团队成员")
        # 任务已完成：直接复用既有产物（重试/恢复幂等）
        try:
            current = await self.board.get_task(self.team_run_id, task_id)
        except TeamNotFound:
            current = None
        if current and current["board_status"] == "completed":
            return MemberResult(
                team_run_id=self.team_run_id,
                member_id="",
                task_id=task_id,
                status="completed",
                output=current.get("result") or "",
                artifact_id=current.get("result_artifact_id"),
                thread_id="",
                recovered=True,
            )
        profile = profile or MemberProfile.from_task(task, team_config=self.team_config)
        member = await self.ensure_member(
            role=role, task_id=task_id, parent_member_id=parent_member_id,
            profile=profile, task=task,
        )
        member_id = member["member_id"]
        await self._prepare_member(member_id)
        try:
            claimed = await self.board.claim(
                self.team_run_id, task_id, member_id=member_id,
            )
        except TaskConflict as exc:
            latest = await self.board.get_task(self.team_run_id, task_id)
            if latest["board_status"] == "completed":
                return MemberResult(
                    team_run_id=self.team_run_id, member_id=member_id, task_id=task_id,
                    status="completed", output=latest.get("result") or "",
                    artifact_id=latest.get("result_artifact_id"),
                    thread_id=member["thread_id"], recovered=True,
                )
            raise MemberStateError(str(exc)) from exc
        lease_token = claimed.get("lease_token")
        if not lease_token:
            # 已完成（claim 幂等返回）
            return MemberResult(
                team_run_id=self.team_run_id, member_id=member_id, task_id=task_id,
                status="completed", output=claimed.get("result") or "",
                artifact_id=claimed.get("result_artifact_id"),
                thread_id=member["thread_id"], recovered=True,
            )
        await self.board.mark_running(
            self.team_run_id, task_id, member_id=member_id, lease_token=lease_token,
        )
        await self._set_member_status(
            member_id, MEMBER_RUNNING,
            sources=frozenset({MEMBER_READY, MEMBER_RUNNING, MEMBER_INTERRUPTED, MEMBER_WAITING}),
            started_at=datetime.utcnow(), task_id=task_id, last_error=None,
        )
        await self._emit_member(member_id)

        execution = MemberExecutionContext(
            team_run_id=self.team_run_id,
            member_id=member_id,
            role=role,
            thread_id=member["thread_id"],
            task_id=task_id,
            task_revision=int(task.get("revision") or 0) or None,
            objective=objective,
            reference_context=reference_context,
            assistant_prompt=assistant_prompt,
            task_brief=f"{task.get('title') or ''}\n{task.get('brief') or ''}".strip(),
            predecessors=list(predecessors or []),
            acceptance=list(acceptance or []),
            findings=list(findings or []),
            prior_result=prior_result,
            profile=profile,
        )
        started = time.monotonic()
        usage = {"model_calls": 0, "tool_calls": 0, "tokens": 0}
        include_inbox = await self.messages.has_pending(self.team_run_id, member_id)
        include_task_get = bool(findings)  # 正常任务信息已注入；返工需查 revision
        try:
            outcome = await self._run_agent(
                member=member,
                execution=execution,
                profile=profile,
                is_leader=False,
                usage=usage,
                include_inbox=include_inbox,
                include_task_get=include_task_get,
            )
            text = outcome.text.strip()
            if not text:
                raise ValueError("成员返回为空")
            artifact = await self.artifacts.create(
                self.team_run_id,
                member_id=member_id,
                task_id=task_id,
                name=f"{role} · {str(task.get('title') or task_id)[:80]}",
                content=text,
            )
            await self.board.complete(
                self.team_run_id, task_id,
                member_id=member_id,
                lease_token=lease_token,
                artifact_id=artifact["artifact_id"],
            )
            await self._finish_member(
                member_id, MEMBER_COMPLETED, usage,
                last_error=None, completed_at=datetime.utcnow(),
            )
            await self._emit_member(member_id)
            await self._flush_usage(usage)
            return MemberResult(
                team_run_id=self.team_run_id,
                member_id=member_id,
                task_id=task_id,
                status="completed",
                output=text,
                artifact_id=artifact["artifact_id"],
                model_calls=usage["model_calls"],
                tool_calls=usage["tool_calls"],
                tokens=usage["tokens"],
                duration_ms=int((time.monotonic() - started) * 1000),
                thread_id=member["thread_id"],
                recovered=outcome.resumed,
            )
        except asyncio.CancelledError:
            await self.board.release(
                self.team_run_id, task_id,
                member_id=member_id, lease_token=lease_token, reason="团队已取消",
            )
            await self._finish_member(
                member_id, "cancelled", usage,
                last_error="团队已取消", completed_at=datetime.utcnow(),
            )
            await self._emit_member(member_id, reason="cancelled")
            await self._flush_usage(usage)
            raise
        except _INTERRUPT_ERRORS as exc:
            error = redact_secrets(str(exc))[:2000]
            await self.board.release(
                self.team_run_id, task_id,
                member_id=member_id, lease_token=lease_token, reason=f"成员执行中断: {error}",
            )
            await self._finish_member(
                member_id, MEMBER_INTERRUPTED, usage,
                last_error=error, recovery=True,
            )
            await self._emit_member(member_id, reason="interrupted")
            await self._flush_usage(usage)
            return MemberResult(
                team_run_id=self.team_run_id, member_id=member_id, task_id=task_id,
                status="interrupted", error=error, model_calls=usage["model_calls"],
                tool_calls=usage["tool_calls"], tokens=usage["tokens"],
                duration_ms=int((time.monotonic() - started) * 1000),
                thread_id=member["thread_id"],
            )
        except Exception as exc:  # noqa: BLE001
            error = redact_secrets(f"{type(exc).__name__}: {exc}")[:2000]
            await self.board.fail(
                self.team_run_id, task_id,
                member_id=member_id, lease_token=lease_token, error=error,
            )
            await self._finish_member(
                member_id, MEMBER_FAILED, usage,
                last_error=error, completed_at=datetime.utcnow(),
            )
            await self._emit_member(member_id, reason="failed")
            await self._flush_usage(usage)
            logger.warning(
                "团队成员执行失败 team_run_id=%s member_id=%s task_id=%s error=%s",
                self.team_run_id, member_id, task_id, type(exc).__name__,
            )
            return MemberResult(
                team_run_id=self.team_run_id, member_id=member_id, task_id=task_id,
                status="failed", error=error, model_calls=usage["model_calls"],
                tool_calls=usage["tool_calls"], tokens=usage["tokens"],
                duration_ms=int((time.monotonic() - started) * 1000),
                thread_id=member["thread_id"],
            )

    async def _run_agent(
        self,
        *,
        member: dict[str, Any],
        execution: MemberExecutionContext,
        profile: MemberProfile,
        is_leader: bool,
        usage: dict[str, int],
        resume_input: Optional[str] = None,
        stage_model_limit: Optional[int] = None,
        stage_tool_limit: Optional[int] = None,
        include_inbox: bool = False,
        include_task_get: bool = False,
        thread_id: Optional[str] = None,
    ) -> _RunOutcome:
        provider, model_config, _snapshot = await self._resolve_model(profile)
        thread_id = thread_id or member["thread_id"]
        member_ctx = self._member_runtime_context(
            member, execution.task_id, profile, provider, model_config,
            is_leader=is_leader, thread_id=thread_id,
        )
        used_model = int(member.get("model_calls") or 0)
        used_tools = int(member.get("tool_calls") or 0)
        remaining_model = max(0, int(profile.max_model_calls) - used_model)
        remaining_tools = max(0, int(profile.max_tool_calls) - used_tools)
        if stage_model_limit is not None and stage_model_limit > 0:
            remaining_model = min(remaining_model, int(stage_model_limit))
        if stage_tool_limit is not None and stage_tool_limit >= 0:
            remaining_tools = min(remaining_tools, int(stage_tool_limit))
        if remaining_model <= 0:
            raise BudgetExceededError(
                f"成员已用完模型调用预算（{profile.max_model_calls}）"
            )
        agent = await self._build_agent(
            member=member,
            execution=execution,
            profile=profile,
            provider=provider,
            model_config=model_config,
            is_leader=is_leader,
            member_ctx=member_ctx,
            remaining_model_calls=remaining_model,
            remaining_tool_calls=remaining_tools,
            include_inbox=include_inbox,
            include_task_get=include_task_get,
        )
        config = make_run_config(
            thread_id, self.run_id, self.parent_ctx.scope.user_id,
        )
        config["recursion_limit"] = max(40, remaining_model * 16 + 40)
        config["callbacks"] = [ModelUsageCallback(member_ctx)]
        state = await agent.aget_state(config)
        has_pending = bool(getattr(state, "next", None))
        if has_pending:
            payload: Any = None
        else:
            prompt = resume_input if resume_input is not None else self._task_prompt(execution)
            payload = {"messages": [HumanMessage(content=prompt)]}
        timeout = float(profile.timeout_seconds or 0)
        key = team_model_key(provider, model_config)
        try:
            async with self.capacity.slot(key):
                if timeout > 0:
                    async with asyncio.timeout(timeout):
                        result = await agent.ainvoke(payload, config)
                else:
                    result = await agent.ainvoke(payload, config)
        finally:
            usage["model_calls"] = int(member_ctx.model_calls)
            usage["tool_calls"] = int(member_ctx.tool_calls)
            usage["tokens"] = int(member_ctx.tokens_generated)
        text, truncated = _last_ai_text(result)
        return _RunOutcome(text=text, truncated=truncated, resumed=has_pending, member_ctx=member_ctx)

    async def _build_agent(
        self,
        *,
        member: dict[str, Any],
        execution: MemberExecutionContext,
        profile: MemberProfile,
        provider: dict[str, Any],
        model_config: dict[str, Any],
        is_leader: bool,
        member_ctx: RuntimeContext,
        remaining_model_calls: int,
        remaining_tool_calls: int,
        include_inbox: bool = True,
        include_task_get: bool = True,
    ):
        if self.agent_builder is not None:
            built = self.agent_builder(
                member=member,
                execution=execution,
                profile=profile,
                member_ctx=member_ctx,
                is_leader=is_leader,
            )
            return await built if inspect.isawaitable(built) else built
        return self._factory.build(
            member_ctx=member_ctx,
            execution=execution,
            profile=profile,
            provider=provider,
            model_config=model_config,
            is_leader=is_leader,
            remaining_model_calls=remaining_model_calls,
            remaining_tool_calls=remaining_tool_calls,
            include_inbox=include_inbox,
            include_task_get=include_task_get,
        )

    # ── 消息唤醒 / 恢复 ──────────────────────────────────────────────────

    async def deliver_inbox(self, member_id: str) -> Optional[MemberResult]:
        """空闲成员被消息唤醒：读取 Inbox → 同 Thread 续跑 → 保存产物并通知 Leader。"""
        member = await self.get_member(member_id)
        if member is None:
            return None
        if member["status"] in {MEMBER_COMPLETED, MEMBER_FAILED, "cancelled"}:
            return None
        messages = await self.messages.list_inbox(
            self.team_run_id, member_id, limit=10,
        )
        if not messages:
            return None
        profile = MemberProfile.from_snapshot(member["profile_snapshot"] or {
            "role": member["role"], "name": member["name"],
        })
        task_id = member.get("task_id")
        task: dict[str, Any] = {"id": task_id or "", "role": member["role"], "title": "", "brief": ""}
        if task_id:
            try:
                current = await self.board.get_task(self.team_run_id, task_id)
                task = {
                    "id": current["id"], "role": current["role"],
                    "title": current["title"], "brief": current["brief"],
                }
            except TeamNotFound:
                pass
        await self._prepare_member(member_id)
        await self._set_member_status(
            member_id, MEMBER_RUNNING,
            sources=frozenset({MEMBER_READY, MEMBER_WAITING, MEMBER_INTERRUPTED}),
            started_at=datetime.utcnow(),
        )
        started = time.monotonic()
        usage = {"model_calls": 0, "tool_calls": 0, "tokens": 0}
        execution = MemberExecutionContext(
            team_run_id=self.team_run_id,
            member_id=member_id,
            role=member["role"],
            thread_id=member["thread_id"],
            task_id=task_id,
            objective=str((await self._team_objective())[:4000]),
            task_brief=f"{task.get('title') or ''}\n{task.get('brief') or ''}".strip(),
            incoming_messages=messages,
            profile=profile,
        )
        try:
            outcome = await self._run_agent(
                member=member, execution=execution, profile=profile,
                is_leader=False, usage=usage, include_inbox=True, include_task_get=True,
            )
            text = outcome.text.strip()
            artifact_id = None
            if text:
                artifact = await self.artifacts.create(
                    self.team_run_id,
                    member_id=member_id,
                    task_id=task_id,
                    name=f"{member['role']} · 消息处理输出",
                    content=text,
                )
                artifact_id = artifact["artifact_id"]
                await self.messages.send(
                    self.team_run_id,
                    sender_member_id=member_id,
                    recipient_member_id=self.leader_member_id,
                    message_type="result",
                    payload={"text": "已处理团队消息并产出补充成果"},
                    artifact_id=artifact_id,
                )
            for message in messages:
                try:
                    await self.messages.ack(
                        self.team_run_id, member_id, message["message_id"],
                    )
                except Exception:  # noqa: BLE001 单条 ack 失败不回滚已处理内容
                    logger.warning("成员消息 ack 失败 member_id=%s", member_id)
            await self._finish_member(
                member_id, MEMBER_WAITING, usage, last_error=None,
            )
            await self._emit_member(member_id, reason="message_wake")
            await self._flush_usage(usage)
            return MemberResult(
                team_run_id=self.team_run_id, member_id=member_id, task_id=task_id,
                status="completed", output=text, artifact_id=artifact_id,
                model_calls=usage["model_calls"], tool_calls=usage["tool_calls"],
                tokens=usage["tokens"], duration_ms=int((time.monotonic() - started) * 1000),
                thread_id=member["thread_id"], recovered=outcome.resumed,
            )
        except asyncio.CancelledError:
            await self._finish_member(
                member_id, "cancelled", usage,
                last_error="团队已取消", completed_at=datetime.utcnow(),
            )
            await self._flush_usage(usage)
            raise
        except Exception as exc:  # noqa: BLE001
            error = redact_secrets(f"{type(exc).__name__}: {exc}")[:2000]
            await self._finish_member(
                member_id, MEMBER_FAILED, usage,
                last_error=error, completed_at=datetime.utcnow(),
            )
            await self._emit_member(member_id, reason="failed")
            await self._flush_usage(usage)
            return MemberResult(
                team_run_id=self.team_run_id, member_id=member_id, task_id=task_id,
                status="failed", error=error,
                model_calls=usage["model_calls"], tool_calls=usage["tool_calls"],
                tokens=usage["tokens"], duration_ms=int((time.monotonic() - started) * 1000),
                thread_id=member["thread_id"],
            )

    async def wake_idle_members(self, *, max_count: int = 2) -> list[MemberResult]:
        """调度周期内唤醒有待处理消息的空闲成员（有界，避免消息风暴）。"""
        results: list[MemberResult] = []
        for member in await self.directory.list_members(self.team_run_id):
            if len(results) >= max_count:
                break
            if member["member_id"] == self.leader_member_id:
                continue
            if member["status"] not in {MEMBER_READY, MEMBER_WAITING}:
                continue
            result = await self.deliver_inbox(member["member_id"])
            if result is not None:
                results.append(result)
        return results

    # ── 内部辅助 ─────────────────────────────────────────────────────────

    async def _resolve_model(
        self, profile: MemberProfile,
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        override = profile.model_config_id
        if override is not None and override != self.model_config.get("id"):
            if self.model_loader is None:
                from .model_routes import load_model_route

                self.model_loader = load_model_route
            provider, model_config, snapshot = await self.model_loader(override)
            return provider, model_config, snapshot
        return self.provider, self.model_config, model_snapshot(
            self.provider, self.model_config, self.model_config.get("id"),
        )

    def _member_runtime_context(
        self,
        member: dict[str, Any],
        task_id: Optional[str],
        profile: MemberProfile,
        provider: dict[str, Any],
        model_config: dict[str, Any],
        *,
        is_leader: bool = False,
        thread_id: Optional[str] = None,
    ) -> RuntimeContext:
        parent = self.parent_ctx
        budget = ExecutionBudget(
            max_steps=max(1, int(profile.max_model_calls)),
            max_tool_calls=max(0, int(profile.max_tool_calls)),
            max_model_calls=max(1, int(profile.max_model_calls)),
            run_timeout_seconds=float(profile.timeout_seconds or 0),
        )
        agent_config = dict(parent.agent_config or {})
        agent_config.pop("_memory_context", None)
        agent_config["team_config"] = self.team_config
        return RuntimeContext(
            scope=parent.scope,
            run_id=parent.run_id,
            thread_id=thread_id or member["thread_id"],
            agent_id=parent.agent_id,
            session_id=parent.session_id,
            project_id=parent.project_id,
            trace_id=parent.trace_id,
            model_id=model_config.get("name"),
            agent_config=agent_config,
            memory_context="",
            history_context="",
            budget=budget,
            enable_hitl=False,
            sandbox_mode=parent.sandbox_mode,
            allow_network=parent.allow_network,
            worker_id=parent.worker_id,
            lease_version=parent.lease_version,
            goal_mode=parent.goal_mode,
            team_context={
                "team_run_id": self.team_run_id,
                "member_id": member["member_id"],
                "role": member["role"],
                "is_leader": bool(is_leader),
                "task_id": task_id,
                "leader_member_id": self.leader_member_id,
                "board": self.board,
                "messages": self.messages,
                "artifacts": self.artifacts,
                "members": self.directory,
            },
        )

    @staticmethod
    def _task_prompt(execution: MemberExecutionContext) -> str:
        lines = [
            f"团队目标：{execution.objective}",
            f"你的角色：{execution.role}",
            f"任务：{execution.task_brief}",
        ]
        if execution.findings:
            lines.append("复核指出的缺口：" + "；".join(
                str(item.get("evidence") or "") for item in execution.findings
            ))
        if execution.prior_result:
            lines.append("上一版成果（在其基础上完善）：\n" + execution.prior_result[:6000])
        lines.append("请完成任务并直接输出完整交付正文。")
        return "\n\n".join(lines)

    async def _prepare_member(self, member_id: str) -> None:
        async with AsyncSessionLocal() as db:
            repo = MemberRepository(db)
            row = await repo.get(self.team_run_id, member_id)
            if row is None:
                raise TeamNotFound(f"成员 {member_id} 不存在")
            if row.status == MEMBER_RUNNING:
                # 进程重启/上次执行未收尾：先标记中断，再从自身 checkpoint 恢复
                await repo.transition(
                    self.team_run_id, member_id,
                    target=MEMBER_INTERRUPTED,
                    sources=frozenset({MEMBER_RUNNING}),
                    last_error="成员上次执行未正常收尾，准备恢复",
                )
                await repo.increment_recovery(self.team_run_id, member_id)
                row = await repo.get(self.team_run_id, member_id)
            if row.status in {MEMBER_INTERRUPTED, MEMBER_FAILED, "cancelled"}:
                await repo.transition(
                    self.team_run_id, member_id,
                    target=MEMBER_READY,
                    sources=frozenset({MEMBER_INTERRUPTED, MEMBER_FAILED, "cancelled"}),
                )
            await db.commit()

    async def _set_member_status(
        self,
        member_id: str,
        target: str,
        *,
        sources: Any,
        **fields: Any,
    ) -> bool:
        async with AsyncSessionLocal() as db:
            ok = await MemberRepository(db).transition(
                self.team_run_id, member_id, target=target, sources=sources, **fields,
            )
            await db.commit()
            if not ok:
                logger.warning(
                    "成员状态迁移未生效 team_run_id=%s member_id=%s target=%s",
                    self.team_run_id, member_id, target,
                )
            return ok

    async def _finish_member(
        self,
        member_id: str,
        target: str,
        usage: dict[str, int],
        *,
        last_error: Optional[str],
        completed_at: Optional[datetime] = None,
        recovery: bool = False,
    ) -> None:
        sources = frozenset({MEMBER_RUNNING, MEMBER_WAITING, MEMBER_READY})
        if target == MEMBER_WAITING:
            sources = frozenset({MEMBER_RUNNING})
        async with AsyncSessionLocal() as db:
            repo = MemberRepository(db)
            ok = await repo.transition(
                self.team_run_id, member_id,
                target=target,
                sources=sources,
                last_error=(last_error[:2000] if last_error else None),
                **({"completed_at": completed_at} if completed_at else {}),
            )
            if not ok:
                await repo.update_fields(
                    self.team_run_id, member_id,
                    last_error=(last_error[:2000] if last_error else None),
                )
            await repo.add_usage(
                self.team_run_id, member_id,
                model_calls=int(usage.get("model_calls") or 0),
                tool_calls=int(usage.get("tool_calls") or 0),
                tokens=int(usage.get("tokens") or 0),
            )
            if recovery:
                await repo.increment_recovery(self.team_run_id, member_id)
            await db.commit()

    async def _flush_usage(self, usage: dict[str, int]) -> None:
        """把成员用量累计到父 Run 上下文并持久化镜像（团队级预算可见）。"""
        deltas = {
            "model_calls": int(usage.get("model_calls") or 0),
            "tool_calls": int(usage.get("tool_calls") or 0),
            "tokens": int(usage.get("tokens") or 0),
        }
        if not any(deltas.values()):
            return
        async with self.usage_lock:
            self.parent_ctx.model_calls += deltas["model_calls"]
            self.parent_ctx.tool_calls += deltas["tool_calls"]
            self.parent_ctx.tokens_generated += deltas["tokens"]
            self.parent_ctx.steps += deltas["model_calls"]
            if self.flush_budget is not None:
                try:
                    await self.flush_budget(self.parent_ctx, self.run_handle)
                except Exception:  # noqa: BLE001 预算镜像失败不改变成员执行结果
                    logger.warning("团队预算镜像刷新失败", exc_info=True)

    async def _emit_member(self, member_id: str, *, reason: str = "") -> None:
        member = await self.get_member(member_id)
        if member is None:
            return
        data = {
            "team_run_id": self.team_run_id,
            "member_id": member["member_id"],
            "role": member["role"],
            "name": member["name"],
            "status": member["status"],
            "task_id": member.get("task_id"),
            "thread_id": member["thread_id"],
            "model_calls": member.get("model_calls", 0),
            "tool_calls": member.get("tool_calls", 0),
            "tokens": member.get("tokens", 0),
            "recovery_count": member.get("recovery_count", 0),
            "last_error": (member.get("last_error") or "")[:400] or None,
        }
        if reason:
            data["reason"] = reason
        try:
            await self.bus.publish(RuntimeEvent(
                type=TEAM_MEMBER_UPDATED, run_id=self.run_id, data=data,
            ))
        except Exception:  # noqa: BLE001 事件失败不影响成员执行
            logger.warning("团队成员事件发布失败 member_id=%s", member_id)

    async def _team_objective(self) -> str:
        async with AsyncSessionLocal() as db:
            from sqlalchemy import select

            from app.models.team import TeamRun

            value = await db.scalar(
                select(TeamRun.objective).where(TeamRun.team_run_id == self.team_run_id)
            )
            return str(value or "")

    @staticmethod
    def _member_dict(row) -> dict[str, Any]:
        if row is None:
            return {}
        return {
            "member_id": row.member_id,
            "team_run_id": row.team_run_id,
            "parent_member_id": row.parent_member_id,
            "task_id": row.task_id,
            "role": row.role,
            "name": row.name,
            "thread_id": row.thread_id,
            "status": row.status,
            "profile_snapshot": row.profile_snapshot or {},
            "model_snapshot": row.model_snapshot or {},
            "budget": row.budget or {},
            "model_calls": row.model_calls,
            "tool_calls": row.tool_calls,
            "tokens": row.tokens,
            "recovery_count": row.recovery_count,
            "last_error": row.last_error,
            "started_at": row.started_at.isoformat() if row.started_at else None,
            "completed_at": row.completed_at.isoformat() if row.completed_at else None,
        }
