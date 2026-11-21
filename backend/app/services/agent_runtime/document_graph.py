"""文档图与既有 Run 租约、工具、事件和预算的连接。"""
from __future__ import annotations

import asyncio
from typing import Any

from langchain_core.messages import ToolMessage
from langgraph.types import Command
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import AsyncSessionLocal
from app.models.agent import AgentRunEvent
from app.models.document import RunInputRequest
from app.services.mcp.registry import parse_mcp_tool_name

from .context import RuntimeContext
from .events import EventBus, RuntimeEvent
from .lifecycle import RunHandle
from .model_adapter import build_chat_model
from .repositories import RunRepository
from .repositories.errors import RunLeaseLost
from .run_input import pause_for_input, response_for_interrupt
from .tools.adapter import ToolAdapterFactory
from .tools.classification import CLASS_READ_ONLY, get_side_effect_contract
from .tools.policy import BudgetExceededError, ToolPolicy, ToolPolicyError, get_tool_risk

_RESULT_EVENTS = {
    "artifact.created", "document.version.created", "document.profile_saved",
    "document.edit.review_required", "document.edit.reviewed",
}


class DocumentGraphContext:
    def __init__(
        self,
        run: RunHandle,
        runtime: RuntimeContext,
        provider: dict[str, Any],
        model_config: dict[str, Any],
        policy: ToolPolicy,
        tool_specs: list[dict[str, Any]],
        flush_budget,
    ):
        self.run = run
        self.runtime = runtime
        self._flush_budget = flush_budget
        self.summary: dict[str, Any] = {}
        self.supports_vision = bool(model_config.get("supports_vision"))
        self.model = build_chat_model(
            provider, model_config, streaming=False,
            temperature=runtime.agent_config.get("temperature"), require_tools=False,
        )
        allowed_specs = []
        if model_config.get("supports_function_call"):
            for spec in tool_specs:
                name = spec["name"]
                risk = get_tool_risk(name)
                mcp = parse_mcp_tool_name(name)
                registration = runtime.agent_config.get("mcp_runtime", {}).get(mcp[0], {}) if mcp else {}
                tool_registration = registration.get("tools", {}).get(name, {})
                approved_mcp = (
                    mcp is not None and policy.allow_network
                    and tool_registration.get("side_effect_class") == CLASS_READ_ONLY
                    and tool_registration.get("requires_approval") is False
                )
                if (
                    risk is not None and name in policy.allowed_tools
                    and not risk.requires_approval and name not in policy.hitl_tools
                    and risk.side_effect_class == CLASS_READ_ONLY
                    and (get_side_effect_contract(name).side_effect_class == CLASS_READ_ONLY or approved_mcp)
                    and (not risk.network_access or policy.allow_network)
                ):
                    allowed_specs.append(spec)
        self.tools = ToolAdapterFactory(runtime, policy, run.bus).build(allowed_specs)
        self._tools_by_name = {tool.name: tool for tool in self.tools}

    async def check_fence(self, db: AsyncSession) -> None:
        """锁定 Run；提交版本的事务应在该锁释放前完成。"""
        lease = self.run.execution_lease()
        if lease is None:
            raise RunLeaseLost("文档工作流缺少有效执行租约")
        if await RunRepository(db).verify_running_lease(lease):
            raise asyncio.CancelledError

    def _check_budget(self) -> None:
        reason = self.runtime.budget.exhausted_reason(
            self.runtime.steps, self.runtime.tool_calls, self.runtime.model_calls,
        )
        if reason:
            raise BudgetExceededError(reason)

    async def step(self, name: str) -> None:
        async with AsyncSessionLocal() as db:
            await self.check_fence(db)
        self.runtime.steps += 1
        self.summary["last_document_stage"] = name
        await self._flush_budget(self.runtime, self.run)
        self._check_budget()

    async def invoke_tool(self, name: str, args: dict[str, Any], call_id: str) -> str:
        tool = self._tools_by_name.get(name)
        if tool is None:
            raise ToolPolicyError(f"当前助手未授权文档工作流调用工具 {name}")
        result = await tool.ainvoke({"name": name, "args": args, "id": call_id, "type": "tool_call"})
        await self._flush_budget(self.runtime, self.run)
        return str(result.content if isinstance(result, ToolMessage) else result)

    async def emit(self, event_type: str, payload: dict[str, Any]) -> None:
        event = RuntimeEvent(
            type=event_type, run_id=self.run.run_id, thread_id=self.run.thread_id, data=payload,
        )
        async with AsyncSessionLocal() as db:
            await self.check_fence(db)
            if event_type in _RESULT_EVENTS:
                # 保存和事件是独立事务，工作流节点重放时交付事件仍只出现一次。
                exists = await db.scalar(select(AgentRunEvent.sequence).where(
                    AgentRunEvent.run_id == self.run.run_id, AgentRunEvent.event_type == event_type,
                ).limit(1))
                if exists is not None:
                    return
            event.sequence = await EventBus.persist_in_transaction(event, db)
            await db.commit()
        self.run.bus.notify_persisted(event)
        if event_type == "artifact.created":
            self._record_artifact(payload)

    def _record_artifact(self, payload: dict[str, Any]) -> None:
        self.summary.update({
            "document_id": payload.get("document_id"),
            "document_version": payload.get("version_no"),
            "output_file_bytes": payload.get("size"),
        })

    async def finish(self, state: dict[str, Any]) -> None:
        await self._flush_budget(self.runtime, self.run)
        self._check_budget()
        route = state.get("execution_context") or {}
        for key in ("task_type", "document_profile_id", "document_profile_version", "document_id", "document_version"):
            if route.get(key) is not None:
                self.summary.setdefault(key, route[key])
        self.summary["input_file_count"] = len(route.get("attachment_refs") or [])
        if (state.get("validation") or {}).get("passed") is not None:
            self.summary["document_validation_passed"] = bool(state["validation"]["passed"])
        async with AsyncSessionLocal() as db:
            self.summary["input_request_count"] = await db.scalar(
                select(func.count()).select_from(RunInputRequest).where(
                    RunInputRequest.run_id == self.run.run_id, RunInputRequest.status == "submitted",
                )
            )
            if "output_file_bytes" not in self.summary:
                artifact = await db.scalar(select(AgentRunEvent.payload).where(
                    AgentRunEvent.run_id == self.run.run_id, AgentRunEvent.event_type == "artifact.created",
                ).limit(1))
                if artifact is not None:
                    self._record_artifact(artifact)


