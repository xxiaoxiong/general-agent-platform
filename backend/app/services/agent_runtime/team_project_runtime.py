"""把项目协作图限制在所在 Run 的工程工作区。"""

import asyncio
import logging

from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.errors import GraphBubbleUp
from sqlalchemy import select

from app.core.config import settings
from app.core.database import AsyncSessionLocal
from app.models.agent import AgentRun

from .persistence import make_run_config
from .project_verifier_protocol import openapi_source, source_digest
from .repositories.errors import PersistenceError
from .team_project_coverage import _REVIEW_SYSTEM, coverage_input, coverage_report, merge_coverage_report
from .team_project_files import ProjectWorkspace, ProjectWorkspaceError
from .team_project_graph import (
    ProjectFileTruncated, ProjectPlanTruncated, TeamProjectContext, _model_text, browser_plan, build_project_graph, drive_project_graph,
)
from .team_project_verifier import merge_runtime_report, verify_runtime
from .team_runtime import TeamRun, team_thread_id
from .tools.policy import BudgetExceededError

logger = logging.getLogger(__name__)


async def _fenced_io(team: TeamRun, operation, *args, **kwargs):
    """租约行锁覆盖整个原子写入。取消后先等已开始的有限磁盘操作结束。"""
    async with AsyncSessionLocal() as db:
        await team._check_fence(db)
        work = asyncio.create_task(asyncio.to_thread(operation, *args, **kwargs))
        try:
            return await asyncio.shield(work)
        except asyncio.CancelledError:
            while not work.done():
                try:
                    await asyncio.shield(work)
                except asyncio.CancelledError:
                    continue
                except Exception:
                    break
            if not work.cancelled():
                work.exception()
            raise


