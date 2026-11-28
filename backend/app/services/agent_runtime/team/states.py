"""团队成员状态机（任务书 §3.3）。

合法迁移（显式、单向为主，禁止隐式状态倒退）：
  created -> ready | cancelled
  ready -> running | cancelled | failed
  running -> waiting | completed | failed | interrupted | cancelled
  waiting -> running | cancelled | failed
  interrupted -> running | cancelled
  failed -> ready（仅显式重试）
  cancelled -> ready（仅显式重试）
  completed -> （终态）
"""
from __future__ import annotations

MEMBER_CREATED = "created"
MEMBER_READY = "ready"
MEMBER_RUNNING = "running"
MEMBER_WAITING = "waiting"
MEMBER_COMPLETED = "completed"
MEMBER_FAILED = "failed"
MEMBER_INTERRUPTED = "interrupted"
MEMBER_CANCELLED = "cancelled"

MEMBER_TERMINAL = frozenset({MEMBER_COMPLETED, MEMBER_FAILED, MEMBER_CANCELLED})
MEMBER_ACTIVE = frozenset({MEMBER_READY, MEMBER_RUNNING, MEMBER_WAITING})

ALLOWED_MEMBER_TRANSITIONS: dict[str, frozenset[str]] = {
    MEMBER_CREATED: frozenset({MEMBER_READY, MEMBER_CANCELLED}),
    MEMBER_READY: frozenset({MEMBER_RUNNING, MEMBER_CANCELLED, MEMBER_FAILED}),
    MEMBER_RUNNING: frozenset({
        MEMBER_WAITING, MEMBER_COMPLETED, MEMBER_FAILED,
        MEMBER_INTERRUPTED, MEMBER_CANCELLED,
    }),
    MEMBER_WAITING: frozenset({MEMBER_RUNNING, MEMBER_CANCELLED, MEMBER_FAILED}),
    MEMBER_INTERRUPTED: frozenset({MEMBER_RUNNING, MEMBER_CANCELLED}),
    MEMBER_FAILED: frozenset({MEMBER_READY}),
    MEMBER_CANCELLED: frozenset({MEMBER_READY}),
    MEMBER_COMPLETED: frozenset(),
}


def can_transition(current: str, target: str) -> bool:
    return target in ALLOWED_MEMBER_TRANSITIONS.get(current, frozenset())


# ── 任务状态机（任务书 §4.1）────────────────────────────────────────────────

TASK_PENDING = "pending"
TASK_READY = "ready"
TASK_CLAIMED = "claimed"
TASK_RUNNING = "running"
TASK_BLOCKED = "blocked"
TASK_COMPLETED = "completed"
TASK_FAILED = "failed"
TASK_CANCELLED = "cancelled"
TASK_SKIPPED = "skipped"

TASK_TERMINAL = frozenset({TASK_COMPLETED, TASK_FAILED, TASK_CANCELLED, TASK_SKIPPED})
TASK_ACTIVE = frozenset({TASK_PENDING, TASK_READY, TASK_CLAIMED, TASK_RUNNING, TASK_BLOCKED})

ALLOWED_TASK_TRANSITIONS: dict[str, frozenset[str]] = {
    TASK_PENDING: frozenset({
        TASK_READY, TASK_CLAIMED, TASK_BLOCKED, TASK_CANCELLED, TASK_SKIPPED,
    }),
    TASK_READY: frozenset({
        TASK_CLAIMED, TASK_BLOCKED, TASK_CANCELLED, TASK_SKIPPED,
    }),
    TASK_CLAIMED: frozenset({
        TASK_RUNNING, TASK_READY, TASK_FAILED, TASK_CANCELLED, TASK_BLOCKED,
    }),
    TASK_RUNNING: frozenset({
        TASK_COMPLETED, TASK_FAILED, TASK_READY, TASK_CANCELLED, TASK_BLOCKED,
    }),
    TASK_BLOCKED: frozenset({
        TASK_READY, TASK_CLAIMED, TASK_FAILED, TASK_CANCELLED, TASK_SKIPPED,
    }),
    TASK_FAILED: frozenset({TASK_READY}),
    TASK_CANCELLED: frozenset(),
    TASK_SKIPPED: frozenset({TASK_READY}),
    TASK_COMPLETED: frozenset(),
}


def can_transition_task(current: str, target: str) -> bool:
    return target in ALLOWED_TASK_TRANSITIONS.get(current, frozenset())


# ── 消息状态（任务书 §4.4）──────────────────────────────────────────────────

MESSAGE_PENDING = "pending"
MESSAGE_DELIVERED = "delivered"
MESSAGE_ACKED = "acked"
MESSAGE_FAILED = "failed"
