"""Sandbox Job 执行器（Runner 进程内，唯一允许操作 Docker 的代码路径）。

安全约束（任务书 §7.3 / §7.5）：
- 镜像、Volume、Network、Mount、SecurityOpt 全部由服务端策略生成；
- 容器非 root、cap_drop=ALL、no-new-privileges、只读 rootfs、CPU/内存/PID 限制；
- 每个 Sandbox 独立 bridge network；Runner 以网关身份加入；
- 容器带平台标签；创建幂等（409 时按标签校验后接管，不静默覆盖）。
"""
from __future__ import annotations

import asyncio
import ipaddress
import logging
from datetime import datetime, timezone
from typing import Awaitable, Callable, Optional

import httpx

from app.core.config import settings
from app.services.sandbox.runtime import agent_token, sandbox_labels

from .engine import DockerEngine, DockerEngineError, wait_agent_ready

logger = logging.getLogger("ai_plat.sandbox.runner.executor")


class SandboxJobError(Exception):
    def __init__(self, code: str, message: str, *, oom: bool = False):
        super().__init__(message)
        self.code = code
        self.message = message
        self.oom = oom


def container_name(sandbox_id: int, generation: int) -> str:
    return f"{settings.SANDBOX_CONTAINER_PREFIX}{sandbox_id}-g{generation}"


def network_name(sandbox_id: int, generation: int) -> str:
    return f"gap-net-{sandbox_id}-g{generation}"


def _network_age_seconds(item: dict) -> float:
    """Docker 网络创建时间到现在的秒数（解析失败按「很旧」处理，允许清理）。"""
    raw = str(item.get("Created") or "")
    if not raw:
        return 1e9
    text = raw.replace("Z", "+00:00")
    if "." in text:
        head, tail = text.split(".", 1)
        digits = "".join(char for char in tail[:9] if char.isdigit())
        zone = "+00:00" if tail.endswith("+00:00") else ""
        text = f"{head}.{digits[:6]}{zone}"
    try:
        created = datetime.fromisoformat(text)
    except ValueError:
        return 1e9
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    return max(0.0, (datetime.now(timezone.utc) - created).total_seconds())


