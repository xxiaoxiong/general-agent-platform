"""ModelClientFactory（P2-03 §4.4）：统一模型 HTTP 客户端生命周期与连接复用。

- OpenAI 兼容客户端（AsyncOpenAI）按 (base_url, api_key, timeout) 键缓存复用，
  修复 ModelService 每次调用新建 httpx.AsyncClient 的连接泄漏；
- httpx 客户端按 TLS 配置（verify / ca_bundle）缓存，供 ChatOpenAI 复用；
- 缓存的客户端是无状态连接池——不绑定 Run Context / DB Session / EventBus
  闭包（Graph 缓存禁令不适用于此处）；
- 进程退出调用 ``close_all`` 释放连接。
"""
from __future__ import annotations

import hashlib
import logging
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger("ai_plat.assistant.model_clients")


def _key(*parts: Any) -> str:
    return hashlib.sha256("|".join(str(p) for p in parts).encode("utf-8")).hexdigest()


class ModelClientFactory:
    """OpenAI 兼容客户端 / httpx 连接的统一缓存工厂。"""

    _openai_clients: Dict[str, Any] = {}
    _httpx_clients: Dict[str, Any] = {}

    @classmethod
    def get_openai_client(
        cls,
        *,
        base_url: str,
        api_key: Optional[str] = None,
        timeout: float = 120.0,
        verify: bool = True,
        ca_bundle: Optional[str] = None,
    ) -> Any:
        from openai import AsyncOpenAI

        key = _key(base_url, api_key, timeout, verify, ca_bundle)
        client = cls._openai_clients.get(key)
        if client is None:
            client = AsyncOpenAI(
                api_key=api_key or "EMPTY",
                base_url=base_url,
                timeout=timeout,
                http_client=cls.get_httpx_client(verify=verify, ca_bundle=ca_bundle, timeout=timeout),
            )
            cls._openai_clients[key] = client
        return client

    @classmethod
    def get_httpx_client(
        cls,
        *,
        verify: bool = True,
        ca_bundle: Optional[str] = None,
        timeout: float = 120.0,
    ) -> Any:
        import httpx

        key = _key(verify, ca_bundle, timeout)
        client = cls._httpx_clients.get(key)
        if client is None:
            if not verify:
                client = httpx.AsyncClient(verify=False, timeout=timeout)
            elif ca_bundle:
                client = httpx.AsyncClient(verify=ca_bundle, timeout=timeout)
            else:
                client = httpx.AsyncClient(verify=True, timeout=timeout)
            cls._httpx_clients[key] = client
        return client

    @classmethod
    async def close_all(cls) -> None:
        for client in list(cls._openai_clients.values()):
            try:
                await client.close()
            except Exception as e:  # noqa: BLE001
                logger.warning("关闭 OpenAI 客户端失败（忽略）: %s", e)
        for client in list(cls._httpx_clients.values()):
            try:
                await client.aclose()
            except Exception as e:  # noqa: BLE001
                logger.warning("关闭 httpx 客户端失败（忽略）: %s", e)
        cls._openai_clients.clear()
        cls._httpx_clients.clear()

    @classmethod
    def reset_for_tests(cls) -> None:
        cls._openai_clients.clear()
        cls._httpx_clients.clear()
