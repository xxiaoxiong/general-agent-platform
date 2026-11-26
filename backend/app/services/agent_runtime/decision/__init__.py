"""Decision Architecture：语义决策与执行控制分层。

- 模型负责语义判断（意图 / 路线 / 连续性 / 目标 / 交付物 / 验收标准）；
- 服务端负责白名单、资源归属、能力授权与安全降级；
- Runtime 只消费校验后的决策快照，不直接信任模型给出的执行对象。
"""
from .schemas import RunDecision
from .validator import ValidatedDecision, validate_decision
from .apply import apply_decision

__all__ = ["RunDecision", "ValidatedDecision", "validate_decision", "apply_decision"]
