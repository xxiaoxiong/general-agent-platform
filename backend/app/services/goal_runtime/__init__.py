"""Goal Runtime：目标契约、目标监督循环与进度（任务书 Phase 1/2）。"""
from .contracts import (
    blocking_criteria,
    build_criteria,
    contract_prompt,
    contract_to_dict,
    ensure_goal_contract,
    evaluate_criteria,
    load_goal_contract,
    progress_of,
    record_steering_revision,
    revise_goal_contract,
)
from .supervisor import (
    ACTION_BLOCKED,
    ACTION_COMPLETE,
    ACTION_CONTINUE,
    ACTION_PAUSED,
    GoalDecision,
    complete_goal_contract,
    mark_goal_status,
    supervise_goal,
)

__all__ = [
    "ACTION_BLOCKED", "ACTION_COMPLETE", "ACTION_CONTINUE", "ACTION_PAUSED",
    "GoalDecision", "blocking_criteria", "build_criteria", "contract_prompt",
    "contract_to_dict", "ensure_goal_contract", "evaluate_criteria",
    "load_goal_contract", "mark_goal_status", "progress_of",
    "record_steering_revision", "revise_goal_contract", "supervise_goal",
    "complete_goal_contract",
]
