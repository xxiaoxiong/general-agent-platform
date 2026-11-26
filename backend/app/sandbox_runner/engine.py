"""Docker Engine API 客户端（只被 Sandbox Runner 使用）。

- 通过 unix socket（默认 /var/run/docker.sock）访问 Docker Engine；
- 不依赖 docker-py，HostConfig 全部由服务端策略生成；
- 所有资源对象带平台标签，禁止仅凭名称识别归属。
"""
from __future__ import annotations

import asyncio
import json
from typing import Any, Optional
from urllib.parse import quote

import httpx

from app.core.config import settings


class DockerEngineError(Exception):
    def __init__(self, status_code: int, message: str):
        super().__init__(f"Docker API {status_code}: {message}")
        self.status_code = status_code
        self.message = message


class DockerEngine:
    def __init__(self, socket_path: Optional[str] = None) -> None:
        self.socket_path = socket_path or settings.SANDBOX_DOCKER_SOCKET
        self._client: Optional[httpx.AsyncClient] = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            transport = httpx.AsyncHTTPTransport(uds=self.socket_path, retries=0)
            self._client = httpx.AsyncClient(
                transport=transport, base_url="http://docker", timeout=60.0,
            )
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _request(
        self, method: str, path: str, *, json_body: Optional[dict] = None,
        params: Optional[dict] = None, timeout: Optional[float] = None,
    ) -> httpx.Response:
        client = await self._get_client()
        try:
            response = await client.request(
                method, path, json=json_body, params=params,
                timeout=timeout if timeout is not None else 60.0,
            )
        except httpx.HTTPError as exc:
            raise DockerEngineError(0, f"Docker Engine 不可达（{self.socket_path}）：{exc}") from exc
        if response.status_code >= 400:
            try:
                message = response.json().get("message") or response.text
            except Exception:  # noqa: BLE001
                message = response.text
            raise DockerEngineError(response.status_code, str(message)[:1000])
        return response

    async def ping(self) -> bool:
        try:
            await self._request("GET", "/_ping", timeout=5)
            return True
        except DockerEngineError:
            return False

    async def version(self) -> dict:
        return (await self._request("GET", "/version")).json()

    async def info(self) -> dict:
        return (await self._request("GET", "/info")).json()

    # ── Images ─────────────────────────────────────────────────────────

    def _split_image(self, image: str) -> tuple[str, str]:
        if "@" in image:
            return image, ""
        head, _, tail = image.rpartition(":")
        if head and "/" not in tail:
            return head, tail
        return image, "latest"

    async def image_digest(self, image: str) -> Optional[str]:
        # Docker API：GET /images/{name}/json，name 需包含 tag（不支持 tag 查询参数）
        try:
            data = (await self._request(
                "GET", f"/images/{quote(image, safe='/')}/json",
            )).json()
            return data.get("Id")
        except DockerEngineError as exc:
            if exc.status_code == 404:
                return None
            raise

    async def ensure_image(self, image: str) -> str:
        digest = await self.image_digest(image)
        if digest:
            return digest
        name, tag = self._split_image(image)
        params = {"fromImage": name}
        if tag:
            params["tag"] = tag
        client = await self._get_client()
        async with client.stream("POST", "/images/create", params=params, timeout=900.0) as response:
            if response.status_code >= 400:
                body = await response.aread()
                raise DockerEngineError(response.status_code, body.decode("utf-8", "replace")[:1000])
            async for _chunk in response.aiter_bytes():
                pass
        digest = await self.image_digest(image)
        if not digest:
            raise DockerEngineError(500, f"镜像拉取后仍不可用: {image}")
        return digest

    # ── Volumes ────────────────────────────────────────────────────────

    async def ensure_volume(self, name: str, labels: dict) -> dict:
        try:
            return (await self._request(
                "GET", f"/volumes/{quote(name, safe='')}")).json()
        except DockerEngineError as exc:
            if exc.status_code != 404:
                raise
        return (await self._request(
            "POST", "/volumes/create",
            json_body={"Name": name, "Driver": "local", "Labels": labels},
        )).json()

    async def remove_volume(self, name: str) -> None:
        try:
            await self._request("DELETE", f"/volumes/{quote(name, safe='')}")
        except DockerEngineError as exc:
            if exc.status_code not in (404, 409):
                raise

    # ── Networks ───────────────────────────────────────────────────────

    async def ensure_network(self, name: str, *, internal: bool, labels: dict) -> dict:
        try:
            return (await self._request(
                "GET", f"/networks/{quote(name, safe='')}")).json()
        except DockerEngineError as exc:
            if exc.status_code != 404:
                raise
        return (await self._request(
            "POST", "/networks/create",
            json_body={
                "Name": name,
                "Driver": "bridge",
                "Internal": bool(internal),
                "CheckDuplicate": True,
                "Labels": labels,
            },
        )).json()

    async def connect_network(self, network: str, container: str) -> None:
        try:
            await self._request(
                "POST", f"/networks/{quote(network, safe='')}/connect",
                json_body={"Container": container},
            )
        except DockerEngineError as exc:
            if exc.status_code in (403, 409):
                return
            raise

    async def disconnect_network(self, network: str, container: str) -> None:
        try:
            await self._request(
                "POST", f"/networks/{quote(network, safe='')}/disconnect",
                json_body={"Container": container, "Force": True},
            )
        except DockerEngineError as exc:
            if exc.status_code in (403, 404, 409):
                return
            raise

    async def remove_network(self, name: str) -> None:
        try:
            await self._request("DELETE", f"/networks/{quote(name, safe='')}")
        except DockerEngineError as exc:
            if exc.status_code not in (404, 409):
                raise

    # ── Containers ─────────────────────────────────────────────────────

    async def create_container(self, name: str, spec: dict) -> tuple[str, bool]:
        """幂等创建：409 时返回已存在容器并标记 adopted。"""
        try:
            data = (await self._request(
                "POST", "/containers/create", params={"name": name}, json_body=spec,
            )).json()
            return data["Id"], False
        except DockerEngineError as exc:
            if exc.status_code != 409:
                raise
        info = await self.inspect_container(name)
        return info["Id"], True

    async def inspect_container(self, container: str) -> dict:
        return (await self._request(
            "GET", f"/containers/{quote(container, safe='')}/json")).json()

    async def start_container(self, container: str) -> None:
        try:
            await self._request("POST", f"/containers/{quote(container, safe='')}/start")
        except DockerEngineError as exc:
            if exc.status_code != 304:  # 304 = already started
                raise

    async def stop_container(self, container: str, timeout: int = 10) -> None:
        try:
            await self._request(
                "POST", f"/containers/{quote(container, safe='')}/stop",
                params={"t": int(timeout)}, timeout=timeout + 30,
            )
        except DockerEngineError as exc:
            if exc.status_code != 304:  # 304 = already stopped
                raise

    async def remove_container(self, container: str, *, force: bool = True) -> None:
        try:
            await self._request(
                "DELETE", f"/containers/{quote(container, safe='')}",
                params={"force": "1" if force else "0"},
            )
        except DockerEngineError as exc:
            if exc.status_code != 404:
                raise

    async def list_containers(self, labels: dict) -> list[dict]:
        filters = {"label": [f"{k}={v}" for k, v in labels.items()]}
        return (await self._request(
            "GET", "/containers/json",
            params={"all": "1", "filters": json.dumps(filters)},
        )).json()

    async def container_ip(self, container: str, network: str) -> Optional[str]:
        info = await self.inspect_container(container)
        networks = ((info.get("NetworkSettings") or {}).get("Networks") or {})
        entry = networks.get(network) or next(iter(networks.values()), {}) if networks else {}
        return (entry or {}).get("IPAddress") or None

    async def container_state(self, container: str) -> dict:
        info = await self.inspect_container(container)
        state = info.get("State") or {}
        status = state.get("Status") or "unknown"
        observed = {
            "created": "stopped",
            "running": "ready",
            "paused": "stopped",
            "restarting": "starting",
            "removing": "stopping",
            "exited": "stopped",
            "dead": "failed",
        }.get(status, "failed")
        return {
            "container_id": info.get("Id"),
            "container_name": (info.get("Name") or "").lstrip("/"),
            "status": status,
            "observed_state": observed,
            "exit_code": state.get("ExitCode"),
            "oom_killed": bool(state.get("OOMKilled")),
            "image": ((info.get("Config") or {}).get("Image")),
            "labels": (info.get("Config") or {}).get("Labels") or {},
        }

    async def stats_one_shot(self, container: str) -> dict:
        return (await self._request(
            "GET", f"/containers/{quote(container, safe='')}/stats",
            params={"stream": "false"}, timeout=20,
        )).json()

    async def logs(self, container: str, *, tail: int = 200) -> bytes:
        response = await self._request(
            "GET", f"/containers/{quote(container, safe='')}/logs",
            params={"stdout": "1", "stderr": "1", "tail": str(int(tail))},
            timeout=30,
        )
        return response.content


async def wait_agent_ready(
    sandbox_id: int, ip: Optional[str], port: int, token: str,
    timeout_seconds: float = 90.0,
) -> bool:
    """等待 workspace-agent /health 就绪（Sandbox 内网直连）。"""
    if not ip:
        return False
    import httpx as _httpx

    deadline = asyncio.get_event_loop().time() + timeout_seconds
    while asyncio.get_event_loop().time() < deadline:
        try:
            async with _httpx.AsyncClient(timeout=5.0) as client:
                response = await client.get(
                    f"http://{ip}:{port}/health",
                    headers={"Authorization": f"Bearer {token}"},
                )
                if response.status_code == 200:
                    return True
        except _httpx.HTTPError:
            pass
        await asyncio.sleep(1.5)
    return False
