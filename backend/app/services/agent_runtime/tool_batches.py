"""合并同轮重复工具请求，并在连续工具调用没有进展时停止。"""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import AIMessage, ToolMessage

_READ_TOOLS = {"read_file", "ls", "glob", "grep", "verify_workspace_project"}
_WORKSPACE_WRITES = {"write_file", "edit_file", "delete"}
_TODO_STATUSES = {"pending", "in_progress", "completed"}


class ToolLoopStopped(Exception):
    """已观察到工具请求与结果连续重复，执行可以保留进度后结束。"""


def _tool_round_signature(message: AIMessage, outputs: dict[str, ToolMessage]) -> tuple[bytes, ...] | None:
    if not message.tool_calls or len(outputs) != len(message.tool_calls):
        return None
    fingerprints = []
    for call in message.tool_calls:
        output = outputs.get(call["id"])
        name = call["name"]
        if output is None or not (
            name in _READ_TOOLS or name == "write_todos"
            or (name in _WORKSPACE_WRITES and output.status == "error")
        ):
            # 允许所有成功的工作区写入与外部操作继续。只根据已完成工具的真实回执判断。
            return None
        try:
            raw = json.dumps([name, call["args"], output.status, output.content], sort_keys=True)
        except (TypeError, ValueError, RecursionError):
            return None
        fingerprints.append(hashlib.sha256(raw.encode()).digest())
    return tuple(sorted(fingerprints))


def _repeating_tool_rounds(messages: list) -> bool:
    rounds = []
    outputs = {}
    for message in reversed(messages):
        if isinstance(message, ToolMessage):
            outputs[message.tool_call_id] = message
            continue
        if not isinstance(message, AIMessage) or not (signature := _tool_round_signature(message, outputs)):
            break
        rounds.append(signature)
        outputs.clear()
        # 同一批工具重复三轮，或交替执行两批工具却取得同样回执，均属于停滞。
        if len(rounds) == 3 and rounds[0] == rounds[1] == rounds[2]:
            return True
        if len(rounds) == 6:
            return rounds[:2] == rounds[2:4] == rounds[4:6]
    return False


def _todo_rows(args: dict) -> list[dict] | None:
    if set(args) != {"todos"}:
        return None
    rows = args["todos"]
    if isinstance(rows, str):
        try:
            rows = json.loads(rows)
        except (ValueError, RecursionError):
            return None
    if not isinstance(rows, list) or any(
        not isinstance(item, dict) or set(item) != {"content", "status"}
        or not isinstance(item["content"], str) or not isinstance(item["status"], str)
        or item["status"] not in _TODO_STATUSES for item in rows
    ):
        return None
    return rows


def _normalize_message(message: AIMessage, previous_todos: list[dict]) -> AIMessage:
    plans = []
    calls = []
    seen = set()
    malformed_plan = False
    for call in message.tool_calls:
        name, args = call["name"], call["args"]
        if name == "write_todos":
            if (todos := _todo_rows(args)) is not None:
                plans.append((len(calls), todos))
            elif malformed_plan:
                continue
            else:
                malformed_plan = True  # 留一次参数报错，避免同批坏参数被当成大量独立任务。
        if name in _READ_TOOLS:
            try:
                key = name, json.dumps(args, ensure_ascii=False, sort_keys=True)
            except (TypeError, ValueError, RecursionError):
                pass
            else:
                if key in seen:
                    continue
                seen.add(key)
        calls.append(call)
    if plans:
        first, initial = plans[0]
        if malformed_plan or any(todos != initial for _, todos in plans[1:]):
            # 多次并行提交部分计划时，先保留之前的计划，再依次应用本轮状态更新。
            by_content = {item["content"]: item for item in previous_todos}
            for _, todos in plans:
                by_content.update((item["content"], item) for item in todos)
            initial = list(by_content.values())
        calls[first] = {**calls[first], "args": {"todos": initial}}
        for index, _ in reversed(plans[1:]):
            del calls[index]
    if calls == message.tool_calls:
        return message
    extra = {key: value for key, value in message.additional_kwargs.items() if key != "tool_calls"}
    return message.model_copy(update={"tool_calls": calls, "additional_kwargs": extra})


class NativeToolBatchMiddleware(AgentMiddleware):
    """合并同批重复查询和计划请求，并识别跨轮工具停滞。"""

    async def abefore_model(self, state, runtime):
        if _repeating_tool_rounds(state.get("messages") or []):
            raise ToolLoopStopped("工具执行连续返回相同结果")

    async def awrap_model_call(self, request, handler):
        response = await handler(request)
        todos = request.state.get("todos") or []
        messages = [_normalize_message(message, todos) if isinstance(message, AIMessage) and message.tool_calls else message
                    for message in response.result]
        return replace(response, result=messages)
