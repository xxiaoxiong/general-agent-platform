"""自进化闭环服务包。

- langfuse_api：Langfuse Public API 读写（Trace/Observation 拉取、Score 回写）
- metrics：确定性运行指标（成功率/效率/工具/健壮性/HITL）
- judge：LLM-as-judge 维度评分
- evaluator：周期评估循环（Run → AgentEvaluation）
- evolution_advisor：优化方向报告 + 建议应用/回滚/验证闭环
"""
