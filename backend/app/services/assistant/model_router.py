"""Model Router（P2-03 §4.4）：模型能力检查与选模。

- 能力矩阵：tool_calling / streaming / structured_output / vision /
  parallel_tool_calls / context_length / max_output_tokens / 验证状态；
- 不兼容模型明确报错（``ModelCapabilityError``），绝不静默切换到不支持
  Tool Calling 的模型；备用候选必须满足相同能力约束；
- 选模确定性：管理员绑定优先 → 默认模型策略 → 按 id 序取首个兼容模型。
本模块不做模型调用、不做客户端管理（客户端复用在 model_clients.ModelClientFactory）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set

from app.services.agent_runtime.model_adapter import ModelCapabilityError

# 能力名 → ModelConfig 字段（能力矩阵）
CAPABILITY_FLAGS: Dict[str, str] = {
    "tool_calling": "supports_function_call",
    "streaming": "supports_streaming",
    "structured_output": "supports_structured_output",
    "vision": "supports_vision",
    "parallel_tool_calls": "supports_parallel_tool_calls",
}


@dataclass(frozen=True)
class ModelCandidate:
    """选模候选（与 ORM 解耦的轻量视图）。"""

    id: int
    name: str
    provider_id: int
    is_default_provider: bool = False
    context_length: int = 4096
    max_tokens: int = 2048
    capability_status: str = "unverified"
    flags: Dict[str, bool] = field(default_factory=dict)

    def has_capability(self, capability: str) -> bool:
        if capability == "context_length":      # 数值能力由调用方按阈值判断
            return True
        return bool(self.flags.get(CAPABILITY_FLAGS.get(capability, capability), False))


def candidate_from_row(mc: Any, provider: Any = None) -> ModelCandidate:
    return ModelCandidate(
        id=mc.id,
        name=mc.name,
        provider_id=mc.provider_id,
        is_default_provider=bool(getattr(provider, "is_default", False)),
        context_length=getattr(mc, "context_length", 4096) or 4096,
        max_tokens=getattr(mc, "max_tokens", 2048) or 2048,
        capability_status=getattr(mc, "capability_status", "unverified") or "unverified",
        flags={
            field_name: bool(getattr(mc, field_name, False))
            for field_name in CAPABILITY_FLAGS.values()
        },
    )


def missing_capabilities(candidate: ModelCandidate, requirements: Iterable[str]) -> List[str]:
    return [r for r in requirements if not candidate.has_capability(r)]


def check_model_requirements(candidate: ModelCandidate, requirements: Iterable[str]) -> None:
    """不满足能力要求的模型明确报错（发布/运行前发现，不静默降级）。"""
    missing = missing_capabilities(candidate, requirements)
    if missing:
        raise ModelCapabilityError(
            f"模型 '{candidate.name}' 不满足能力要求: 缺少 {missing}；"
            "不支持 Tool Calling 的模型不得进入 Agent 执行路径，请更换模型或补齐能力配置"
        )


def select_model(
    candidates: Sequence[ModelCandidate],
    requirements: Iterable[str] = ("tool_calling",),
    *,
    preferred_model_config_id: Optional[int] = None,
    require_verified: bool = False,
) -> ModelCandidate:
    """选模（确定性）。

    - ``preferred_model_config_id``：管理员/Agent 绑定的模型，必须兼容，
      不兼容直接报错（禁止静默换模型）；
    - 否则：默认供应商优先 → 验证过的优先 → id 序取首个兼容候选；
    - ``require_verified``：只允许真实探测验证过的模型；
    - 无兼容候选：明确报错。
    """
    reqs = list(requirements)

    if preferred_model_config_id is not None:
        preferred = next((c for c in candidates if c.id == preferred_model_config_id), None)
        if preferred is None:
            raise ModelCapabilityError(f"指定的模型配置 {preferred_model_config_id} 不存在或不可用")
        check_model_requirements(preferred, reqs)
        if require_verified and preferred.capability_status != "verified":
            raise ModelCapabilityError(
                f"模型 '{preferred.name}' 尚未通过真实能力探测（capability_status="
                f"{preferred.capability_status}）"
            )
        return preferred

    pool = [c for c in candidates if not missing_capabilities(c, reqs)]
    if require_verified:
        verified = [c for c in pool if c.capability_status == "verified"]
        pool = verified
    if not pool:
        raise ModelCapabilityError(
            f"没有满足能力要求 {reqs} 的可用模型；"
            "请在模型管理中配置并探测至少一个支持 Tool Calling 的模型"
        )
    # 确定性排序：默认供应商 → 已验证 → id
    pool.sort(key=lambda c: (not c.is_default_provider, c.capability_status != "verified", c.id))
    return pool[0]
