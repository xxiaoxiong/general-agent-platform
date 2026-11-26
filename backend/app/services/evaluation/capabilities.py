"""Agent 能力维度（任务书 §4.5）：任务测量什么，与复杂度（任务多难）正交。"""

CAPABILITY_DIMENSIONS = (
    "task_understanding", "planning_reasoning", "tool_use", "multi_tool",
    "context_memory", "error_recovery", "delivery", "reliability_safety",
)

CAPABILITY_TEXT = {
    "task_understanding": "任务理解",
    "planning_reasoning": "规划与推理",
    "tool_use": "工具调用",
    "multi_tool": "多工具协作",
    "context_memory": "上下文与记忆",
    "error_recovery": "异常恢复",
    "delivery": "交付能力",
    "reliability_safety": "可靠性与安全",
}

# task_type → 默认能力维度（仅作缺省归档；用例可用 capability 字段显式覆盖）
_TASK_TYPE_CAPABILITY = {
    "direct_qa": "task_understanding",
    "multi_step_reasoning": "planning_reasoning",
    "complex_math": "planning_reasoning",
    "tool_use": "tool_use",
    "multi_turn": "context_memory",
    "instruction_following": "reliability_safety",
    "adversarial": "reliability_safety",
    "project_build": "delivery",
    "long_document": "delivery",
    "deep_research": "multi_tool",
}


def resolve_capability(explicit: str | None, task_type: str | None) -> str:
    value = (explicit or "").strip()
    if value in CAPABILITY_DIMENSIONS:
        return value
    return _TASK_TYPE_CAPABILITY.get((task_type or "direct_qa").strip(), "task_understanding")
