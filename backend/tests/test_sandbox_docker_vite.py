"""真实 Vite HMR WebSocket 集成测试（需 Docker + 网络可安装 npm 依赖）。

运行（Runner 容器内，需显式开启）：
    docker compose -f docker/docker-compose.yml run --rm \
      -e SANDBOX_DOCKER_VITE=1 \
      -e DATABASE_URL=sqlite+aiosqlite:////tmp/sandbox_vite.db \
      -e DATABASE_SYNC_URL=sqlite:////tmp/sandbox_vite.db \
      sandbox-runner python -m pytest tests/test_sandbox_docker_vite.py -q -s

验证：Sandbox 内 npm 安装依赖 → Vite dev server → WebSocket（vite-hmr）→
修改源码触发 full-reload/update 消息。
"""
from __future__ import annotations

import asyncio
import os
import re
import uuid

import httpx
import pytest
import websockets

from app.core.config import settings
from app.sandbox_runner.engine import DockerEngine
from app.sandbox_runner.executor import RunnerExecutor
from app.services.sandbox.runtime import agent_token

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get("SANDBOX_DOCKER_VITE") != "1",
        reason="设置 SANDBOX_DOCKER_VITE=1 才运行 Vite HMR 集成测试",
    ),
]

_AGENT_PORT = int(settings.SANDBOX_AGENT_PORT)
_NPM_REGISTRY = os.environ.get("SANDBOX_NPM_REGISTRY", "https://registry.npmmirror.com")


def _payload(volume: str, sandbox_id: int) -> dict:
    return {
        "workspace_id": 991001,
        "project_id": 991001,
        "sandbox_id": sandbox_id,
        "generation": 1,
        "volume_id": volume,
        "image": settings.SANDBOX_IMAGE,
        "network_policy": "egress",
        "profile": {
            "cpu_request_millis": 500,
            "cpu_limit_millis": 1000,
            "memory_request_bytes": 512 * 1024 * 1024,
            "memory_limit_bytes": 1024 * 1024 * 1024,
            "disk_quota_bytes": 2 * 1024 * 1024 * 1024,
            "pid_limit": 256,
        },
        "auto_services": [],
    }


async def _agent(ip: str, token: str, method: str, path: str, **kwargs) -> dict:
    async with httpx.AsyncClient(timeout=120) as client:
        response = await client.request(
            method, f"http://{ip}:{_AGENT_PORT}{path}",
            headers={"Authorization": f"Bearer {token}"}, **kwargs,
        )
        response.raise_for_status()
        return response.json()


async def _exec(ip: str, token: str, command: str, timeout: int = 900) -> dict:
    data = await _agent(ip, token, "POST", "/exec", json={
        "command": command, "working_directory": "/workspace",
        "timeout_seconds": timeout, "wait_seconds": 60,
    })
    op_id = data["operation_id"]
    deadline = asyncio.get_event_loop().time() + timeout + 120
    while data["status"] == "running" and asyncio.get_event_loop().time() < deadline:
        await asyncio.sleep(2)
        data = await _agent(ip, token, "GET", f"/exec/{op_id}")
    assert data["status"] == "succeeded", data
    return data


async def test_vite_hmr_websocket_inside_sandbox():
    engine = DockerEngine()
    if not await engine.ping():
        pytest.skip("Docker Socket 不可用（需在挂载 /var/run/docker.sock 的 Runner 容器内运行）")
    executor = RunnerExecutor(engine)
    suffix = uuid.uuid4().hex[:8]
    sandbox_id = int(f"991{suffix[:5]}", 16) % 900000 + 100000
    volume = f"gap-ws-vite-{suffix}"
    payload = _payload(volume, sandbox_id)
    token = agent_token(sandbox_id, 1)
    container_id = None
    try:
        result = await executor.execute(
            {"job_type": "provision", "payload": payload}, lambda _state: asyncio.sleep(0))
        container_id = result["container_id"]
        ip = result["container_ip"]
        assert ip

        env = f"export NPM_CONFIG_REGISTRY={_NPM_REGISTRY} && "
        await _exec(
            ip, token,
            env + "cd /workspace && npm create --yes vite@latest app -- --template vanilla",
        )
        await _exec(
            ip, token,
            env + "cd /workspace/app && npm install --no-audit --no-fund",
        )
        start_at = asyncio.get_event_loop().time()
        service = await _agent(ip, token, "POST", "/services", json={
            "name": "vite",
            "command": (
                "export CHOKIDAR_USEPOLLING=1 "
                f"NPM_CONFIG_REGISTRY={_NPM_REGISTRY} && "
                "npm run dev -- --host 0.0.0.0 --port 5173"
            ),
            "working_directory": "/workspace/app",
            "port": 5173,
            "health_check_type": "http",
            "health_check_path": "/",
            "health_check_timeout": 120,
            "auto_restart": False,
        })
        assert service["name"] == "vite"
        healthy = False
        for _ in range(60):
            status = await _agent(ip, token, "GET", "/services/vite")
            if status["observed_state"] == "healthy":
                healthy = True
                break
            if status["observed_state"] in ("failed", "exited"):
                break
            await asyncio.sleep(2)
        assert healthy, status
        healthy_seconds = asyncio.get_event_loop().time() - start_at

        async with httpx.AsyncClient(timeout=20) as client:
            page = await client.get(f"http://{ip}:5173/")
            assert page.status_code == 200 and "vite" in page.text.lower()
            print(f"[vite-bench] start_to_healthy={healthy_seconds:.3f}s "
                  f"first_page_seconds={asyncio.get_event_loop().time() - start_at:.3f}s")
            client_js = await client.get(f"http://{ip}:5173/@vite/client")
            match = re.search(r'const wsToken = "([^"]+)"', client_js.text)
            assert match, "Vite client 未找到 wsToken"

        ws_url = f"ws://{ip}:5173/?token={match.group(1)}"
        async with websockets.connect(
            ws_url, subprotocols=["vite-hmr"], open_timeout=30,
            ping_interval=None, max_size=None,
        ) as ws:
            connected = await asyncio.wait_for(ws.recv(), timeout=30)
            assert "connected" in connected
            current = await _agent(ip, token, "POST", "/files/read", json={
                "path": "/workspace/app/src/main.js", "offset": 0, "limit": 5000,
            })
            original = current["content"]
            await _agent(ip, token, "POST", "/files/write", json={
                "path": "/workspace/app/src/main.js",
                "content": original + "\nconsole.log('HMR-PROBE-20261127');\n",
            })
            update_seen = False
            for _ in range(20):
                message = await asyncio.wait_for(ws.recv(), timeout=15)
                if "update" in message or "full-reload" in message:
                    update_seen = True
                    break
            assert update_seen, "未收到 Vite HMR update/full-reload 消息"
            await _agent(ip, token, "POST", "/files/write", json={
                "path": "/workspace/app/src/main.js", "content": original,
            })
    finally:
        if container_id:
            try:
                await engine.stop_container(container_id, timeout=5)
                await engine.remove_container(container_id, force=True)
            except Exception:  # noqa: BLE001
                pass
        try:
            await engine.remove_network(f"gap-net-{sandbox_id}-g1")
        except Exception:  # noqa: BLE001
            pass
        try:
            await engine.remove_volume(volume)
        except Exception:  # noqa: BLE001
            pass
        await engine.close()
