"""在同一个可恢复的执行图中验收长任务，并把缺口交回执行节点。"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
import re
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any, NotRequired
from zipfile import BadZipFile, ZipFile

from langchain.agents.middleware import AgentMiddleware, AgentState, hook_config
from langchain_core.messages import AIMessage, HumanMessage
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.assistant.task_contract import TaskContract, build_task_contract
from app.assistant.verification import CheckResult, ToolEvidence, verify_task
from app.core.config import settings
from app.core.database import AsyncSessionLocal
from app.models.agent import AgentRun, AgentRunEvent, AgentToolExecution

from .backends import RUN_ARTIFACT_FOLDER, workspace_root
from .context import RuntimeContext
from .events import TOOL_COMPLETED, TOOL_FAILED, TOOL_STARTED
from .tools.classification import CLASS_EXTERNAL_IDEMPOTENT, CLASS_NON_IDEMPOTENT, get_side_effect_contract
from .tools.policy import BudgetExceededError


class CompletionState(AgentState):
    completion_review: NotRequired[dict[str, Any]]


_FILE_TOOLS = frozenset({"write_file", "edit_file"})
_LIMIT_FINISH_REASONS = frozenset({"length", "max_tokens", "max_output_tokens"})
# MySQL 现有 final_text 字段是 TEXT，预留空间给末尾的 UTF-8 字符。
_MAX_ANSWER_BYTES = 60_000
_RESERVED_ROOTS = {"uploads", "team-projects", "tool_results", "tool-outputs", "documents"}
_REASONING_PREFIX = re.compile(r"^\s*<think>.*?</think>\s*", re.S | re.I)
_CONTINUE_FILES = re.compile(
    r"^\s*(?:请\s*)?(?:继续(?:完成|处理)?(?:(?:刚才|上次|当前)(?:的)?任务)?|接着做|继续做)[吧。！!\s]*$",
)
_EXTERNAL_CLASSES = {CLASS_EXTERNAL_IDEMPOTENT, CLASS_NON_IDEMPOTENT}
_UNRESOLVED_OR_EXECUTED = {"succeeded", "executing", "unknown", "needs_reconciliation"}


def _workspace_path(value: Any) -> str | None:
    if not isinstance(value, str) or not value.startswith("/workspace/"):
        return None
    relative = value.removeprefix("/workspace/")
    parts = relative.split("/")
    if (len(relative) > 400 or any(not part or part in {".", ".."} or part.startswith(".") for part in parts)
            or parts[0].casefold() in _RESERVED_ROOTS or re.search(r"[\\:\x00-\x1f]", relative)):
        return None
    return relative


async def resolve_completion_resume(
    db: AsyncSession, user_id: int, session_id: int, message: str, history: list[dict],
) -> dict | None:
    """明确续做最近一次文件任务，沿用原要求与本会话的部分交付。"""
    if not (settings.AGENT_VERIFICATION_ENABLED and history and _CONTINUE_FILES.fullmatch(message)):
        return None
    latest = history[-1]
    if not isinstance(latest, dict) or latest.get("role") != "assistant" or not latest.get("source_run_id"):
        return None
    previous = (await db.execute(select(AgentRun.id, AgentRun.input_payload, AgentRun.summary).where(
        AgentRun.id == latest["source_run_id"], AgentRun.user_id == user_id, AgentRun.session_id == session_id,
        AgentRun.status.in_(("failed", "limit_exceeded")),
    ))).one_or_none()
    if previous is None:
        return None
    verification = (previous.summary or {}).get("verification") or {}
    if verification.get("version") != 2 or verification.get("stop_reason") == "external_operation":
        return None
    payload = previous.input_payload or {}
    route = payload.get("execution_context") or {}
    if route.get("team_mode") or route.get("task_type") not in (None, "general_chat"):
        return None
    inherited = route.get("completion_resume") or {}
    request = inherited.get("request") or next(
        (item.get("content", "") for item in reversed(payload.get("messages") or [])
         if isinstance(item, dict) and item.get("role") == "user"), "",
    )
    contract = build_task_contract(request)
    if ("artifact_produced" not in contract.completion_checks
            or {"side_effect_evidence", "db_evidence"} & set(contract.completion_checks)):
        return None
    artifacts = {item["path"]: item["sha256"] for item in verification.get("artifacts") or []
                 if isinstance(item, dict) and isinstance(item.get("path"), str)
                 and _workspace_path(f"/workspace/{item['path']}")
                 and re.fullmatch(r"[a-f0-9]{64}", item.get("sha256") or "")}
    if not artifacts:
        return None
    operations = (await db.scalars(select(AgentToolExecution.tool_name).where(
        AgentToolExecution.run_id == previous.id,
        AgentToolExecution.status.in_(_UNRESOLVED_OR_EXECUTED),
    ))).all()
    if any(get_side_effect_contract(name).side_effect_class in _EXTERNAL_CLASSES for name in operations):
        return None
    return {"run_id": previous.id, "request": request, "artifacts": artifacts}


def _artifact_root(ctx: RuntimeContext) -> Path | None:
    if ctx.scope.user_id is None:
        return None
    path = Path(workspace_root(ctx.scope.user_id, ctx.agent_id, ctx.session_id))
    expected = Path(settings.AGENT_FILES_ROOT).resolve() / str(ctx.scope.user_id) / str(ctx.agent_id) / str(ctx.session_id or "ephemeral")
    return expected if path.resolve() == expected else None


def _read_artifacts(ctx: RuntimeContext, paths: set[str]) -> dict[str, tuple[int, str, Any]]:
    if (root := _artifact_root(ctx)) is None:
        return {}
    valid = {}
    max_size = int(settings.AGENT_MAX_FILE_SIZE_MB * 1024 * 1024)
    for relative in sorted(paths)[:100]:
        target = root / relative
        try:
            if target.resolve() != target or not target.is_file():
                continue
            with target.open("rb") as stream:
                raw = stream.read(max_size + 1)
            if not 0 < len(raw) <= max_size:
                continue
            suffix = target.suffix.casefold()
            content = None
            if suffix == ".json":
                parsed = json.loads(raw.decode("utf-8-sig"))
                if ctx.task_contract and ctx.task_contract.table_facts:
                    content = parsed
            elif suffix in {".zip", ".xlsx", ".docx", ".pptx"}:
                with ZipFile(io.BytesIO(raw)) as archive:
                    if not archive.namelist():
                        continue
            elif suffix in {".txt", ".md", ".csv", ".tsv", ".py", ".js", ".html", ".css", ".sql"}:
                text = raw.decode("utf-8-sig")
                if not text.strip():
                    continue
                if suffix in {".txt", ".md"} and ctx.task_contract and ctx.task_contract.table_facts:
                    content = text
        except (OSError, ValueError, RuntimeError, BadZipFile):
            continue
        valid[relative] = (len(raw), hashlib.sha256(raw).hexdigest(), content)
    return valid


def freeze_artifacts(ctx: RuntimeContext, artifacts: list[dict]) -> dict[str, str]:
    """保存验收时的字节，后续会话内的编辑不会改变已发布的下载记录。"""
    root = _artifact_root(ctx)
    if not root or not re.fullmatch(r"[a-zA-Z0-9_-]{1,80}", ctx.run_id):
        return {}
    published: dict[str, str] = {}
    for artifact in artifacts:
        path, digest, size = artifact["path"], artifact.get("sha256", ""), artifact["size"]
        if not _workspace_path(f"/workspace/{path}") or not re.fullmatch(r"[a-f0-9]{64}", digest):
            continue
        source = root / path
        destination = root / RUN_ARTIFACT_FOLDER / ctx.run_id / digest / path
        try:
            if source.resolve() != source or destination.resolve() != destination:
                continue
            if destination.is_file():
                with destination.open("rb") as stream:
                    if hashlib.file_digest(stream, "sha256").hexdigest() == digest:
                        published[path] = destination.relative_to(root).as_posix()
                        continue
            if not source.is_file():
                continue
            with source.open("rb") as stream:
                raw = stream.read(size + 1)
            if len(raw) != size or hashlib.sha256(raw).hexdigest() != digest:
                continue
            destination.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=destination.parent, delete=False) as stream:
                temporary = Path(stream.name)
                try:
                    stream.write(raw)
                    stream.flush()
                    os.fsync(stream.fileno())
                    stream.close()
                    os.replace(temporary, destination)
                finally:
                    temporary.unlink(missing_ok=True)
            published[path] = destination.relative_to(root).as_posix()
        except (OSError, ValueError, RuntimeError):
            continue
    return published


async def _collect_evidence(ctx: RuntimeContext) -> tuple[list[ToolEvidence], list[dict[str, Any]], bool]:
    ledger, internal = AgentToolExecution, AgentRunEvent
    async with AsyncSessionLocal() as db:
        rows = (await db.execute(select(ledger.tool_name, ledger.status, ledger.external_operation_id, ledger.result_payload)
                                 .where(ledger.run_id == ctx.run_id))).all()
        events = (await db.execute(select(internal.sequence, internal.event_type, internal.payload)
                                   .where(internal.run_id == ctx.run_id,
                                          internal.event_type.in_((TOOL_STARTED, TOOL_COMPLETED, TOOL_FAILED))))).all()
    evidence = [ToolEvidence(row.tool_name, row.status, row.external_operation_id, str(row.result_payload or "")) for row in rows]
    # 已获得回执或仍需核对的外部操作均阻止自动返工，避免重新推理生成另一笔操作。
    external_handled = any(row.status in _UNRESOLVED_OR_EXECUTED
                           and get_side_effect_contract(row.tool_name).side_effect_class in _EXTERNAL_CLASSES for row in rows)
    writes: dict[str, str] = {}
    finished_paths: set[str] = set()
    for _, event_type, payload in sorted(events, key=lambda row: row.sequence):
        event = payload or {}
        if not event.get("internal") or event.get("tool") not in _FILE_TOOLS:
            continue
        call_id = str(event.get("tool_call_id") or "")
        if event_type == TOOL_STARTED:
            args = event.get("input")
            path = _workspace_path(args.get("file_path") or args.get("path")) if isinstance(args, dict) else None
            if path and call_id:
                writes[call_id] = path
        elif event_type == TOOL_COMPLETED and call_id in writes:
            finished_paths.add(writes[call_id])
    paths = finished_paths | ctx.carried_artifacts.keys()
    available = await asyncio.to_thread(_read_artifacts, ctx, paths) if paths else {}
    available = {path: result for path, result in available.items()
                 if path in finished_paths or result[1] == ctx.carried_artifacts.get(path)}
    for relative, (_, _, content) in available.items():
        evidence.append(ToolEvidence("write_file", path=relative, artifact_content=content))
    return evidence, [{"path": path, "name": PurePosixPath(path).name, "size": size, "sha256": digest}
                      for path, (size, digest, _) in available.items()], external_handled


def bounded_answer(text: str) -> tuple[str, bool]:
    raw = text.encode()
    if len(raw) <= _MAX_ANSWER_BYTES:
        return text, False
    return raw[:_MAX_ANSWER_BYTES].decode(errors="ignore"), True


async def interrupted_report(ctx: RuntimeContext, draft: str, reason: str) -> dict:
    """中断后的本地核对复用现有证据，不调用模型或重新执行工具。"""
    evidence, artifacts, external = await _collect_evidence(ctx)
    result = verify_task(ctx.task_contract, draft, evidence, execution_checks=[
        CheckResult("execution_interrupted", False, "任务中断，最终交付尚未完成"),
    ])
    review = ctx.completion_review or {}
    rounds = review.get("fix_rounds", 0) if review.get("run_id") == ctx.run_id else 0
    return {
        **result.to_event_data(), "version": 2, "artifacts": artifacts, "fix_rounds": rounds,
        "stop_reason": "external_operation" if external else reason,
    }


def completion_prompt(contract: TaskContract) -> str:
    if not contract.is_complex:
        return ""
    lines = ["复杂任务先分解关键交付和依赖，并用 write_todos 跟踪。执行并核对各项，提交前关闭已实际完成的项。"
             "保留实际不可完成的检查并说明原因。不要把计划、工具失败或没有写入的文件描述为已交付。"]
    if "artifact_produced" in contract.completion_checks:
        lines.append("把用户要求的实际文件写到 /workspace/，内容完整再交付。路径及格式要求 "
                     + "、".join([*contract.artifact_paths, *(f".{suffix}" for suffix in contract.artifact_formats)]))
    if contract.source_count:
        lines.append(f"在最终正文引用实际获取或用户提供的来源，至少 {contract.source_count} 个。")
    if contract.minimum_chars:
        lines.append(f"最终正文至少 {contract.minimum_chars} 个非空白字符。")
    if "json_answer" in contract.completion_checks:
        lines.append("最终正文仅输出可解析 JSON。")
    if facts := contract.table_facts:
        lines.append(f"原始表格已独立复算，共 {facts.raw_rows} 条记录，按本次去重和排除规则得到 {facts.valid_rows} 条有效记录。"
                     "文稿、表格和 JSON 应保持一致。" + ("用户指定的已复算字段 " + json.dumps(contract.expected_json, ensure_ascii=False)
                                                      if contract.expected_json else ""))
    return "\n".join(lines)


class CompletionMiddleware(AgentMiddleware[CompletionState]):
    """模型尝试结束时重新核对计划、工具回执、交付文件和最后一份答案。"""

    state_schema = CompletionState

    def __init__(self, ctx: RuntimeContext, contract: TaskContract):
        super().__init__()
        self.ctx = ctx
        self.contract = contract

    async def abefore_model(self, state, runtime):
        if reason := self.ctx.budget.exhausted_reason(self.ctx.steps + 1, self.ctx.tool_calls, self.ctx.model_calls + 1):
            raise BudgetExceededError(reason)

    @hook_config(can_jump_to=["model"])
    async def aafter_model(self, state, runtime):
        messages = state["messages"]
        if not messages or not isinstance(answer := messages[-1], AIMessage) or answer.tool_calls:
            return None
        previous = state.get("completion_review") or {}
        if previous.get("run_id") != self.ctx.run_id:
            previous = {}
        text = _REASONING_PREFIX.sub("", str(answer.text))
        finish_reason = answer.response_metadata.get("finish_reason") or answer.response_metadata.get("stop_reason")
        truncated = finish_reason in _LIMIT_FINISH_REASONS
        # 每次继续只拼接同一份因长度截断的答案。一般返工提交完整替换稿。
        prefix = previous.get("prefix", "")
        if prefix and not text.startswith(prefix):
            text = prefix + (("" if prefix.endswith("\n") or text.startswith("\n") else "\n") + text if text else "")
        text, too_large = bounded_answer(text)
        open_tasks = [str(todo.get("content") or "").strip()[:120] for todo in state.get("todos") or []
                      if todo.get("status") in {"pending", "in_progress"} and str(todo.get("content") or "").strip()]
        execution_checks = []
        if open_tasks:
            execution_checks.append(CheckResult("plan_finished", False, "计划中还有未完成项 " + "、".join(open_tasks[:6])))
        if truncated or too_large:
            execution_checks.append(CheckResult("answer_complete", False, "输出达到长度上限，需要补全交付"))
        if answer.invalid_tool_calls:
            execution_checks.append(CheckResult("tool_request_valid", False, "模型未形成有效的工具参数"))
        if not (self.contract.is_complex or execution_checks or previous):
            # 普通问答保留既有返回语义，也允许调用方明确要求不输出文本。
            return None
        evidence, artifacts, external = await _collect_evidence(self.ctx) if self.contract.is_complex or execution_checks else ([], [], False)
        result = verify_task(self.contract, text, evidence, execution_checks=execution_checks)
        rounds = previous.get("fix_rounds", 0)
        data = result.to_event_data()
        data.update(fix_rounds=rounds, artifacts=artifacts)
        failures = [item.id for item in result.failed_checks]
        signature = hashlib.sha256(json.dumps([text, failures, artifacts, len(evidence)], ensure_ascii=False).encode()).hexdigest()
        budget = self.ctx.budget.exhausted_reason(self.ctx.steps + 1, self.ctx.tool_calls, self.ctx.model_calls + 1)
        stop_reason = ("external_operation" if external else "budget" if budget else
                       "length" if too_large else "no_progress" if signature == previous.get("signature") else
                       "rounds" if rounds >= settings.AGENT_VERIFICATION_MAX_ROUNDS else "")
        review = {"run_id": self.ctx.run_id, "answer": text or previous.get("answer", ""), "signature": signature,
                  "report": data, "fix_rounds": rounds, "stop_reason": stop_reason if failures else ""}
        if not failures or stop_reason:
            return {"completion_review": review}

        # JSON 回复需要由模型重新输出完整对象。其他文稿能够从截断处继续。
        continuing = bool(text and truncated and failures == ["answer_complete"] and "json_answer" not in self.contract.completion_checks)
        if continuing:
            feedback = "上一份正文在生成长度上限处中断。紧接其末尾完成余下内容，省略已完成部分。"
            review["prefix"] = text
        else:
            feedback = result.feedback_text() + ("\n沿用原始任务和已经确认的证据，处理缺口后交付完整结果。"
                                                 "若已有文件需要修正，先读取其内容，再按原路径编辑。")
        review.update(fix_rounds=rounds + 1, continuing=True)
        return {"completion_review": review, "jump_to": "model", "messages": [
            HumanMessage(content=feedback, id=f"completion-{self.ctx.run_id}-{rounds + 1}"),
        ]}