async def drive_project(team: TeamRun, checkpointer, messages: list[dict], route: dict) -> dict:
    run = team.run
    workspace = ProjectWorkspace(run.user_id, run.agent_id, run.session_id, run.run_id)
    previous_runtime: dict | None = None
    openapi_attempt: str | None = None
    runtime_enabled = bool(settings.AGENT_PROJECT_VERIFIER_DIR.strip())

    async def write_file(path: str, content: str) -> None:
        # 租约校验与源文件提交一起持锁，接管者不会并发覆盖当前操作。
        await _fenced_io(team, workspace.write_files, {path: content}, allowed=(path,))

    async def read_file(path: str) -> str:
        return await asyncio.to_thread(workspace.read_file, path)

    async def review(plan: dict) -> dict:
        nonlocal openapi_attempt, previous_runtime
        expected = [path for task in plan["tasks"] for path in task["files"]]
        result = await asyncio.to_thread(workspace.verify, expected=expected, scope=team.project_scope)
        if not runtime_enabled or (not result["passed"] and team.project_scope != "fullstack"):
            return result
        files = await asyncio.to_thread(workspace.inspect)
        if not workspace.matches_snapshot(files, result["source_digest"]):
            raise ProjectWorkspaceError("项目源码在提交运行验收前发生变化")
        if not result["passed"]:
            missing_contract = next((path for path in expected if path.casefold() == "openapi.json" and path not in files), None)
            backend_paths = [path for path in expected if path.casefold().startswith(("backend/", "server/", "api/"))]
            readme = next((path for path in files if path.casefold() == "readme.md"), None)
            if missing_contract and readme and backend_paths and all(path in files for path in backend_paths):
                inputs = {path: files[path] for path in [readme, *backend_paths]}
                if "requirements.txt" in files:
                    inputs["requirements.txt"] = files["requirements.txt"]
                backend_digest = source_digest(inputs)
                if openapi_attempt == backend_digest:
                    return result
                openapi_attempt = backend_digest
                await team.emit("project.progress", {"phase": "checking", "title": plan["name"],
                                                     "detail": "从隔离运行的后端读取 OpenAPI 契约"})
                receipt = await verify_runtime(files, team.project_scope, capture_openapi=True)
                if receipt and (contract := openapi_source(receipt.get("openapi"))):
                    return {**result, "verified_openapi": {"path": missing_contract, "content": contract}}
            return result
        if previous_runtime is None or previous_runtime["source_digest"] != result["source_digest"]:
            await team.emit("project.progress", {"phase": "checking", "title": plan["name"],
                                                 "detail": "在隔离环境启动项目并执行功能验收"})
            previous_runtime = await verify_runtime(files, team.project_scope)
        return merge_runtime_report(result, previous_runtime, files) if previous_runtime else result

    async def publish(plan: dict, verification: dict) -> dict:
        return await _fenced_io(team, workspace.publish, plan["name"], verification)

    async def invoke(stage: str, system: str, user: str) -> str:
        # 源码经过 checkpoint 保存到工作区。对话事件只公开路径和验收结果。
        reply = await team._model(stage, [SystemMessage(content=system), HumanMessage(content=user)])
        if reply.response_metadata.get("finish_reason") in {
            "length", "max_tokens", "max_output_tokens",
        }:
            if stage == "project_planning":
                raise ProjectPlanTruncated
            raise ProjectFileTruncated
        return reply.text

    async def audit(objective: str, plan: dict, verification: dict, previous: dict | None) -> dict:
        files = await asyncio.to_thread(workspace.inspect)
        digest = verification["source_digest"]
        if not workspace.matches_snapshot(files, digest):
            raise ProjectWorkspaceError("需求复核前工程源码发生变化")
        if previous and previous.get("source_digest") == digest:
            return merge_coverage_report(verification, previous, files)
        criteria, prompt = coverage_input(objective, plan, files, verification["checks"])
        await team.emit("project.progress", {"phase": "checking", "title": plan["name"],
                                             "detail": "对照原始要求复核工程实现与实际验收证据"})
        raw = ""
        if team.remaining_model_calls():
            try:
                raw = _model_text(await invoke("project_coverage", _REVIEW_SYSTEM, prompt))
            except (GraphBubbleUp, PersistenceError):
                raise
            except BudgetExceededError:
                pass
            except Exception as exc:
                logger.info("工程需求复核未返回可用证据 error=%s", type(exc).__name__)
        review = coverage_report(raw, criteria, files, verification["checks"], digest)
        return merge_coverage_report(verification, review, files)

    context = TeamProjectContext(
        invoke=invoke, emit=team.emit, write_file=write_file, read_file=read_file,
        review=review, publish=publish, browser_checks=runtime_enabled, audit=audit if runtime_enabled else None,
    )
    graph = build_project_graph(checkpointer, context)
    config = make_run_config(team_thread_id(run.run_id), run.run_id, team.runtime.scope.user_id)
    checkpoint = await graph.aget_state(config)
    if checkpoint.values or checkpoint.next:
        return await drive_project_graph(graph, config, {})
    if base_id := route.get("project_base_run_id"):
        try:
            initial = await _restore_project(team, graph, workspace, base_id)
            initial["plan"] = browser_plan(initial["plan"], runtime_enabled)
        except (OSError, ValueError) as exc:
            failure = str(exc) if isinstance(exc, ProjectWorkspaceError) else "项目草稿或原计划已不可读取。"
            board = {"phase": "failed", "lead": "工程负责人", "tasks": [], "summary": failure}
            await team.emit("team.updated", board)
            return {"failure": failure, "team": board}
    else:
        initial = await team._initial(messages, route, synthesize=False)
    initial["project_scope"] = team.project_scope
    return await drive_project_graph(graph, config, initial)


async def _restore_project(team: TeamRun, graph, workspace: ProjectWorkspace, base_id: str) -> dict:
    run = team.run
    async with AsyncSessionLocal() as db:
        base = await db.scalar(select(AgentRun.id).where(
            AgentRun.id == base_id, AgentRun.user_id == run.user_id,
            AgentRun.agent_id == run.agent_id, AgentRun.session_id == run.session_id,
            AgentRun.status == "failed",
        ))
    if base is None:
        raise ProjectWorkspaceError("无法读取当前会话的失败项目草稿。")
    previous = await graph.aget_state(make_run_config(team_thread_id(base), base, run.user_id))
    state = previous.values
    plan = state.get("plan") or {}
    if not state.get("objective") or plan.get("scope") != team.project_scope or state.get("project", {}).get("state") != "failed":
        raise ProjectWorkspaceError("原项目缺少可继续修复的计划和草稿。")
    source = ProjectWorkspace(run.user_id, run.agent_id, run.session_id, base)
    files = await asyncio.to_thread(source.inspect)
    allowed = [path for task in plan["tasks"] for path in task["files"]]
    recorded = state.get("verification", {}).get("source_digest")
    if not files or not set(files) <= set(allowed) or not source.matches_snapshot(files, recorded):
        raise ProjectWorkspaceError("项目草稿与原交付记录不一致。请从原需求重新发起。")
    await _fenced_io(team, workspace.write_files, files, allowed=allowed)
    return {
        "objective": state["objective"], "reference_context": state.get("reference_context", ""),
        "assistant_prompt": team.runtime.agent_config.get("system_prompt", ""),
        "plan": plan, "restored_paths": list(files),
    }
