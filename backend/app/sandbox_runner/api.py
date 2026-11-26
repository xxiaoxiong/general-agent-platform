"""Sandbox Runner 进程入口：注册 / 心跳 / 领取 Job / 执行 / 对账 / Gateway。

独立于 Uvicorn Backend 进程部署（Docker Socket 只挂载给本进程）。
"""
from __future__ import annotations

import asyncio
import logging
import os
import socket
from contextlib import asynccontextmanager
from typing import Any, Optional

import httpx
from fastapi import FastAPI, Response

from app.core.config import settings
from app.observability import metrics
from app.sandbox_runner import gateway
from app.sandbox_runner.engine import DockerEngine, DockerEngineError
from app.sandbox_runner.executor import RunnerExecutor, SandboxJobError

logger = logging.getLogger("ai_plat.sandbox.runner")


class RunnerApiError(Exception):
    def __init__(self, status_code: int, message: str):
        super().__init__(f"Backend {status_code}: {message}")
        self.status_code = status_code


class SandboxRunnerAgent:
    def __init__(self) -> None:
        self.engine = DockerEngine()
        self.executor = RunnerExecutor(self.engine)
        self.node_name = (settings.SANDBOX_RUNNER_ID or socket.gethostname())[:128]
        self.runner_id: Optional[int] = None
        self._backend = (settings.SANDBOX_BACKEND_URL or "").rstrip("/")
        self._headers = {"X-Runner-Token": settings.SANDBOX_RUNNER_TOKEN}
        self._stop = asyncio.Event()
        self._loops: list[asyncio.Task] = []
        self._job_tasks: set[asyncio.Task] = set()
        self._semaphore = asyncio.Semaphore(4)

    # ── Backend 内部 API ──────────────────────────────────────────────
    async def _post(self, path: str, payload: dict, *, timeout: float = 30.0) -> dict:
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.post(
                    f"{self._backend}{path}", json=payload, headers=self._headers,
                )
        except httpx.HTTPError as exc:
            raise RunnerApiError(0, f"Backend 不可达: {exc}") from exc
        if response.status_code >= 400:
            raise RunnerApiError(response.status_code, response.text[:500])
        try:
            return response.json()
        except Exception:  # noqa: BLE001
            return {}

    async def _register(self) -> None:
        usage = await self.executor.usage()
        payload = {
            "node_name": self.node_name,
            "runtime_provider": settings.SANDBOX_PROVIDER,
            "runtime_version": usage.get("runtime_version") or "",
            "capabilities": {
                "docker": True,
                "websocket": True,
                "preview": True,
                "network_policy": settings.SANDBOX_NETWORK_POLICY,
            },
            **usage,
        }
        data = await self._post("/internal/runners/register", payload)
        self.runner_id = int(data["runner_id"])
        logger.info("Sandbox Runner 注册成功（id=%s, node=%s）", self.runner_id, self.node_name)

    async def _register_with_retry(self, attempts: int = 60) -> None:
        for attempt in range(1, attempts + 1):
            try:
                await self._register()
                return
            except RunnerApiError as exc:
                logger.warning("Runner 注册失败（第 %s 次）：%s", attempt, exc)
                await asyncio.sleep(min(10, attempt * 2))
        raise RuntimeError("Sandbox Runner 无法注册到 Backend，进程退出")

    # ── 生命周期 ──────────────────────────────────────────────────────
    async def start(self) -> None:
        self._stop.clear()
        if not settings.SANDBOX_ENABLED:
            logger.info("SANDBOX_ENABLED=false：Runner 保持空闲（不注册、不领取任务）")
            return
        await self._register_with_retry()
        gateway.set_engine(self.engine)
        # 重启后重新接入所有既有 Sandbox 网络，恢复 Gateway 连通性
        try:
            attached = await self.executor.attach_managed_networks()
            if attached:
                logger.info("Runner 已重新接入 %s 个 Sandbox 网络", attached)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Runner 重新接入 Sandbox 网络失败：%s", exc)
        self._loops = [
            asyncio.create_task(self._control_loop(), name="sandbox-runner-control"),
            asyncio.create_task(self._reconcile_loop(), name="sandbox-runner-reconcile"),
        ]
        logger.info(
            "Sandbox Runner 启动（node=%s, heartbeat=%.1fs, reconcile=%.1fs）",
            self.node_name,
            float(settings.SANDBOX_RUNNER_HEARTBEAT_INTERVAL),
            float(settings.SANDBOX_RECONCILE_INTERVAL),
        )

    async def stop(self) -> None:
        self._stop.set()
        for task in self._loops:
            task.cancel()
        for task in list(self._job_tasks):
            task.cancel()
        if self._job_tasks:
            await asyncio.wait(list(self._job_tasks), timeout=30)
        await self.engine.close()
        logger.info("Sandbox Runner 已停止（不删除 Workspace Volume）")

    # ── 控制循环 ──────────────────────────────────────────────────────
    async def _control_loop(self) -> None:
        while not self._stop.is_set():
            try:
                await self._heartbeat_and_claim()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.warning("Runner 控制循环失败（下轮继续）：%s", exc)
            try:
                await asyncio.wait_for(
                    self._stop.wait(),
                    timeout=max(2.0, float(settings.SANDBOX_RUNNER_HEARTBEAT_INTERVAL)),
                )
            except asyncio.TimeoutError:
                continue

    async def _heartbeat_and_claim(self) -> None:
        if self.runner_id is None:
            await self._register()
        usage = await self.executor.usage()
        await self._post(
            f"/internal/runners/{self.runner_id}/heartbeat",
            {"status": "online", **usage},
        )
        data = await self._post(
            f"/internal/runners/{self.runner_id}/jobs/claim", {"limit": 2},
        )
        for job in data.get("jobs") or []:
            task = asyncio.create_task(self._run_job(job), name=f"sandbox-job-{job['id']}")
            self._job_tasks.add(task)
            task.add_done_callback(self._job_tasks.discard)

    async def _reconcile_loop(self) -> None:
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(
                    self._stop.wait(),
                    timeout=max(5.0, float(settings.SANDBOX_RECONCILE_INTERVAL)),
                )
                continue
            except asyncio.TimeoutError:
                pass
            try:
                if self.runner_id is None:
                    continue
                try:
                    await self.executor.attach_managed_networks()
                except Exception:  # noqa: BLE001 网络接入失败不阻断对账
                    pass
                items = await self.executor.list_managed()
                usage = await self.executor.sample_usage()
                result = await self._post(
                    f"/internal/runners/{self.runner_id}/sandboxes/report",
                    {"items": items, "usage": usage},
                )
                if result.get("unknown"):
                    metrics.inc_sandbox_reconcile_mismatch()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.warning("Runner 对账失败（下轮继续）：%s", exc)

    # ── Job 执行 ──────────────────────────────────────────────────────
    async def _run_job(self, job: dict) -> None:
        job_id = job.get("id")
        lease_token = int(job.get("lease_token") or 0)
        async with self._semaphore:
            heartbeat_stop = asyncio.Event()
            fenced = asyncio.Event()
            heartbeat_task = asyncio.create_task(
                self._job_heartbeat(job_id, lease_token, heartbeat_stop, fenced),
                name=f"sandbox-job-heartbeat-{job_id}",
            )
            try:
                async def _on_state(state: str) -> None:
                    await self._report_state(job, state)

                exec_task = asyncio.create_task(
                    self.executor.execute(job, _on_state), name=f"sandbox-exec-{job_id}",
                )
                fence_task = asyncio.create_task(fenced.wait(), name=f"sandbox-fence-{job_id}")
                done, _pending = await asyncio.wait(
                    {exec_task, fence_task}, return_when=asyncio.FIRST_COMPLETED,
                )
                if exec_task not in done:
                    exec_task.cancel()
                    logger.warning("Job %s 租约失效（fencing），停止执行", job_id)
                    return
                fence_task.cancel()
                result = exec_task.result()
                await self._post(
                    f"/internal/jobs/{job_id}/complete",
                    {"runner_id": self.runner_id, "lease_token": lease_token,
                     "result": result},
                    timeout=60,
                )
                logger.info("Job %s（%s）完成", job_id, job.get("job_type"))
            except asyncio.CancelledError:
                raise
            except SandboxJobError as exc:
                await self._fail_job(job_id, lease_token, exc.code, exc.message, oom=exc.oom)
            except DockerEngineError as exc:
                await self._fail_job(
                    job_id, lease_token, "docker_error", str(exc),
                    oom=exc.status_code in (500, 507) and "memory" in exc.message.lower(),
                )
            except RunnerApiError as exc:
                logger.warning("Job %s 结果上报失败（租约可能已过期）：%s", job_id, exc)
            except Exception as exc:  # noqa: BLE001
                await self._fail_job(job_id, lease_token, "runner_error", str(exc)[:1000])
            finally:
                heartbeat_stop.set()
                heartbeat_task.cancel()
                try:
                    await heartbeat_task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass

    async def _job_heartbeat(self, job_id: str, lease_token: int,
                             stop: asyncio.Event, fenced: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                await asyncio.wait_for(
                    stop.wait(),
                    timeout=max(3.0, float(settings.SANDBOX_JOB_HEARTBEAT_INTERVAL)),
                )
                return
            except asyncio.TimeoutError:
                pass
            try:
                await self._post(
                    f"/internal/jobs/{job_id}/heartbeat",
                    {"runner_id": self.runner_id, "lease_token": lease_token},
                )
            except RunnerApiError as exc:
                if exc.status_code in (409, 404):
                    fenced.set()
                    return
                logger.warning("Job %s 心跳失败：%s", job_id, exc)

    async def _report_state(self, job: dict, state: str) -> None:
        payload = job.get("payload") or {}
        try:
            await self._post(
                f"/internal/runners/{self.runner_id}/sandboxes/report",
                {"items": [{
                    "sandbox_id": payload.get("sandbox_id"),
                    "workspace_id": payload.get("workspace_id"),
                    "generation": payload.get("generation"),
                    "observed_state": state,
                }]},
            )
        except RunnerApiError as exc:
            logger.debug("Job %s 状态上报失败：%s", job.get("id"), exc)

    async def _fail_job(self, job_id: str, lease_token: int, code: str,
                        message: str, *, oom: bool = False) -> None:
        try:
            await self._post(
                f"/internal/jobs/{job_id}/fail",
                {"runner_id": self.runner_id, "lease_token": lease_token,
                 "error_code": code, "error_message": message[:4000], "oom": bool(oom)},
            )
            logger.warning("Job %s 失败：%s %s", job_id, code, message[:300])
        except RunnerApiError as exc:
            logger.warning("Job %s 失败上报被拒绝：%s", job_id, exc)


_agent: Optional[SandboxRunnerAgent] = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _agent
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if not settings.SANDBOX_ENABLED:
        logger.warning("SANDBOX_ENABLED=false：Runner 仍会启动但不会领取任务")
    _agent = SandboxRunnerAgent()
    await _agent.start()
    try:
        yield
    finally:
        await _agent.stop()
        _agent = None


app = FastAPI(title="Sandbox Runner", lifespan=lifespan)
app.include_router(gateway.router)


@app.get("/health")
async def health():
    agent = _agent
    return {
        "status": "ok" if agent and agent.runner_id else "starting",
        "runner_id": agent.runner_id if agent else None,
        "node_name": agent.node_name if agent else os.environ.get("HOSTNAME", ""),
    }


@app.get("/metrics")
async def prometheus_metrics():
    body, content_type = metrics.render()
    return Response(content=body, media_type=content_type)
