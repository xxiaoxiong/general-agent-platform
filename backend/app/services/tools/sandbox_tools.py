"""Sandbox Agent 工具集（任务书 §9.1）。

- 命令：execute_command / get_command_status / get_command_output / cancel_command
- 服务：start_service / list_services / get_service_status / get_service_logs /
        stop_service / restart_service / get_preview_url
- 文件工具继续使用 DeepAgents 原生 read/write/edit/ls/glob/grep，
  由 SandboxFilesBackend 路由到同一个 /workspace（见 sandbox_backend.py）。

权限：
- 所有工具从已认证 Run Context 取 project_id，不接受模型指定其他 Workspace；
- 写操作（命令/服务启停/文件写入）先获取 Workspace 写租约；
- execute_command / start_service / restart_service 进入 HITL 审批。
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from sqlalchemy import select

from app.core.config import settings
from app.core.database import AsyncSessionLocal
from app.models.sandbox import (
    Project, SandboxCommandOperation, SandboxInstance, Workspace, WorkspaceService,
)
from app.models.user import User
from app.services.sandbox import (
    agent_client, preview as preview_service, runtime, service_manager,
)
from app.services.sandbox.common import (
    SandboxAccessDenied, SandboxDisabledError, SandboxError,
)
from app.services.sandbox.write_lease import acquire_write_lease

from .result import ToolResult

logger = logging.getLogger("ai_plat.sandbox.tools")

SANDBOX_TOOL_NAMES = (
    "execute_command", "get_command_status", "get_command_output", "cancel_command",
    "start_service", "list_services", "get_service_status", "get_service_logs",
    "stop_service", "restart_service", "get_preview_url",
)

# 需要 HITL 审批的 Sandbox 工具（装配期加入 policy.hitl_tools）
SANDBOX_HITL_TOOLS = ("execute_command", "start_service", "restart_service")

TOOLS: List[Dict[str, Any]] = [
    {
        "name": "execute_command",
        "display_name": "🖥️ 执行命令",
        "category": "项目工作空间",
        "description": (
            "在项目 Docker Sandbox 中执行 shell 命令（工作目录 /workspace，与文件工具同一文件系统）。"
            "适合作业：安装依赖、运行测试/构建、脚本执行、查看目录。"
            "长时间命令会返回 running 状态与 operation_id，用 get_command_status/get_command_output 查询，cancel_command 取消。"
            "命令在非 root、只读 rootfs、受限 CPU/内存的隔离容器中执行。"
        ),
        "parameters": {
            "command": {"type": "string", "description": "要执行的 shell 命令"},
            "working_directory": {"type": "string", "description": "工作目录（默认 /workspace）", "default": "/workspace"},
            "timeout_seconds": {"type": "integer", "description": "命令最长运行秒数（默认 600）", "default": 600},
            "wait_seconds": {"type": "integer", "description": "同步等待秒数，超时后转为异步返回（默认 30）", "default": 30},
        },
        "required": ["command"],
    },
    {
        "name": "get_command_status",
        "display_name": "📊 命令状态",
        "category": "项目工作空间",
        "description": "查询异步命令的当前状态与退出码（running / succeeded / failed / timeout / cancelled）。",
        "parameters": {
            "operation_id": {"type": "string", "description": "execute_command 返回的 operation_id"},
        },
        "required": ["operation_id"],
    },
    {
        "name": "get_command_output",
        "display_name": "📜 命令输出",
        "category": "项目工作空间",
        "description": "读取异步命令的 stdout/stderr（默认各取尾部 200 行）。",
        "parameters": {
            "operation_id": {"type": "string", "description": "execute_command 返回的 operation_id"},
            "tail_lines": {"type": "integer", "description": "输出尾部行数（默认 200）", "default": 200},
        },
        "required": ["operation_id"],
    },
    {
        "name": "cancel_command",
        "display_name": "⏹️ 取消命令",
        "category": "项目工作空间",
        "description": "取消运行中的命令并杀死整个进程组（避免孤儿进程）。",
        "parameters": {
            "operation_id": {"type": "string", "description": "execute_command 返回的 operation_id"},
        },
        "required": ["operation_id"],
    },
    {
        "name": "start_service",
        "display_name": "🚀 启动服务",
        "category": "项目工作空间",
        "description": (
            "在 Sandbox 中启动长期运行的服务（如 Vite / FastAPI）。返回服务初始状态；"
            "健康检查通过后变为 healthy。端口在同一项目内必须唯一。"
            "启动后可用 get_preview_url 获取浏览器预览地址。"
        ),
        "parameters": {
            "name": {"type": "string", "description": "服务名（项目内唯一，如 frontend）"},
            "command": {"type": "string", "description": "服务启动命令（如 npm run dev -- --host 0.0.0.0）"},
            "port": {"type": "integer", "description": "服务监听端口（如 5173）"},
            "working_directory": {"type": "string", "description": "工作目录（默认 /workspace）", "default": "/workspace"},
            "health_check_path": {"type": "string", "description": "HTTP 健康检查路径（默认 /）", "default": "/"},
            "health_check_timeout": {"type": "integer", "description": "健康检查超时秒数（默认 60）", "default": 60},
            "auto_restart": {"type": "boolean", "description": "崩溃后自动重启（默认 true）", "default": True},
            "auto_start_on_sandbox_start": {"type": "boolean", "description": "Sandbox 启动时自动拉起（默认 true）", "default": True},
        },
        "required": ["name", "command", "port"],
    },
    {
        "name": "list_services",
        "display_name": "📋 服务列表",
        "category": "项目工作空间",
        "description": "列出项目内所有服务及其状态、端口、最近启停时间与退出码。",
        "parameters": {},
        "required": [],
    },
    {
        "name": "get_service_status",
        "display_name": "🩺 服务状态",
        "category": "项目工作空间",
        "description": "查询单个服务状态并触发一次实时同步（健康检查结果）。",
        "parameters": {
            "name": {"type": "string", "description": "服务名"},
        },
        "required": ["name"],
    },
    {
        "name": "get_service_logs",
        "display_name": "📜 服务日志",
        "category": "项目工作空间",
        "description": "读取服务 stdout/stderr 日志（默认尾部 200 行；已脱敏）。",
        "parameters": {
            "name": {"type": "string", "description": "服务名"},
            "tail": {"type": "integer", "description": "尾部行数（默认 200）", "default": 200},
        },
        "required": ["name"],
    },
    {
        "name": "stop_service",
        "display_name": "⏹️ 停止服务",
        "category": "项目工作空间",
        "description": "停止服务并清理整个进程组；Desired State 同步为 stopped（Sandbox 重启后不会自动拉起）。",
        "parameters": {
            "name": {"type": "string", "description": "服务名"},
        },
        "required": ["name"],
    },
    {
        "name": "restart_service",
        "display_name": "🔄 重启服务",
        "category": "项目工作空间",
        "description": "重启服务（清理旧进程组后重新启动）。",
        "parameters": {
            "name": {"type": "string", "description": "服务名"},
        },
        "required": ["name"],
    },
    {
        "name": "get_preview_url",
        "display_name": "🌐 获取预览地址",
        "category": "项目工作空间",
        "description": "为服务生成受授权的浏览器预览地址（绑定 Sandbox Generation 与端口，可过期/撤销）。",
        "parameters": {
            "name": {"type": "string", "description": "服务名"},
            "ttl_seconds": {"type": "integer", "description": "预览授权有效期秒数（默认 3600）", "default": 3600},
        },
        "required": ["name"],
    },
]


@dataclass
class SandboxRef:
    sandbox_id: int
    generation: int
    workspace_id: int
    project_id: int


async def resolve_sandbox(
    project_id: Optional[int], *, user_id: Optional[int] = None,
    min_role: str = "operator", wait_seconds: float = 25.0,
) -> SandboxRef:
    """取（必要时启动）项目 Sandbox；启动中最多等待 wait_seconds。

    ``user_id`` 非空时每次调用都重新校验 Project ACL（成员移除后立即失效），
    不信任任何缓存或模型参数。
    """
    if not project_id:
        raise SandboxError(
            "当前会话未绑定项目工作空间（旧会话使用平台文件区），"
            "请新建对话或选择项目后再使用 Sandbox 工具。",
            code="sandbox_project_missing",
        )
    async with AsyncSessionLocal() as db:
        project = (await db.execute(
            select(Project).where(Project.id == project_id, Project.deleted_at.is_(None))
        )).scalar_one_or_none()
        if project is None:
            raise SandboxError("项目不存在或已删除", code="sandbox_project_missing")
        if user_id is not None:
            from app.services.sandbox import acl
            from app.services.sandbox.common import sandbox_enabled_for

            if not sandbox_enabled_for(int(user_id), project.id):
                raise SandboxDisabledError("Workspace Sandbox 未对当前用户/项目开放")
            user = await db.get(User, int(user_id))
            if user is None:
                raise SandboxAccessDenied("用户不存在")
            role = await acl.project_role(db, project, user)
            if not acl.role_satisfies(role, min_role):
                raise SandboxAccessDenied(
                    f"当前项目角色 {role or '无'} 无 {min_role} 权限"
                )
        workspace = (await db.execute(
            select(Workspace).where(
                Workspace.project_id == project.id, Workspace.deleted_at.is_(None))
        )).scalar_one_or_none()
        if workspace is None:
            workspace = await runtime.ensure_workspace(
                db, project.id, project.owner_id, project.tenant_id)
        sandbox = await runtime.active_sandbox(db, workspace.id)
        if sandbox is None or sandbox.desired_state == "deleted" or sandbox.observed_state in (
            "stopped", "failed",
        ):
            if sandbox is None or sandbox.desired_state == "deleted":
                sandbox = await runtime.provision_workspace(db, workspace)
            else:
                await runtime.request_lifecycle(db, workspace, sandbox, "start")
            await db.commit()
        await runtime.touch_workspace(db, workspace.id)
        await db.commit()
        ref = SandboxRef(sandbox.id, sandbox.generation, workspace.id, project.id)
    deadline = asyncio.get_event_loop().time() + max(0.0, wait_seconds)
    while True:
        async with AsyncSessionLocal() as db:
            current = (await db.execute(
                select(SandboxInstance).where(SandboxInstance.id == ref.sandbox_id)
            )).scalar_one_or_none()
            if current is not None and current.observed_state == "ready":
                return SandboxRef(current.id, current.generation, current.workspace_id, project_id)
            if current is not None and current.observed_state == "failed":
                raise SandboxError(
                    f"Sandbox 启动失败：{current.failure_message or current.failure_code or '未知原因'}",
                    code="sandbox_start_failed",
                )
        if asyncio.get_event_loop().time() >= deadline:
            raise SandboxError(
                "Sandbox 正在启动，请稍后重试（可先检查服务列表或环境状态）",
                code="sandbox_starting",
            )
        await asyncio.sleep(1.5)


async def ensure_write_lease(ctx: Any, ref: SandboxRef) -> Optional[str]:
    """写租约：同一 Workspace 只允许一个写 Run；失败返回错误文本。"""
    owner = f"run:{ctx.run_id}"
    try:
        async with AsyncSessionLocal() as db:
            token = await acquire_write_lease(
                db, ref.workspace_id, owner, user_id=ctx.scope.user_id,
            )
            await db.commit()
    except Exception as exc:  # noqa: BLE001
        logger.warning("获取 Workspace 写租约失败: %s", exc)
        return f"获取项目写租约失败（数据库异常）：{exc}"
    if token is None:
        return (
            "当前项目工作空间有另一个任务正在写入（写租约被占用），"
            "为避免文件互相覆盖，本次写入被拒绝。请等待该任务完成或稍后重试。"
        )
    return None


def _max_chars() -> int:
    return int(settings.SANDBOX_COMMAND_MAX_OUTPUT_BYTES or 200_000)


def _trim(text: str, limit: int = 12_000) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n...[输出截断，共 {len(text)} 字符]"


def _format_command(data: dict, command: str) -> str:
    status = data.get("status") or "unknown"
    exit_code = data.get("exit_code")
    stdout = str(data.get("stdout") or "")
    stderr = str(data.get("stderr") or "")
    parts = [
        f"命令: {command}",
        f"状态: {status}",
        f"退出码: {exit_code if exit_code is not None else '-'}",
    ]
    if data.get("truncated"):
        parts.append("注意: 输出超过上限，已截断")
    parts.append("stdout:\n" + (_trim(stdout) or "(空)"))
    parts.append("stderr:\n" + (_trim(stderr) or "(空)"))
    if status == "running":
        parts.append(
            f"命令仍在运行，operation_id={data.get('operation_id')}；"
            "用 get_command_status 查询、get_command_output 读取输出、cancel_command 取消。"
        )
    return "\n".join(parts)


async def _persist_operation(
    ref: SandboxRef, ctx: Any, data: dict, command: str, cwd: str, timeout: int,
) -> None:
    op_id = str(data.get("operation_id") or "")
    if not op_id:
        return
    try:
        async with AsyncSessionLocal() as db:
            row = (await db.execute(
                select(SandboxCommandOperation).where(
                    SandboxCommandOperation.id == op_id)
            )).scalar_one_or_none()
            if row is None:
                from datetime import datetime

                row = SandboxCommandOperation(
                    id=op_id,
                    workspace_id=ref.workspace_id,
                    sandbox_id=ref.sandbox_id,
                    sandbox_generation=ref.generation,
                    run_id=ctx.run_id,
                    user_id=ctx.scope.user_id,
                    command=command[:8000],
                    working_directory=cwd,
                    status=str(data.get("status") or "running"),
                    exit_code=data.get("exit_code"),
                    stdout_text=_trim(str(data.get("stdout") or ""), _max_chars()),
                    stderr_text=_trim(str(data.get("stderr") or ""), _max_chars()),
                    truncated=bool(data.get("truncated")),
                    timeout_seconds=timeout,
                    started_at=datetime.utcnow(),
                    finished_at=(datetime.utcnow()
                                 if data.get("status") not in ("running",) else None),
                )
                db.add(row)
            else:
                row.status = str(data.get("status") or row.status)
                row.exit_code = data.get("exit_code")
                if data.get("stdout") is not None:
                    row.stdout_text = _trim(str(data.get("stdout") or ""), _max_chars())
                if data.get("stderr") is not None:
                    row.stderr_text = _trim(str(data.get("stderr") or ""), _max_chars())
                row.truncated = bool(data.get("truncated"))
                if row.status != "running" and row.finished_at is None:
                    from datetime import datetime

                    row.finished_at = datetime.utcnow()
            await db.commit()
    except Exception as exc:  # noqa: BLE001 审计落库失败不阻断命令结果交付
        logger.warning("命令操作落库失败: %s", exc)


async def _operation_ctx(operation_id: str, ctx: Any) -> SandboxRef:
    """按 operation_id 找所属 Sandbox（并校验 Run 归属）。"""
    async with AsyncSessionLocal() as db:
        row = (await db.execute(
            select(SandboxCommandOperation).where(
                SandboxCommandOperation.id == operation_id)
        )).scalar_one_or_none()
        if row is None:
            raise SandboxError("命令操作不存在或已过期", code="sandbox_operation_missing")
        if row.run_id and row.run_id != ctx.run_id:
            raise SandboxError("无权访问其他任务的命令操作", code="sandbox_forbidden")
        if row.sandbox_id is None:
            raise SandboxError("命令操作未绑定 Sandbox", code="sandbox_operation_missing")
        sandbox = (await db.execute(
            select(SandboxInstance).where(SandboxInstance.id == row.sandbox_id)
        )).scalar_one_or_none()
        if sandbox is None:
            raise SandboxError("命令所属 Sandbox 不存在", code="sandbox_not_found")
        workspace = await db.get(Workspace, sandbox.workspace_id)
        if workspace is not None and ctx.scope.user_id is not None:
            from app.services.sandbox import acl

            await acl.require_project_role(
                db, workspace.project_id, ctx.scope.user_id, "operator")
        return SandboxRef(sandbox.id, sandbox.generation, sandbox.workspace_id, 0)


async def _service_by_name(workspace_id: int, name: str) -> WorkspaceService:
    async with AsyncSessionLocal() as db:
        service = (await db.execute(
            select(WorkspaceService).where(
                WorkspaceService.workspace_id == workspace_id,
                WorkspaceService.name == name,
            )
        )).scalar_one_or_none()
        if service is None:
            raise SandboxError(f"服务 '{name}' 不存在", code="sandbox_service_missing")
        return service


async def _resolve(ctx: Any, min_role: str = "operator") -> SandboxRef:
    """工具入口统一 ACL 复核：project_id 来自 Run 上下文，user_id 来自认证身份。"""
    return await resolve_sandbox(
        getattr(ctx, "project_id", None),
        user_id=getattr(ctx.scope, "user_id", None),
        min_role=min_role,
    )


# ── Handlers（ToolExecutor 以 (ctx, params) 调用）──────────────────────────

async def handle_execute_command(ctx: Any, params: dict) -> ToolResult:
    command = str(params.get("command") or "").strip()
    if not command:
        return ToolResult.failed("command 不能为空")
    ref = await _resolve(ctx)
    lease_error = await ensure_write_lease(ctx, ref)
    if lease_error:
        return ToolResult.failed(lease_error)
    cwd = str(params.get("working_directory") or "/workspace")
    timeout = max(1, min(int(params.get("timeout_seconds") or settings.SANDBOX_COMMAND_TIMEOUT),
                         int(settings.SANDBOX_COMMAND_TIMEOUT or 600)))
    wait_seconds = float(params.get("wait_seconds") or settings.SANDBOX_SYNC_COMMAND_WAIT)
    data = await agent_client.exec_command(
        ref.sandbox_id, command, working_directory=cwd,
        timeout_seconds=timeout, wait_seconds=min(60.0, wait_seconds),
    )
    await _persist_operation(ref, ctx, data, command, cwd, timeout)
    text = _format_command(data, command)
    return ToolResult.succeeded(
        text,
        external_operation_id=str(data.get("operation_id") or ""),
        metadata={"status": data.get("status"), "exit_code": data.get("exit_code")},
    )


async def handle_get_command_status(ctx: Any, params: dict) -> ToolResult:
    operation_id = str(params.get("operation_id") or "")
    ref = await _operation_ctx(operation_id, ctx)
    data = await agent_client.operation_status(ref.sandbox_id, operation_id)
    await _persist_operation(ref, ctx, data, "", "/workspace", 0)
    return ToolResult.succeeded(
        f"operation_id: {operation_id}\n状态: {data.get('status')}\n"
        f"退出码: {data.get('exit_code') if data.get('exit_code') is not None else '-'}\n"
        f"开始: {data.get('started_at')}\n结束: {data.get('finished_at') or '-'}",
        metadata={"status": data.get("status")},
    )


async def handle_get_command_output(ctx: Any, params: dict) -> ToolResult:
    operation_id = str(params.get("operation_id") or "")
    tail = max(1, min(2000, int(params.get("tail_lines") or 200)))
    ref = await _operation_ctx(operation_id, ctx)
    data = await agent_client.operation_status(ref.sandbox_id, operation_id)
    await _persist_operation(ref, ctx, data, "", "/workspace", 0)
    stdout = "\n".join(str(data.get("stdout") or "").splitlines()[-tail:])
    stderr = "\n".join(str(data.get("stderr") or "").splitlines()[-tail:])
    text = (
        f"状态: {data.get('status')}（退出码 {data.get('exit_code')}）\n"
        f"stdout:\n{_trim(stdout) or '(空)'}\n\nstderr:\n{_trim(stderr) or '(空)'}"
    )
    return ToolResult.succeeded(text, metadata={"status": data.get("status")})


async def handle_cancel_command(ctx: Any, params: dict) -> ToolResult:
    operation_id = str(params.get("operation_id") or "")
    ref = await _operation_ctx(operation_id, ctx)
    data = await agent_client.cancel_operation(ref.sandbox_id, operation_id)
    await _persist_operation(ref, ctx, data, "", "/workspace", 0)
    return ToolResult.succeeded(
        f"命令已取消（状态 {data.get('status')}，退出码 {data.get('exit_code')}）",
        metadata={"status": data.get("status")},
    )


async def handle_start_service(ctx: Any, params: dict) -> ToolResult:
    ref = await _resolve(ctx)
    lease_error = await ensure_write_lease(ctx, ref)
    if lease_error:
        return ToolResult.failed(lease_error)
    name = str(params.get("name") or "").strip()
    command = str(params.get("command") or "").strip()
    try:
        port = int(params.get("port") or 0)
    except (TypeError, ValueError):
        return ToolResult.failed("port 必须为整数")
    if not name or not command or not (1 <= port <= 65535):
        return ToolResult.failed("name / command / port 必须完整且合法")
    async with AsyncSessionLocal() as db:
        workspace = (await db.execute(
            select(Workspace).where(Workspace.id == ref.workspace_id)
        )).scalar_one()
        sandbox = (await db.execute(
            select(SandboxInstance).where(SandboxInstance.id == ref.sandbox_id)
        )).scalar_one()
        user = (await db.execute(
            select(User).where(User.id == ctx.scope.user_id)
        )).scalar_one()
        service = await service_manager.create_service(
            db, workspace, sandbox, user,
            name=name, command=command, port=port,
            working_directory=str(params.get("working_directory") or "/workspace"),
            health_check_type="http",
            health_check_path=str(params.get("health_check_path") or "/"),
            health_check_timeout=int(params.get("health_check_timeout") or 60),
            auto_restart=bool(params.get("auto_restart", True)),
            auto_start_on_sandbox_start=bool(params.get("auto_start_on_sandbox_start", True)),
        )
        await db.commit()
        payload = service_manager.service_to_dict(service)
    return ToolResult.succeeded(
        f"服务 {payload['name']} 已提交启动（端口 {payload['port']}，"
        f"当前状态 {payload['observed_state']}）。\n"
        f"健康检查通过后可用 get_service_status 确认，用 get_preview_url('{payload['name']}') 获取预览地址。",
        metadata={"service": payload},
    )


async def handle_list_services(ctx: Any, params: dict) -> ToolResult:
    ref = await _resolve(ctx, "viewer")
    async with AsyncSessionLocal() as db:
        workspace = (await db.execute(
            select(Workspace).where(Workspace.id == ref.workspace_id)
        )).scalar_one()
        sandbox = (await db.execute(
            select(SandboxInstance).where(SandboxInstance.id == ref.sandbox_id)
        )).scalar_one()
        await service_manager.sync_service_states(db, workspace, sandbox)
        services = await service_manager.list_services(db, workspace)
        await db.commit()
    if not services:
        return ToolResult.succeeded("当前项目还没有服务。可用 start_service 启动 Vite / FastAPI 等服务。")
    lines = ["服务列表："]
    for service in services:
        lines.append(
            f"- {service.name}: {service.observed_state}（desired={service.desired_state}，"
            f"端口 {service.port}，PID {service.process_id or '-'}，退出码 {service.exit_code if service.exit_code is not None else '-'}）"
        )
    return ToolResult.succeeded("\n".join(lines))


async def handle_get_service_status(ctx: Any, params: dict) -> ToolResult:
    ref = await _resolve(ctx, "viewer")
    name = str(params.get("name") or "")
    service = await _service_by_name(ref.workspace_id, name)
    async with AsyncSessionLocal() as db:
        workspace = (await db.execute(
            select(Workspace).where(Workspace.id == ref.workspace_id)
        )).scalar_one()
        sandbox = (await db.execute(
            select(SandboxInstance).where(SandboxInstance.id == ref.sandbox_id)
        )).scalar_one()
        await service_manager.sync_service_states(db, workspace, sandbox)
        await db.refresh(service)
        await db.commit()
    return ToolResult.succeeded(
        f"服务 {service.name}: {service.observed_state}\n"
        f"desired={service.desired_state}，端口 {service.port}，PID {service.process_id or '-'}，"
        f"最近启动 {service.last_started_at}，最近退出 {service.last_exited_at or '-'}，"
        f"退出码 {service.exit_code if service.exit_code is not None else '-'}"
    )


async def handle_get_service_logs(ctx: Any, params: dict) -> ToolResult:
    ref = await _resolve(ctx, "viewer")
    name = str(params.get("name") or "")
    tail = max(1, min(2000, int(params.get("tail") or 200)))
    service = await _service_by_name(ref.workspace_id, name)
    data = await service_manager.fetch_logs(service, tail=tail)
    content = str(data.get("content") or "")
    return ToolResult.succeeded(
        f"服务 {name} 日志（tail={tail}）:\n{_trim(content) or '(空)'}",
    )


async def handle_stop_service(ctx: Any, params: dict) -> ToolResult:
    ref = await _resolve(ctx)
    lease_error = await ensure_write_lease(ctx, ref)
    if lease_error:
        return ToolResult.failed(lease_error)
    name = str(params.get("name") or "")
    async with AsyncSessionLocal() as db:
        workspace = (await db.execute(
            select(Workspace).where(Workspace.id == ref.workspace_id)
        )).scalar_one()
        service = await _service_by_name(ref.workspace_id, name)
        user = (await db.execute(
            select(User).where(User.id == ctx.scope.user_id)
        )).scalar_one()
        await service_manager.stop_service(db, workspace, service, user)
        await db.commit()
        state = service_manager.service_to_dict(service)
    return ToolResult.succeeded(
        f"服务 {name} 已停止（Desired State=stopped，Sandbox 重启后不会自动拉起）",
        metadata={"service": state},
    )


async def handle_restart_service(ctx: Any, params: dict) -> ToolResult:
    ref = await _resolve(ctx)
    lease_error = await ensure_write_lease(ctx, ref)
    if lease_error:
        return ToolResult.failed(lease_error)
    name = str(params.get("name") or "")
    async with AsyncSessionLocal() as db:
        workspace = (await db.execute(
            select(Workspace).where(Workspace.id == ref.workspace_id)
        )).scalar_one()
        service = await _service_by_name(ref.workspace_id, name)
        user = (await db.execute(
            select(User).where(User.id == ctx.scope.user_id)
        )).scalar_one()
        await service_manager.restart_service(db, workspace, service, user)
        await db.commit()
        state = service_manager.service_to_dict(service)
    return ToolResult.succeeded(
        f"服务 {name} 已重启（当前状态 {state['observed_state']}）",
        metadata={"service": state},
    )


async def handle_get_preview_url(ctx: Any, params: dict) -> ToolResult:
    ref = await _resolve(ctx, "viewer")
    name = str(params.get("name") or "")
    ttl = max(0, min(86400, int(params.get("ttl_seconds") or settings.PREVIEW_TOKEN_TTL)))
    async with AsyncSessionLocal() as db:
        workspace = (await db.execute(
            select(Workspace).where(Workspace.id == ref.workspace_id)
        )).scalar_one()
        service = await _service_by_name(ref.workspace_id, name)
        user = (await db.execute(
            select(User).where(User.id == ctx.scope.user_id)
        )).scalar_one()
        grant, token = await preview_service.create_grant(
            db, workspace, service, user, ttl_seconds=(ttl or None),
        )
        access_token = None
        if settings.PREVIEW_REQUIRE_LOGIN:
            access_token = preview_service.create_preview_access_token(
                grant.id, user.id, ttl or settings.PREVIEW_TOKEN_TTL)
        url = preview_service.grant_to_dict(grant, token, access_token)
        await runtime.touch_workspace(db, workspace.id)
        await db.commit()
    return ToolResult.succeeded(
        f"预览地址（服务 {name}，端口 {grant.port}，有效期至 {grant.expires_at}）：\n"
        f"{url['url']}\n"
        f"备用地址（子域不可用时）：{url['fallback_url']}\n"
        "该地址已绑定当前 Sandbox Generation 与端口，可过期/撤销；请在右侧 Preview 区域查看页面。",
        metadata={"preview": {k: url[k] for k in ("id", "url", "fallback_url", "expires_at")
                              if k in url}},
    )


HANDLERS = {
    "execute_command": handle_execute_command,
    "get_command_status": handle_get_command_status,
    "get_command_output": handle_get_command_output,
    "cancel_command": handle_cancel_command,
    "start_service": handle_start_service,
    "list_services": handle_list_services,
    "get_service_status": handle_get_service_status,
    "get_service_logs": handle_get_service_logs,
    "stop_service": handle_stop_service,
    "restart_service": handle_restart_service,
    "get_preview_url": handle_get_preview_url,
}
