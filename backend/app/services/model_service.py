from typing import AsyncGenerator, Dict, Any, Tuple
from openai import AsyncOpenAI
import httpx

from app.core.config import settings
from app.models.model_provider import ModelProvider, ModelConfig
from app.schemas.model_provider import ChatRequest


class ModelService:

    @staticmethod
    def _get_client(provider: ModelProvider) -> AsyncOpenAI:
        # P2-03: ModelClientFactory 统一客户端生命周期（连接复用，
        # 修复每次调用新建 httpx.AsyncClient 的连接泄漏）
        from app.assistant.model_clients import ModelClientFactory

        verify = bool(settings.AGENT_CA_BUNDLE or settings.AGENT_TLS_VERIFY)
        return ModelClientFactory.get_openai_client(
            base_url=provider.base_url,
            api_key=provider.api_key or "ollama",
            timeout=120,
            verify=verify,
            ca_bundle=settings.AGENT_CA_BUNDLE or None,
        )

    @staticmethod
    async def test_provider(provider: ModelProvider) -> Tuple[bool, str]:
        try:
            client = ModelService._get_client(provider)
            models = await client.models.list()
            return True, f"连接成功，共 {len(list(models))} 个模型"
        except Exception as e:
            return False, f"连接失败: {str(e)}"

    # ── P0-06 模型能力真实探测 ─────────────────────────────────────────────
    # bind_tools() 成功只证明 SDK 能绑定工具；必须向模型服务端发一次携带
    # 无副作用工具的真实请求，验证返回体真的包含原生 tool_calls 结构。

    PROBE_TOOL_NAME = "get_probe_constant"
    PROBE_TOOL_SCHEMA = {
        "type": "function",
        "function": {
            "name": PROBE_TOOL_NAME,
            "description": "探针工具：返回一个固定常量，无任何副作用",
            "parameters": {
                "type": "object",
                "properties": {"constant": {"type": "string", "description": "要回显的常量"}},
                "required": ["constant"],
            },
        },
    }
    PROBE_MESSAGES = [
        {"role": "system", "content": "你是能力探测助手。必须调用工具完成用户要求，不要直接回答。"},
        {"role": "user", "content": "请调用 get_probe_constant 工具，constant 参数为 PING-1234。"},
    ]

    @staticmethod
    def _classify_tool_call_response(response) -> Tuple[bool, str]:
        """判定服务端是否真的返回了原生工具调用。"""
        try:
            msg = response.choices[0].message
        except (AttributeError, IndexError, TypeError):
            return False, "响应结构异常：缺少 choices[0].message"
        tool_calls = getattr(msg, "tool_calls", None) or []
        names = []
        for tc in tool_calls:
            fn = getattr(tc, "function", None)
            names.append(getattr(fn, "name", None) or (tc.get("name") if isinstance(tc, dict) else None))
        if ModelService.PROBE_TOOL_NAME in names:
            return True, "服务端返回原生 tool_calls（探测通过）"
        if tool_calls:
            return False, f"返回了非预期工具调用: {names}"
        content = (getattr(msg, "content", "") or "")[:120]
        return False, f"模型只返回文本、未返回 tool_calls: {content!r}"

    @staticmethod
    async def probe_tool_calling(provider: ModelProvider, model_config: ModelConfig) -> Dict[str, Any]:
        """真实 Tool Calling 探测：单轮请求 + 无副作用探针工具。

        返回 {"tool_calling": bool, "message": str, "latency_ms": float}。
        超时/连接错误归类为探测失败（error 语义），不吞异常细节到服务端日志。
        """
        import time

        client = ModelService._get_client(provider)
        started = time.monotonic()
        try:
            response = await client.chat.completions.create(
                model=model_config.name,
                messages=list(ModelService.PROBE_MESSAGES),
                tools=[ModelService.PROBE_TOOL_SCHEMA],
                tool_choice="auto",
                temperature=0,
                max_tokens=64,
                stream=False,
            )
        except Exception as e:
            return {
                "tool_calling": False,
                "message": f"探测请求失败: {type(e).__name__}: {str(e)[:200]}",
                "latency_ms": (time.monotonic() - started) * 1000,
            }
        latency = (time.monotonic() - started) * 1000
        ok, msg = ModelService._classify_tool_call_response(response)
        return {"tool_calling": ok, "message": msg, "latency_ms": latency}

    @staticmethod
    async def probe_streaming(provider: ModelProvider, model_config: ModelConfig) -> Dict[str, Any]:
        """流式能力探测：真实 stream 请求，至少收到一个内容增量视为可用。"""
        import time

        client = ModelService._get_client(provider)
        started = time.monotonic()
        chunks = 0
        try:
            stream = await client.chat.completions.create(
                model=model_config.name,
                messages=[{"role": "user", "content": "只回复 OK 两个字母。"}],
                temperature=0,
                max_tokens=8,
                stream=True,
            )
            async for chunk in stream:
                if chunk.choices and getattr(chunk.choices[0], "delta", None) is not None:
                    chunks += 1
        except Exception as e:
            return {
                "streaming": False,
                "message": f"流式探测失败: {type(e).__name__}: {str(e)[:200]}",
                "latency_ms": (time.monotonic() - started) * 1000,
            }
        latency = (time.monotonic() - started) * 1000
        if chunks:
            return {"streaming": True, "message": f"收到 {chunks} 个增量", "latency_ms": latency}
        return {"streaming": False, "message": "流式请求无任何增量", "latency_ms": latency}

    @staticmethod
    async def test_model_config(provider: ModelProvider, model_config: ModelConfig) -> Dict[str, Any]:
        """模型配置综合能力测试（P0-06）：Tool Calling + Streaming 真实探测。"""
        tool = await ModelService.probe_tool_calling(provider, model_config)
        streaming = await ModelService.probe_streaming(provider, model_config)
        return {
            "model": model_config.name,
            "tool_calling": tool["tool_calling"],
            "tool_calling_message": tool["message"],
            "tool_calling_latency_ms": tool["latency_ms"],
            "streaming": streaming["streaming"],
            "streaming_message": streaming["message"],
            "streaming_latency_ms": streaming["latency_ms"],
            "passed": bool(tool["tool_calling"]),
        }

    @staticmethod
    def _build_messages(request: "ChatRequest") -> list:
        """Build OpenAI-compatible messages, supporting multimodal image_urls."""
        messages = []
        for m in request.messages:
            image_urls = getattr(m, "image_urls", None) or []
            if image_urls:
                content = [{"type": "text", "text": m.content}]
                for url in image_urls:
                    content.append({"type": "image_url", "image_url": {"url": url}})
                messages.append({"role": m.role, "content": content})
            else:
                messages.append({"role": m.role, "content": m.content})
        if request.system_prompt:
            messages.insert(0, {"role": "system", "content": request.system_prompt})
        return messages

    @staticmethod
    async def chat_stream(
        provider: ModelProvider,
        model_config: ModelConfig,
        request: "ChatRequest",
    ) -> AsyncGenerator[Dict[str, Any], None]:
        client = ModelService._get_client(provider)
        messages = ModelService._build_messages(request)

        params = {
            "model": model_config.name,
            "messages": messages,
            "temperature": request.temperature,
            "stream": True,
        }
        if request.max_tokens:
            params["max_tokens"] = request.max_tokens

        try:
            stream = await client.chat.completions.create(**params)
            async for chunk in stream:
                delta = chunk.choices[0].delta if chunk.choices else None
                if delta and delta.content:
                    yield {"type": "content", "content": delta.content}
            yield {"type": "done"}
        except Exception as e:
            yield {"type": "error", "content": str(e)}

    @staticmethod
    async def chat(
        provider: ModelProvider,
        model_config: ModelConfig,
        request: "ChatRequest",
    ) -> Dict[str, Any]:
        client = ModelService._get_client(provider)
        messages = ModelService._build_messages(request)

        params = {
            "model": model_config.name,
            "messages": messages,
            "temperature": request.temperature,
            "stream": False,
        }
        if request.max_tokens:
            params["max_tokens"] = request.max_tokens

        response = await client.chat.completions.create(**params)
        return {
            "content": response.choices[0].message.content,
            "model": response.model,
            "usage": {
                "prompt_tokens": response.usage.prompt_tokens,
                "completion_tokens": response.usage.completion_tokens,
                "total_tokens": response.usage.total_tokens,
            } if response.usage else None,
        }

    @staticmethod
    async def get_embeddings(
        provider: ModelProvider,
        model_name: str,
        texts: list[str],
    ) -> list[list[float]]:
        client = ModelService._get_client(provider)
        response = await client.embeddings.create(model=model_name, input=texts)
        return [item.embedding for item in response.data]