class RunnerExecutor:
    def __init__(self, engine: DockerEngine) -> None:
        self.engine = engine

    # ── 容器规格（服务端策略生成）───────────────────────────────────────
    def _container_spec(self, payload: dict, *, network: str, token: str) -> dict:
        profile = payload.get("profile") or {}
        cpu_request = int(profile.get("cpu_request_millis") or 500)
        cpu_limit = int(profile.get("cpu_limit_millis") or 1000)
        memory_request = int(profile.get("memory_request_bytes") or 512 * 1024 * 1024)
        memory_limit = int(profile.get("memory_limit_bytes") or 1024 * 1024 * 1024)
        pid_limit = int(profile.get("pid_limit") or 256)
        sandbox_id = int(payload["sandbox_id"])
        workspace_id = int(payload["workspace_id"])
        generation = int(payload["generation"])
        labels = sandbox_labels(workspace_id, sandbox_id, generation)
        labels.update({
            "platform.network": network,
            "platform.image": str(payload.get("image") or ""),
            "platform.cpu_request": str(cpu_request),
            "platform.memory_request": str(memory_request),
        })
        agent_port = int(settings.SANDBOX_AGENT_PORT)
        return {
            "Image": payload["image"],
            "Env": [
                f"WORKSPACE_AGENT_TOKEN={token}",
                f"WORKSPACE_AGENT_PORT={agent_port}",
                "WORKSPACE_ROOT=/workspace",
                "HOME=/home/sandbox",
                "PYTHONDONTWRITEBYTECODE=1",
                "PIP_USER=1",
                "PIP_DISABLE_PIP_VERSION_CHECK=1",
                "NPM_CONFIG_PREFIX=/home/sandbox/.npm-global",
            ],
            "WorkingDir": "/workspace",
            "Labels": labels,
            "HostConfig": {
                "Binds": [f"{payload['volume_id']}:/workspace"],
                "NetworkMode": network,
                "NanoCpus": cpu_limit * 1_000_000,
                "CpuShares": max(2, cpu_request),
                "Memory": memory_limit,
                "MemorySwap": memory_limit,
                "PidsLimit": pid_limit,
                "ReadonlyRootfs": True,
                "Tmpfs": {
                    "/tmp": "rw,noexec,nosuid,nodev,size=256m,mode=1777",
                    "/run": "rw,noexec,nosuid,nodev,size=64m,mode=755",
                    "/var/tmp": "rw,nosuid,nodev,size=128m,mode=1777",
                    # 用户目录挂载为 tmpfs：必须显式 uid/gid，否则 Docker 以 root
                    # 创建挂载点，非 root sandbox 用户无法写 npm/pip 缓存（EACCES）
                    "/home/sandbox/.npm": "rw,exec,size=256m,mode=700,uid=10001,gid=10001",
                    "/home/sandbox/.npm-global": "rw,exec,size=128m,mode=700,uid=10001,gid=10001",
                    "/home/sandbox/.cache": "rw,exec,size=256m,mode=700,uid=10001,gid=10001",
                    "/home/sandbox/.local": "rw,exec,size=256m,mode=700,uid=10001,gid=10001",
                },
                "CapDrop": ["ALL"],
                "SecurityOpt": ["no-new-privileges:true"],
                "Privileged": False,
                "Init": True,
                "PortBindings": {},
                "RestartPolicy": {"Name": "no"},
                "LogConfig": {
                    "Type": "json-file",
                    "Config": {"max-size": "10m", "max-file": "3"},
                },
                "Ulimits": [{"Name": "nofile", "Soft": 1024, "Hard": 4096}],
            },
        }

    async def _allocate_subnet(self) -> str:
        """从配置的子网池分配未占用网段（默认空 = Docker 自动分配）。

        Docker 默认 address pool 只支持约十几个 bridge 网络；并发 Sandbox 较多时
        必须配置 SANDBOX_NETWORK_SUBNET_POOL，否则创建网络会报
        ``all predefined address pools have been fully subnetted``。
        """
        pool = (settings.SANDBOX_NETWORK_SUBNET_POOL or "").strip()
        if not pool:
            return ""
        try:
            network = ipaddress.ip_network(pool, strict=False)
            prefix = int(settings.SANDBOX_NETWORK_SUBNET_PREFIX or 24)
            candidates = network.subnets(new_prefix=prefix)
        except ValueError:
            logger.warning("SANDBOX_NETWORK_SUBNET_POOL 配置无效：%s", pool)
            return ""
        try:
            networks = await self.engine.list_networks({})
        except DockerEngineError:
            return ""
        used = set()
        for item in networks:
            for config in ((item.get("IPAM") or {}).get("Config") or []):
                subnet = config.get("Subnet")
                if not subnet:
                    continue
                try:
                    used.add(ipaddress.ip_network(subnet, strict=False))
                except ValueError:
                    continue
        for candidate in candidates:
            if not any(candidate.overlaps(block) for block in used):
                return str(candidate)
        logger.warning("Sandbox 子网池 %s 已用尽（prefix=/%s）", pool, prefix)
        return ""

    async def _ensure_network(self, payload: dict) -> str:
        name = network_name(int(payload["sandbox_id"]), int(payload["generation"]))
        internal = str(payload.get("network_policy") or "egress") == "none"
        await self.engine.ensure_network(
            name, internal=internal,
            labels=sandbox_labels(
                int(payload["workspace_id"]), int(payload["sandbox_id"]),
                int(payload["generation"]),
            ),
            subnet=await self._allocate_subnet(),
        )
        runner_container = settings.SANDBOX_RUNNER_CONTAINER or _self_container_id()
        if runner_container:
            await self.engine.connect_network(name, runner_container)
        return name

    async def _ensure_container(self, payload: dict, *, network: str) -> tuple[str, str]:
        sandbox_id = int(payload["sandbox_id"])
        generation = int(payload["generation"])
        name = container_name(sandbox_id, generation)
        token = agent_token(sandbox_id, generation)
        spec = self._container_spec(payload, network=network, token=token)
        container_id, adopted = await self.engine.create_container(name, spec)
        if adopted:
            info = await self.engine.inspect_container(container_id)
            labels = ((info.get("Config") or {}).get("Labels") or {})
            expected = spec["Labels"]
            mismatched = [
                key for key in ("platform.workspace_id", "platform.sandbox_id", "platform.generation")
                if str(labels.get(key)) != str(expected.get(key))
            ]
            if mismatched:
                raise SandboxJobError(
                    "sandbox_label_conflict",
                    f"同名容器标签不一致（{', '.join(mismatched)}），进入人工核对，禁止静默覆盖",
                )
            logger.info("接管已存在 Sandbox 容器 %s（幂等创建）", name)
        return container_id, name

    async def _wait_ready(self, payload: dict, container_id: str, network: str) -> dict:
        sandbox_id = int(payload["sandbox_id"])
        generation = int(payload["generation"])
        token = agent_token(sandbox_id, generation)
        ip = await self.engine.container_ip(container_id, network)
        ok = await wait_agent_ready(
            sandbox_id, ip, int(settings.SANDBOX_AGENT_PORT), token, timeout_seconds=90,
        )
        if not ok:
            state = await self.engine.container_state(container_id)
            raise SandboxJobError(
                "agent_unavailable",
                f"workspace-agent 未就绪（容器状态 {state.get('status')}）",
            )
        return {"container_ip": ip or ""}

    async def _start_auto_services(self, payload: dict, ip: str) -> list[dict]:
        services = payload.get("auto_services") or []
        if not services or not ip:
            return []
        token = agent_token(int(payload["sandbox_id"]), int(payload["generation"]))
        results: list[dict] = []
        async with httpx.AsyncClient(timeout=120.0) as client:
            for svc in services:
                body = {
                    "name": svc.get("name"),
                    "command": svc.get("command"),
                    "working_directory": svc.get("working_directory") or "/workspace",
                    "port": int(svc.get("port") or 0),
                    "protocol": svc.get("protocol") or "http",
                    "health_check_type": svc.get("health_check_type") or "http",
                    "health_check_path": svc.get("health_check_path") or "/",
                    "health_check_timeout": int(svc.get("health_check_timeout") or 60),
                    "auto_restart": bool(svc.get("auto_restart", True)),
                }
                try:
                    response = await client.post(
                        f"http://{ip}:{settings.SANDBOX_AGENT_PORT}/services",
                        json=body,
                        headers={"Authorization": f"Bearer {token}"},
                    )
                    data = response.json() if response.status_code < 400 else {}
                    results.append({
                        "name": svc.get("name"),
                        "observed_state": data.get("observed_state") or (
                            "failed" if response.status_code >= 400 else "starting"
                        ),
                        "process_id": data.get("process_id"),
                        "error": None if response.status_code < 400 else response.text[:300],
                    })
                except httpx.HTTPError as exc:
                    results.append({
                        "name": svc.get("name"), "observed_state": "failed",
                        "error": str(exc)[:300],
                    })
        return results

    # ── 生命周期 ────────────────────────────────────────────────────────

    async def _provision(self, payload: dict, on_state: Callable[[str], Awaitable[None]]) -> dict:
        workspace_id = int(payload["workspace_id"])
        sandbox_id = int(payload["sandbox_id"])
        generation = int(payload["generation"])
        labels = sandbox_labels(workspace_id, sandbox_id, generation)
        digest = await self.engine.ensure_image(payload["image"])
        try:
            await self.engine.ensure_volume(str(payload["volume_id"]), {
                **labels, "platform.kind": "workspace-volume",
            })
        except DockerEngineError as exc:
            raise SandboxJobError("volume_create_failed", str(exc)) from exc
        network = await self._ensure_network(payload)
        await on_state("provisioning")
        try:
            container_id, name = await self._ensure_container(payload, network=network)
        except DockerEngineError as exc:
            raise SandboxJobError("container_create_failed", str(exc),
                                  oom=exc.status_code == 507) from exc
        await on_state("starting")
        await self.engine.start_container(container_id)
        ready = await self._wait_ready(payload, container_id, network)
        services = await self._start_auto_services(payload, ready["container_ip"])
        return {
            "container_id": container_id,
            "container_name": name,
            "container_ip": ready["container_ip"],
            "image_digest": digest,
            "observed_state": "ready",
            "network": network,
            "services": services,
        }

    async def _find_existing(self, payload: dict) -> Optional[str]:
        containers = await self.engine.list_containers({
            "platform.managed": "true",
            "platform.sandbox_id": str(payload["sandbox_id"]),
            "platform.generation": str(payload["generation"]),
        })
        if not containers:
            return None
        return containers[0]["Id"]

    async def _start(self, payload: dict, on_state: Callable[[str], Awaitable[None]]) -> dict:
        sandbox_id = int(payload["sandbox_id"])
        generation = int(payload["generation"])
        network = network_name(sandbox_id, generation)
        container_id = await self._find_existing(payload)
        if container_id is None:
            # 容器丢失（宿主清理/迁移）：按同一 Generation 幂等重建，Volume 不变
            return await self._provision(payload, on_state)
        await self._ensure_network(payload)
        await on_state("starting")
        await self.engine.start_container(container_id)
        ready = await self._wait_ready(payload, container_id, network)
        digest = await self.engine.image_digest(payload["image"])
        services = await self._start_auto_services(payload, ready["container_ip"])
        return {
            "container_id": container_id,
            "container_name": container_name(sandbox_id, generation),
            "container_ip": ready["container_ip"],
            "image_digest": digest,
            "observed_state": "ready",
            "network": network,
            "services": services,
        }

    async def _stop(self, payload: dict, on_state: Callable[[str], Awaitable[None]]) -> dict:
        container_id = await self._find_existing(payload)
        if container_id is not None:
            await on_state("stopping")
            await self.engine.stop_container(container_id, timeout=10)
        return {"observed_state": "stopped", "services": [
            {"name": svc.get("name"), "observed_state": "stopped"}
            for svc in (payload.get("auto_services") or [])
        ]}

    async def _restart(self, payload: dict, on_state: Callable[[str], Awaitable[None]]) -> dict:
        container_id = await self._find_existing(payload)
        if container_id is None:
            return await self._provision(payload, on_state)
        network = network_name(int(payload["sandbox_id"]), int(payload["generation"]))
        await on_state("starting")
        try:
            await self.engine.stop_container(container_id, timeout=10)
        except DockerEngineError:
            pass
        await self.engine.start_container(container_id)
        ready = await self._wait_ready(payload, container_id, network)
        digest = await self.engine.image_digest(payload["image"])
        services = await self._start_auto_services(payload, ready["container_ip"])
        return {
            "container_id": container_id,
            "container_name": container_name(
                int(payload["sandbox_id"]), int(payload["generation"])),
            "container_ip": ready["container_ip"],
            "image_digest": digest,
            "observed_state": "ready",
            "network": network,
            "services": services,
        }

    async def _delete(self, payload: dict, on_state: Callable[[], Awaitable[None]]) -> dict:
        container_id = await self._find_existing(payload)
        if container_id is not None:
            await on_state("stopping")
            await self.engine.stop_container(container_id, timeout=5)
            await self.engine.remove_container(container_id, force=True)
        network = network_name(int(payload["sandbox_id"]), int(payload["generation"]))
        runner_container = settings.SANDBOX_RUNNER_CONTAINER or _self_container_id()
        if runner_container:
            # Runner 是该网络的端点；不先断开会导致 DELETE 失败并留下孤儿网络
            try:
                await self.engine.disconnect_network(network, runner_container)
            except DockerEngineError:
                pass
        try:
            await self.engine.remove_network(network)
        except DockerEngineError as exc:
            logger.warning("删除 Sandbox 网络失败（留待对账清理）%s: %s", network, exc)
        if payload.get("purge_volume"):
            await self.engine.remove_volume(str(payload["volume_id"]))
        return {"observed_state": "stopped", "volume_retained": not bool(payload.get("purge_volume"))}

    async def _reconcile(self, payload: dict) -> dict:
        container_id = await self._find_existing(payload)
        if container_id is None:
            return {"observed_state": "failed", "reason": "container_missing"}
        state = await self.engine.container_state(container_id)
        return {"observed_state": state["observed_state"],
                "container_id": container_id,
                "container_name": state["container_name"],
                "oom_killed": state.get("oom_killed", False)}

    async def execute(
        self, job: dict, on_state: Callable[[str], Awaitable[None]],
    ) -> dict:
        job_type = job.get("job_type")
        payload = job.get("payload") or {}
        if job_type == "provision":
            return await self._provision(payload, on_state)
        if job_type == "start":
            return await self._start(payload, on_state)
        if job_type == "stop":
            return await self._stop(payload, on_state)
        if job_type == "restart":
            return await self._restart(payload, on_state)
        if job_type == "delete":
            return await self._delete(payload, on_state)
        if job_type == "reconcile":
            return await self._reconcile(payload)
        raise SandboxJobError("unknown_job_type", f"未知 Job 类型: {job_type}")

    async def list_managed(self) -> list[dict]:
        containers = await self.engine.list_containers({"platform.managed": "true"})
        items = []
        for container in containers:
            labels = container.get("Labels") or {}
            sandbox_id = labels.get("platform.sandbox_id")
            workspace_id = labels.get("platform.workspace_id")
            if not sandbox_id or not workspace_id:
                continue
            try:
                state = await self.engine.container_state(container["Id"])
                network = labels.get("platform.network") or ""
                ip = await self.engine.container_ip(container["Id"], network) if network else None
            except DockerEngineError as exc:
                logger.warning("读取容器状态失败 %s: %s", container.get("Id"), exc)
                continue
            items.append({
                "sandbox_id": int(sandbox_id),
                "workspace_id": int(workspace_id),
                "generation": int(labels.get("platform.generation") or 0),
                "container_id": container["Id"],
                "container_name": state.get("container_name"),
                "container_ip": ip,
                "image_digest": state.get("image"),
                "observed_state": state.get("observed_state"),
                "oom_killed": state.get("oom_killed"),
            })
        return items

    async def cleanup_orphan_networks(self) -> int:
        """清理没有对应 Sandbox 容器的平台网络（删除失败/测试残留）。

        每个 Sandbox 一个 bridge 网络，Docker 默认地址池容量有限；孤儿网络会
        逐步耗尽地址池，导致新 Sandbox 创建时报 400
        ``all predefined address pools have been fully subnetted``。
        """
        runner_container = settings.SANDBOX_RUNNER_CONTAINER or _self_container_id()
        try:
            containers = await self.engine.list_containers({"platform.managed": "true"})
        except DockerEngineError:
            return 0
        active = {str((item.get("Labels") or {}).get("platform.network") or "")
                  for item in containers}
        active.discard("")
        try:
            networks = await self.engine.list_networks({"platform.managed": "true"})
        except DockerEngineError:
            return 0
        min_age = max(600.0, float(settings.SANDBOX_RECONCILE_INTERVAL) * 3)
        removed = 0
        for network in networks:
            name = str(network.get("Name") or "")
            if not name.startswith("gap-net-") or name in active:
                continue
            # 新建网络可能正处于 Provision 中（网络已建、容器未建），不清理
            if _network_age_seconds(network) < min_age:
                continue
            try:
                detail = await self.engine.inspect_network(name)
            except DockerEngineError:
                continue
            endpoints = detail.get("Containers") or {}
            foreign = [
                container_id for container_id, info in endpoints.items()
                if container_id != runner_container
                and str((info or {}).get("Name") or "").lstrip("/") != runner_container
            ]
            # 存在非 Runner 端点时不自动删除（可能是未知容器网络，只告警）
            if foreign:
                logger.warning("孤儿网络存在未知容器端点，跳过清理：%s", name)
                continue
            runner_attached = runner_container and (
                runner_container in endpoints
                or any(str((info or {}).get("Name") or "").lstrip("/") == runner_container
                       for info in endpoints.values())
            )
            if runner_attached:
                try:
                    await self.engine.disconnect_network(name, runner_container)
                except DockerEngineError:
                    pass
            try:
                await self.engine.remove_network(name)
                removed += 1
            except DockerEngineError as exc:
                logger.warning("清理孤儿 Sandbox 网络失败 %s: %s", name, exc)
        if removed:
            logger.info("已清理孤儿 Sandbox 网络 %d 个", removed)
        return removed

    async def attach_managed_networks(self) -> int:
        """Runner 重启后重新加入所有已管理 Sandbox 的独立网络。

        Runner 容器重建后不会保留动态接入的网络；不重新接入会导致 Gateway
        无法访问仍在运行的 workspace-agent / Service（HTTP 502）。
        """
        runner_container = settings.SANDBOX_RUNNER_CONTAINER or _self_container_id()
        if not runner_container:
            return 0
        try:
            await self.cleanup_orphan_networks()
        except Exception as exc:  # noqa: BLE001 清理失败不阻断重新接入
            logger.warning("清理孤儿 Sandbox 网络失败：%s", exc)
        attached = 0
        try:
            containers = await self.engine.list_containers({"platform.managed": "true"})
        except DockerEngineError:
            return 0
        for container in containers:
            labels = container.get("Labels") or {}
            network = labels.get("platform.network")
            if not network:
                continue
            try:
                await self.engine.connect_network(network, runner_container)
                attached += 1
            except DockerEngineError as exc:
                logger.warning("Runner 重新接入网络失败 %s: %s", network, exc)
        return attached

    async def sample_usage(self) -> dict:
        """采样实际资源使用（container stats + workspace volume du）。

        仅用于监控指标（预留量在数据库），采样失败不影响对账与业务。
        """
        items = await self.list_managed()
        running = [item for item in items if item.get("observed_state") == "ready"]
        cpu_cores = 0.0
        memory_bytes = 0
        volume_bytes = 0
        sandbox_usage: list[dict] = []
        async with httpx.AsyncClient(timeout=10.0) as client:
            for index, item in enumerate(running[:10]):
                try:
                    stats = await self.engine.stats_one_shot(item["container_id"])
                    cpu, memory = _stats_values(stats)
                    cpu_cores += cpu
                    memory_bytes += memory
                except DockerEngineError:
                    continue
                # Volume 实际占用（限前 5 个，du 有成本）；同时上报单个 Sandbox 用量供软配额预警
                if index < 5 and item.get("container_ip"):
                    token = agent_token(int(item["sandbox_id"]), int(item["generation"]))
                    try:
                        response = await client.get(
                            f"http://{item['container_ip']}:{settings.SANDBOX_AGENT_PORT}/files/usage",
                            headers={"Authorization": f"Bearer {token}"},
                        )
                        if response.status_code == 200:
                            used = int(response.json().get("bytes") or 0)
                            volume_bytes += used
                            sandbox_usage.append({
                                "sandbox_id": int(item["sandbox_id"]),
                                "volume_bytes": used,
                            })
                    except (httpx.HTTPError, ValueError):
                        pass
        return {
            "cpu_cores": round(cpu_cores, 3),
            "memory_bytes": int(memory_bytes),
            "volume_bytes": int(volume_bytes),
            "sandboxes": sandbox_usage,
        }

    async def usage(self) -> dict:
        """Runner 资源总量（CPU 核 / 内存 / 磁盘）与已用容器统计。"""
        try:
            info = await self.engine.info()
        except DockerEngineError:
            return {}
        cpu_total = int(info.get("NCPU") or 1) * 1000
        memory_total = int(info.get("MemTotal") or 0)
        disk_total = int(settings.SANDBOX_RUNNER_DISK_GB) * 1024 * 1024 * 1024
        cpu_alloc = max(0, cpu_total - int(float(settings.SANDBOX_CPU_RESERVE) * 1000))
        memory_alloc = max(0, memory_total - int(settings.SANDBOX_MEMORY_RESERVE) * 1024 * 1024)
        disk_alloc = max(0, disk_total - int(settings.SANDBOX_DISK_RESERVE) * 1024 * 1024 * 1024)
        try:
            version = (await self.engine.version()).get("Version") or ""
        except DockerEngineError:
            version = ""
        try:
            running = (await self.engine.list_containers({"platform.managed": "true"}))
            active = sum(1 for c in running if str(c.get("State")) == "running")
        except DockerEngineError:
            active = 0
        return {
            "cpu_total_millis": cpu_total,
            "cpu_allocatable_millis": cpu_alloc,
            "memory_total_bytes": memory_total,
            "memory_allocatable_bytes": memory_alloc,
            "disk_total_bytes": disk_total,
            "disk_allocatable_bytes": disk_alloc,
            "active_sandbox_count": active,
            "runtime_version": version,
        }


