"""将团队图接入 Run 执行租约、模型预算及平台授权。"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Awaitable, Callable
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from sqlalchemy import func, select

from app.core.database import AsyncSessionLocal
from app.models.agent import AgentRunEvent
from app.observability.langfuse_tracing import platform_span
from app.services.documents.workflow_support import read_attachments

from .context import RuntimeContext
from .events import TEAM_UPDATED, EventBus, RuntimeEvent, redact_secrets
from .lifecycle import RunHandle
from .model_adapter import build_chat_model
from .persistence import make_run_config
from .repositories import RunRepository
from .repositories.errors import RunLeaseLost
from .team_graph import (
    TeamGraphContext, TeamResponseTruncated, build_team_graph, drive_team_graph, needs_evidence_review, terminal_team_snapshot,
)
from .tools.adapter import ToolAdapterFactory
from .tools.classification import CLASS_READ_ONLY, get_side_effect_contract
from .tools.policy import BudgetExceededError, ToolPolicy, get_tool_risk


_MODEL_CALL_LIMIT = 12
_DEEP_MODEL_CALL_LIMIT = 20
_PROJECT_MODEL_CALL_LIMIT = 40
_MAX_TOOL_CALLS_PER_MEMBER = 2
_MAX_REFERENCE_CHARS = 16_000


def team_thread_id(run_id: str) -> str:
    return f"team:{run_id}"


async def _latest_event(db, run_id: str, event_type: str) -> dict | None:
    later = select(func.max(AgentRunEvent.sequence)).where(
        AgentRunEvent.run_id == run_id, AgentRunEvent.event_type == event_type,
    ).scalar_subquery()
    return await db.scalar(select(AgentRunEvent.payload).where(
        AgentRunEvent.run_id == run_id, AgentRunEvent.sequence == later,
    ))


class TeamRun:
    def __init__(self, run: RunHandle, runtime: RuntimeContext, provider: dict, model_config: dict,
                 policy: ToolPolicy, tool_specs: list[dict], flush_budget: Callable[..., Awaitable[None]],
                 *, project_scope: str | None = None):
        self.run = run
        self.runtime = runtime
        self.flush_budget = flush_budget
        self.budget_lock = asyncio.Lock()
        self.project_scope = project_scope
        self.model_call_limit = _PROJECT_MODEL_CALL_LIMIT if project_scope else _MODEL_CALL_LIMIT
        # 每次调用均纳入预算，关闭 SDK 内部的隐藏重试。
        model_settings = {**model_config, "extra_params": {**(model_config.get("extra_params") or {}), "max_retries": 0}}
        max_output = 8192 if project_scope else 4096
        self.model = build_chat_model(
            provider, model_settings, streaming=False, require_tools=False,
            temperature=runtime.agent_config.get("temperature"),
            max_tokens=min(int(model_config.get("max_tokens") or max_output), max_output),
        )
        # 成员只调用助手已获授权的只读工具。写入由原有业务流程执行。
        allowed = []
        if not project_scope and model_config.get("supports_function_call"):
            for spec in tool_specs:
                name = spec["name"]
                risk = get_tool_risk(name)
                if (risk and name in policy.allowed_tools and name not in policy.hitl_tools
                        and not risk.requires_approval and (not risk.network_access or policy.allow_network)
                        and get_side_effect_contract(name).side_effect_class == CLASS_READ_ONLY):
                    allowed.append(spec)
        tools = ToolAdapterFactory(runtime, policy, self.run.bus).build(allowed)
        self.tools = {tool.name: tool for tool in tools}
        self.tool_model = self.model.bind_tools(tools) if tools else None

    async def _check_fence(self, db) -> None:
        lease = self.run.execution_lease()
        if lease is None:
            raise RunLeaseLost("团队缺少有效执行租约")
        if await RunRepository(db).verify_running_lease(lease):
            raise asyncio.CancelledError

    def _check_budget(self, *, models: int = 0, tools: int = 0) -> None:
        ctx = self.runtime
        reason = ctx.budget.exhausted_reason(ctx.steps + models, ctx.tool_calls + tools, ctx.model_calls + models)
        if reason:
            raise BudgetExceededError(reason)
        if models and ctx.model_calls + models > self.model_call_limit:
            raise BudgetExceededError(f"本轮团队达到 {self.model_call_limit} 次模型调用上限")

    def remaining_model_calls(self) -> int:
        ctx = self.runtime
        limits = [self.model_call_limit - ctx.model_calls]
        if ctx.budget.max_model_calls > 0:
            limits.append(ctx.budget.max_model_calls - ctx.model_calls)
        if ctx.budget.max_steps > 0:
            limits.append(ctx.budget.max_steps - ctx.steps)
        return max(0, min(limits))

    async def _model(self, stage: str, messages: list, *, tool_model=False):
        async with self.budget_lock:
            async with AsyncSessionLocal() as db:
                await self._check_fence(db)
            self._check_budget(models=1)
            self.runtime.model_calls += 1
            self.runtime.model_node_runs += 1
            self.runtime.steps += 1
            await self.flush_budget(self.runtime, self.run)
        model = self.tool_model if tool_model else self.model
        with platform_span(f"team_{stage}", run_id=self.run.run_id, record_exception_detail=False):
            reply = await model.ainvoke(messages)
        async with self.budget_lock:
            self.runtime.tokens_generated += int((reply.usage_metadata or {}).get("output_tokens") or 0)
            await self.flush_budget(self.runtime, self.run)
        return reply

    async def invoke(self, stage: str, system: str, user: str) -> str:
        reply = await self._model(stage, [SystemMessage(content=system), HumanMessage(content=user)])
        return self._completed_text(reply)

    @staticmethod
    def _completed_text(reply) -> str:
        text = str(redact_secrets(reply.text))
        if reply.response_metadata.get("finish_reason") in {"length", "max_tokens", "max_output_tokens"}:
            raise TeamResponseTruncated(text)
        return text

    async def worker(self, task: dict, system: str, user: str) -> str:
        messages = [SystemMessage(content=system), HumanMessage(content=user)]
        if self.tool_model is None:
            return await self.invoke("worker", system, user)
        reply = await self._model("worker", messages, tool_model=True)
        text = self._completed_text(reply)
        calls = reply.tool_calls[:_MAX_TOOL_CALLS_PER_MEMBER]
        if not calls:
            return text
        messages.append(AIMessage(content=reply.content, tool_calls=calls))
        for index, call in enumerate(calls):
            name = call["name"]
            tool = self.tools.get(name)
            result = "当前成员没有此工具的权限"
            if tool is not None:
                # 续跑可复用参数一致的只读结果，模型重新选读时使用独立调用身份。
                identity = json.dumps([task["id"], index, name, call["args"]], sort_keys=True, ensure_ascii=False)
                nonce = hashlib.sha256(identity.encode()).hexdigest()[:24]
                async with self.budget_lock:
                    self._check_budget(tools=1)
                    value = await tool.ainvoke({"name": name, "args": call["args"], "id": f"team-{nonce}", "type": "tool_call"})
                    await self.flush_budget(self.runtime, self.run)
                result = str(value.content if isinstance(value, ToolMessage) else value)[:4000]
            messages.append(ToolMessage(content=result, tool_call_id=call["id"]))
        answer = await self._model("worker", messages)
        return self._completed_text(answer)

    async def emit(self, event_type: str, payload: dict[str, Any]) -> None:
        event = RuntimeEvent(type=event_type, run_id=self.run.run_id, thread_id=self.run.thread_id, data=payload)
        async with AsyncSessionLocal() as db:
            await self._check_fence(db)
            if await _latest_event(db, self.run.run_id, event_type) == redact_secrets(payload):
                return
            event.sequence = await EventBus.persist_in_transaction(event, db)
            await db.commit()
        self.run.bus.notify_persisted(event)

    async def drive(self, checkpointer, messages: list[dict], route: dict, *, synthesize: bool) -> dict:
        if self.project_scope:
            from .team_project_runtime import drive_project

            return await drive_project(self, checkpointer, messages, route)
        context = TeamGraphContext(self.invoke, self.emit, worker=self.worker,
                                   remaining_model_calls=self.remaining_model_calls)
        graph = build_team_graph(checkpointer, context)
        config = make_run_config(team_thread_id(self.run.run_id), self.run.run_id, self.runtime.scope.user_id)
        state = await graph.aget_state(config)
        initial = {} if state.values else await self._initial(messages, route, synthesize=synthesize)
        if state.values.get("deep_review") or (synthesize and needs_evidence_review(initial.get("objective", ""))):
            self.model_call_limit = _DEEP_MODEL_CALL_LIMIT
        return await drive_team_graph(graph, config, initial)

    async def _initial(self, messages: list[dict], route: dict, *, synthesize: bool) -> dict:
        turns = [(entry.get("role"), entry["content"]) for entry in messages
                 if isinstance(entry, dict) and entry.get("role") in ("user", "assistant")
                 and isinstance(entry.get("content"), str)]
        current = turns[-1][1] if turns and turns[-1][0] == "user" else ""
        request_text, _, document_preview = current.partition("\n\n[参考文档]")
        objective, _, attachments = request_text.partition("\n\n[附件]")
        clarification = (route.get("document_context") or {}).get("request")
        objective = f"{clarification}\n本轮要求：{objective}" if clarification and clarification != objective else objective
        recent = [f"{'用户' if role == 'user' else '助手'}：{text[-1900:]}" for role, text in turns[:-1][-6:]]
        refs = list(route.get("attachment_refs") or [])
        if source := route.get("source_attachment"):
            if all(ref["path"] != source["path"] for ref in refs):
                refs.append(source)
        sources, warnings = await read_attachments(self.run, refs) if refs else ([], [])
        template_paths = set((route.get("document_context") or {}).get("template_paths") or [])
        template_hashes = {ref["sha256"] for ref in refs if ref["path"] in template_paths}
        notes = ["已绑定文档摘录：\n" + document_preview[:8500]] if document_preview else []
        for item in sources:
            reference = item["reference"]
            if reference["locator"] in template_hashes:
                headings = "、".join(section["title"] for section in item["outline"] if section["title"])
                notes.append(f"版式模板 {reference['label']}（只参考结构）：{headings[:1600]}")
            else:
                notes.append(f"附件 {reference['label']}：\n{item['content'][:3800]}")
        if attachments and not notes:
            notes.append("本轮有附件。团队上下文未取得可用的文本摘录。")
        return {
            "objective": objective.strip()[:12_000] or "根据本轮附件和对话背景完成分析。",
            "reference_context": "\n\n".join([*notes, *warnings[:3], *recent])[:_MAX_REFERENCE_CHARS],
            "assistant_prompt": str(self.runtime.agent_config.get("system_prompt") or "")[:6000],
            "synthesize": synthesize,
        }


async def close_team_board(run: RunHandle, phase: str) -> None:
    """Run 终态已提交时，关闭仍有未决分工的可回放任务板。"""
    async with AsyncSessionLocal() as db:
        previous = await _latest_event(db, run.run_id, TEAM_UPDATED)
    if not previous or previous.get("phase") not in {"completed", "failed", "cancelled"}:
        await run.bus.publish(RuntimeEvent(type=TEAM_UPDATED, run_id=run.run_id,
                                           data=terminal_team_snapshot(previous, phase)))
