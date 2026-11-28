"""成员模型路由：团队默认模型 + 经校验的角色级覆盖（不保存密钥）。"""
from __future__ import annotations

import logging
from typing import Any, Optional

from sqlalchemy import select

from app.core.database import AsyncSessionLocal
from app.models.model_provider import ModelConfig, ModelProvider
from app.services.agent_runtime.model_adapter import ModelCapabilityError

logger = logging.getLogger("ai_plat.team")


def model_snapshot(
    provider: dict[str, Any], model_config: dict[str, Any], model_config_id: Optional[int],
) -> dict[str, Any]:
    """可持久化的模型快照（绝不包含 API Key）。"""
    return {
        "model_config_id": model_config_id,
        "name": model_config.get("name"),
        "provider": provider.get("name"),
        "max_tokens": model_config.get("max_tokens"),
        "supports_function_call": bool(model_config.get("supports_function_call")),
        "supports_streaming": bool(model_config.get("supports_streaming")),
    }


async def load_model_route(model_config_id: int) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """加载并校验角色指定模型（必须启用、chat、支持工具调用）。"""
    async with AsyncSessionLocal() as db:
        row = (await db.execute(
            select(ModelConfig, ModelProvider)
            .join(ModelProvider, ModelProvider.id == ModelConfig.provider_id)
            .where(
                ModelConfig.id == model_config_id,
                ModelConfig.is_active == True,  # noqa: E712
                ModelConfig.model_type == "chat",
                ModelProvider.is_active == True,  # noqa: E712
            )
        )).first()
    if row is None:
        raise ModelCapabilityError(f"成员模型配置 {model_config_id} 不存在或已禁用")
    mc, provider = row
    if not mc.supports_function_call:
        raise ModelCapabilityError(
            f"成员模型 '{mc.name}' 未启用原生工具调用，不能用于团队执行"
        )
    provider_data = {
        "name": provider.name,
        "base_url": provider.base_url,
        "api_key": provider.api_key,
        "extra_config": provider.extra_config or {},
    }
    model_data = {
        "name": mc.name,
        "max_tokens": mc.max_tokens,
        "supports_streaming": mc.supports_streaming,
        "supports_vision": mc.supports_vision,
        "supports_function_call": mc.supports_function_call,
        "extra_params": mc.extra_params or {},
    }
    return provider_data, model_data, model_snapshot(provider_data, model_data, model_config_id)


def team_model_key(provider: dict[str, Any], model_config: dict[str, Any]) -> str:
    return f"{provider.get('name') or 'provider'}:{model_config.get('name') or 'model'}"
