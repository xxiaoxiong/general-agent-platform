"""Workspace Sandbox Docker 集成测试（真实 Docker Engine，非 Mock）。

运行方式（在带 Docker Socket 的 Runner 容器内）：
    docker compose -f docker/docker-compose.yml run --rm \
      -e SANDBOX_DOCKER_TEST=1 \
      -e DATABASE_URL=sqlite+aiosqlite:////tmp/sandbox_it.db \
      -e DATABASE_SYNC_URL=sqlite:////tmp/sandbox_it.db \
      sandbox-runner python -m pytest tests/test_sandbox_docker_integration.py -q

验证：
1. 镜像 / Volume / 独立网络 / 容器创建（非 root、只读 rootfs、cap_drop）；
2. workspace-agent 命令执行、文件写入与命令读取同一份 /workspace；
3. Service 启动、健康检查、HTTP 访问、停止；
4. 容器删除后 Volume 保留，新 Generation 挂载同一 Volume 文件仍在；
5. 网络隔离：Sandbox 无法解析平台服务名（backend/mysql/redis）；
6. 只读 rootfs：系统目录不可写。
"""
from __future__ import annotations

import asyncio
import os
import uuid

import httpx
import pytest

from app.core.config import settings
from app.sandbox_runner.engine import DockerEngine
from app.sandbox_runner.executor import RunnerExecutor
from app.services.sandbox.runtime import agent_token

pytestmark = pytest.mark.asyncio

_ENABLED = os.environ.get("SANDBOX_DOCKER_TEST") == "1"
pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(not _ENABLED, reason="设置 SANDBOX_DOCKER_TEST=1 才运行真实 Docker 集成测试"),
]

_AGENT_PORT = int(settings.SANDBOX_AGENT_PORT)


def _payload(suffix: str, generation: int, volume: str) -> dict:
    return {
        "workspace_id": 990001,
        "project_id": 990001,
        "sandbox_id": 990002,
        "generation": generation,
        "volume_id": volume,
        "image": settings.SANDBOX_IMAGE,
        "network_policy": "egress",
        "profile": {
            "cpu_request_millis": 250,
            "cpu_limit_millis": 500,
            "memory_request_bytes": 256 * 1024 * 1024,
            "memory_limit_bytes": 512 * 1024 * 1024,
            "disk_quota_bytes": 1024 * 1024 * 1024,
            "pid_limit": 128,
        },
        "auto_services": [],
        "suffix": suffix,
    }


async def _agent(ip: str, token: str, method: str, path: str,
                 *, timeout: float = 60.0, **kwargs) -> dict:
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.request(
            method, f"http://{ip}:{_AGENT_PORT}{path}",
            headers={"Authorization": f"Bearer {token}"}, **kwargs,
        )
        response.raise_for_status()
        return response.json()