def _stats_values(stats: dict) -> tuple[float, int]:
    """解析 Docker stats（one-shot）：返回 (CPU 核, 内存字节)。"""
    try:
        cpu_stats = stats.get("cpu_stats") or {}
        pre_stats = stats.get("precpu_stats") or {}
        cpu_total = int((cpu_stats.get("cpu_usage") or {}).get("total_usage") or 0)
        pre_total = int((pre_stats.get("cpu_usage") or {}).get("total_usage") or 0)
        system = int(cpu_stats.get("system_cpu_usage") or 0)
        pre_system = int(pre_stats.get("system_cpu_usage") or 0)
        online = int(cpu_stats.get("online_cpus") or len(
            (cpu_stats.get("cpu_usage") or {}).get("percpu_usage") or []) or 1)
        cpu_delta = cpu_total - pre_total
        system_delta = system - pre_system
        cores = (cpu_delta / system_delta) * online if system_delta > 0 and cpu_delta > 0 else 0.0
        memory_stats = stats.get("memory_stats") or {}
        usage = int(memory_stats.get("usage") or 0)
        cache = int((memory_stats.get("stats") or {}).get("cache") or 0)
        return max(0.0, cores), max(0, usage - cache)
    except (TypeError, ValueError):
        return 0.0, 0


def _self_container_id() -> str:
    import os

    return os.environ.get("HOSTNAME", "")