async def drive_document_graph(graph, config, initial: dict[str, Any], context: DocumentGraphContext) -> None:
    """按已提交 checkpoint 续跑，配对当前 interrupt 与永久存储的补充内容。"""
    async with AsyncSessionLocal() as db:
        await context.check_fence(db)
    snapshot = await graph.aget_state(config)
    payload: Any = initial
    if snapshot.interrupts:
        interrupt = snapshot.interrupts[0]
        response = await response_for_interrupt(context.run.run_id, interrupt.id, interrupt.value)
        if response is None:
            await pause_for_input(context.run, interrupt.id, interrupt.value)
            return
        payload = Command(resume={interrupt.id: response})
    elif snapshot.next:
        payload = None
    elif snapshot.values:
        # 上次 Worker 已完成 LangGraph，可能尚未来得及提交 Run 终态。
        await context.finish(snapshot.values)
        context.run.final_text_parts = [snapshot.values.get("final_text") or ""]
        return
    result = await graph.ainvoke(payload, config, durability="sync")
    interrupts = result.get("__interrupt__") or ()
    if interrupts:
        await context.finish(result)
        await pause_for_input(context.run, interrupts[0].id, interrupts[0].value)
    else:
        await context.finish(result)
        context.run.final_text_parts = [result.get("final_text") or ""]
