"""Workspace Sandbox 性能基准（真实 Docker）。

在已部署 Backend 容器内执行（复用数据库 / Runner / Preview Proxy）：
    docker cp backend/scripts/sandbox_benchmark.py ai_plat_backend:/tmp/sandbox_benchmark.py
    docker exec -e PYTHONPATH=/app -w /app ai_plat_backend \
      python /tmp/sandbox_benchmark.py

记录：
- Sandbox Provision -> Ready 耗时；
- 简单命令执行耗时；
- Service 启动 -> Healthy 耗时；
- Preview Grant 签发 + 首次 HTTP 访问耗时。

输出 JSON（含环境、样本、P50/P95）；不伪造结果，失败样本如实记录。
"""
from __future__ import annotations

import asyncio
import json
import os
import platform
import statistics
import time

import httpx
from sqlalchemy import select

from app.core.config import settings
from app.core.database import AsyncSessionLocal
from app.models.sandbox import SandboxInstance, Workspace, WorkspaceService
from app.models.user import User
from app.services.sandbox import (
    agent_client, preview, projects, runtime, service_manager,
)

ITERATIONS = int(os.environ.get("BENCH_ITERATIONS", "3"))


def _percentile(values: list[float], ratio: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round((len(ordered) - 1) * ratio))))
    return ordered[index]


async def _wait_sandbox(sandbox_id: int, timeout: float = 240.0) -> float:
    started = time.monotonic()
    while time.monotonic() - started < timeout:
        async with AsyncSessionLocal() as db:
            sandbox = await db.get(SandboxInstance, sandbox_id)
            if sandbox is not None and sandbox.observed_state == "ready":
                return time.monotonic() - started
            if sandbox is not None and sandbox.observed_state == "failed":
                raise RuntimeError(f"sandbox failed: {sandbox.failure_message}")
        await asyncio.sleep(0.5)
    raise TimeoutError("sandbox ready timeout")


async def _wait_service(workspace_id: int, sandbox_id: int, name: str,
                        timeout: float = 180.0) -> float:
    started = time.monotonic()
    while time.monotonic() - started < timeout:
        async with AsyncSessionLocal() as db:
            workspace = await db.get(Workspace, workspace_id)
            sandbox = await db.get(SandboxInstance, sandbox_id)
            await service_manager.sync_service_states(db, workspace, sandbox)
            await db.commit()
            service = (await db.execute(
                select(WorkspaceService).where(
                    WorkspaceService.workspace_id == workspace_id,
                    WorkspaceService.name == name,
                )
            )).scalar_one()
            if service.observed_state == "healthy":
                return time.monotonic() - started
            if service.observed_state in ("failed", "exited"):
                raise RuntimeError(f"service failed: exit={service.exit_code}")
        await asyncio.sleep(0.5)
    raise TimeoutError("service healthy timeout")


async def _one_iteration(user_id: int, index: int) -> dict:
    async with AsyncSessionLocal() as db:
        user = await db.get(User, user_id)
        project, workspace = await projects.create_project(
            db, user, f"Bench {int(time.time())}-{index}")
        await db.commit()
        await db.refresh(workspace)
        project_id, workspace_id = project.id, workspace.id

    async with AsyncSessionLocal() as db:
        sandbox = await runtime.provision_workspace(db, workspace)
        await db.commit()
        sandbox_id = sandbox.id
    provision_seconds = await _wait_sandbox(sandbox_id)

    command_started = time.monotonic()
    data = await agent_client.exec_command(
        sandbox_id, "echo bench-ok", wait_seconds=30, timeout_seconds=60)
    while data["status"] == "running":
        await asyncio.sleep(0.2)
        data = await agent_client.operation_status(sandbox_id, data["operation_id"])
    assert data["status"] == "succeeded", data
    command_seconds = time.monotonic() - command_started

    await agent_client.write_file(
        sandbox_id, "/workspace/index.html", "<h1>bench</h1>")

    async with AsyncSessionLocal() as db:
        user = await db.get(User, user_id)
        workspace_row = await db.get(Workspace, workspace_id)
        sandbox_row = await db.get(SandboxInstance, sandbox_id)
        await service_manager.create_service(
            db, workspace_row, sandbox_row, user,
            name="web", command="python3 -m http.server 8123 --bind 0.0.0.0",
            port=8123, working_directory="/workspace",
            health_check_type="http", health_check_timeout=60,
        )
        await db.commit()
    service_seconds = await _wait_service(workspace_id, sandbox_id, "web")

    preview_started = time.monotonic()
    async with AsyncSessionLocal() as db:
        user = await db.get(User, user_id)
        workspace_row = await db.get(Workspace, workspace_id)
        service_row = (await db.execute(
            select(WorkspaceService).where(
                WorkspaceService.workspace_id == workspace_id,
                WorkspaceService.name == "web",
            )
        )).scalar_one()
        grant, token = await preview.create_grant(
            db, workspace_row, service_row, user)
        await db.commit()
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.get(
            f"http://preview-proxy:8081/p/{token}/",
            headers={"Host": "preview-proxy:8081"},
        )
        assert response.status_code == 200 and "bench" in response.text
    preview_seconds = time.monotonic() - preview_started

    async with AsyncSessionLocal() as db:
        user = await db.get(User, user_id)
        await projects.delete_project(db, project_id, user, purge_volume=True)
        await db.commit()

    return {
        "iteration": index,
        "provision_ready_seconds": round(provision_seconds, 3),
        "command_seconds": round(command_seconds, 3),
        "service_healthy_seconds": round(service_seconds, 3),
        "preview_grant_first_request_seconds": round(preview_seconds, 3),
    }


def _summary(samples: list[dict]) -> dict:
    result = {}
    for key in (
        "provision_ready_seconds", "command_seconds",
        "service_healthy_seconds", "preview_grant_first_request_seconds",
    ):
        values = [float(item[key]) for item in samples]
        result[key] = {
            "samples": values,
            "p50": round(statistics.median(values), 3) if values else 0.0,
            "p95": round(_percentile(values, 0.95), 3) if values else 0.0,
            "min": round(min(values), 3) if values else 0.0,
            "max": round(max(values), 3) if values else 0.0,
        }
    return result


async def main() -> None:
    async with AsyncSessionLocal() as db:
        user = (await db.execute(select(User).order_by(User.id).limit(1))).scalar_one()
        user_id = user.id
    environment = {
        "platform": platform.platform(),
        "python": platform.python_version(),
        "cpu_count": os.cpu_count(),
        "sandbox_image": settings.SANDBOX_IMAGE,
        "sandbox_default_profile": settings.SANDBOX_DEFAULT_PROFILE,
        "cpu_reserve_cores": settings.SANDBOX_CPU_RESERVE,
        "memory_reserve_mb": settings.SANDBOX_MEMORY_RESERVE,
        "runner_disk_gb": settings.SANDBOX_RUNNER_DISK_GB,
        "iterations": ITERATIONS,
    }
    samples = []
    for index in range(1, ITERATIONS + 1):
        try:
            sample = await _one_iteration(user_id, index)
            samples.append(sample)
            print("ITERATION", json.dumps(sample, ensure_ascii=False), flush=True)
        except Exception as exc:  # noqa: BLE001 失败样本如实记录
            print("ITERATION_FAILED", index, repr(exc), flush=True)
    print("BENCHMARK_RESULT", json.dumps({
        "environment": environment,
        "samples": samples,
        "summary": _summary(samples),
        "failed": ITERATIONS - len(samples),
    }, ensure_ascii=False, indent=2), flush=True)


asyncio.run(main())