async def test_real_docker_sandbox_lifecycle_and_isolation():
    engine = DockerEngine()
    if not await engine.ping():
        pytest.skip("Docker Socket 不可用（需在挂载 /var/run/docker.sock 的 Runner 容器内运行）")
    executor = RunnerExecutor(engine)
    suffix = uuid.uuid4().hex[:8]
    volume = f"gap-ws-it-{suffix}"
    sandbox_id = 990002
    token = agent_token(sandbox_id, 1)
    states: list[str] = []

    async def on_state(state: str) -> None:
        states.append(state)

    payload1 = _payload(suffix, 1, volume)
    payload2 = {**_payload(suffix, 2, volume)}
    container_ids: list[str] = []
    try:
        # ── Generation 1：创建 + 启动 + Agent Ready ────────────────────
        result = await executor.execute({"job_type": "provision", "payload": payload1}, on_state)
        container_ids.append(result["container_id"])
        assert result["observed_state"] == "ready"
        assert "provisioning" in states and "starting" in states
        ip = result["container_ip"]
        assert ip, "容器必须分配独立网络 IP"

        # 幂等：重复 provision 接管同一容器，不重复创建
        again = await executor.execute({"job_type": "provision", "payload": payload1}, on_state)
        assert again["container_id"] == result["container_id"]

        # ── 容器安全基线（inspect 校验）────────────────────────────────
        info = await engine.inspect_container(result["container_id"])
        host = info["HostConfig"]
        assert host["Privileged"] is False
        assert "ALL" in host["CapDrop"]
        assert host["ReadonlyRootfs"] is True
        assert host["Memory"] == 512 * 1024 * 1024
        assert host["NanoCpus"] == 500_000_000
        assert host["PidsLimit"] == 128
        assert any("no-new-privileges" in item for item in host["SecurityOpt"])
        assert info["Config"]["User"] in ("", None) or not info["Config"]["User"].startswith("0")
        assert info["Config"]["Labels"].get("platform.managed") == "true"

        # ── Runner 重启恢复：断开自身网络后重新接入，Gateway 恢复 ─────────
        from app.sandbox_runner.executor import _self_container_id

        network = f"gap-net-{sandbox_id}-g1"
        await engine.disconnect_network(network, _self_container_id())
        await asyncio.sleep(2)
        with pytest.raises(httpx.HTTPError):
            await _agent(ip, token, "GET", "/health", timeout=4.0)
        attached = await executor.attach_managed_networks()
        assert attached >= 1
        recovered = await _agent(ip, token, "GET", "/health", timeout=10.0)
        assert recovered["status"] == "ok"

        # ── 命令执行 ────────────────────────────────────────────────────
        echo = await _agent(ip, token, "POST", "/exec", json={
            "command": "echo hello-sandbox && pwd", "working_directory": "/workspace",
            "timeout_seconds": 30, "wait_seconds": 10,
        })
        assert echo["status"] == "succeeded", echo
        assert "hello-sandbox" in echo["stdout"]
        assert "/workspace" in echo["stdout"]

        # ── 文件工具与命令工具同一份文件 ────────────────────────────────
        await _agent(ip, token, "POST", "/files/write", json={
            "path": "/workspace/demo.txt", "content": "persisted-data",
        })
        cat = await _agent(ip, token, "POST", "/exec", json={
            "command": "cat demo.txt", "working_directory": "/workspace",
            "timeout_seconds": 30, "wait_seconds": 10,
        })
        assert "persisted-data" in cat["stdout"]

        # ── 只读 rootfs：系统目录不可写 ─────────────────────────────────
        readonly = await _agent(ip, token, "POST", "/exec", json={
            "command": "touch /usr/local/bin/should-fail", "working_directory": "/workspace",
            "timeout_seconds": 30, "wait_seconds": 10,
        })
        assert readonly["exit_code"] != 0

        # ── 网络隔离：不能解析平台服务名 ─────────────────────────────────
        isolation = await _agent(ip, token, "POST", "/exec", json={
            "command": (
                "python3 - <<'PY'\n"
                "import socket\n"
                "socket.setdefaulttimeout(1)\n"
                "for name in ('backend','mysql','redis','checkpoint_pg'):\n"
                "    try:\n"
                "        socket.gethostbyname(name)\n"
                "        print(name, 'RESOLVED')\n"
                "    except OSError:\n"
                "        print(name, 'BLOCKED')\n"
                "PY"
            ),
            "working_directory": "/workspace", "timeout_seconds": 40, "wait_seconds": 20,
        })
        for _ in range(30):
            if isolation.get("status") != "running":
                break
            await asyncio.sleep(1)
            isolation = await _agent(
                ip, token, "GET", f"/exec/{isolation['operation_id']}")
        assert isolation["status"] == "succeeded", isolation
        assert "RESOLVED" not in isolation["stdout"], isolation["stdout"]
        assert isolation["stdout"].count("BLOCKED") >= 3

        # ── Service：启动 + 健康检查 + HTTP 访问 + 停止 ─────────────────
        service = await _agent(ip, token, "POST", "/services", json={
            "name": "web",
            "command": "python3 -m http.server 8123 --bind 0.0.0.0",
            "working_directory": "/workspace",
            "port": 8123,
            "health_check_type": "tcp",
            "health_check_timeout": 25,
            "auto_restart": False,
        })
        assert service["name"] == "web"
        healthy = False
        for _ in range(25):
            status = await _agent(ip, token, "GET", "/services/web")
            if status["observed_state"] == "healthy":
                healthy = True
                break
            await asyncio.sleep(1)
        assert healthy, status
        async with httpx.AsyncClient(timeout=10) as client:
            page = await client.get(f"http://{ip}:8123/")
            assert page.status_code == 200
        stopped = await _agent(ip, token, "POST", "/services/web/stop")
        assert stopped["observed_state"] == "stopped"

        # ── 容器删除（保留 Volume）→ 新 Generation 文件仍在 ─────────────
        delete_result = await executor.execute(
            {"job_type": "delete", "payload": {**payload1, "purge_volume": False}}, on_state)
        assert delete_result["volume_retained"] is True
        container_ids.clear()  # 容器已删除

        result2 = await executor.execute({"job_type": "provision", "payload": payload2}, on_state)
        container_ids.append(result2["container_id"])
        ip2 = result2["container_ip"]
        token2 = agent_token(sandbox_id, 2)
        assert result2["container_id"] != result["container_id"]
        restored = await _agent(ip2, token2, "POST", "/exec", json={
            "command": "cat demo.txt", "working_directory": "/workspace",
            "timeout_seconds": 30, "wait_seconds": 10,
        })
        assert "persisted-data" in restored["stdout"], "新 Generation 必须挂载同一 Volume"
    finally:
        for container_id in container_ids:
            try:
                await engine.stop_container(container_id, timeout=3)
                await engine.remove_container(container_id, force=True)
            except Exception:  # noqa: BLE001
                pass
        for generation in (1, 2):
            try:
                await engine.remove_network(
                    f"gap-net-{_payload(suffix, generation, volume)['sandbox_id']}-g{generation}")
            except Exception:  # noqa: BLE001
                pass
        try:
            await engine.remove_volume(volume)
        except Exception:  # noqa: BLE001
            pass
        await engine.close()
