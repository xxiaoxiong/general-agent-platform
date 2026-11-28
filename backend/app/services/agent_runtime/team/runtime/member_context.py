"""成员执行上下文与统一结果（任务书 §3.1）。"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from .member_profile import MemberProfile


@dataclass
class MemberExecutionContext:
    team_run_id: str
    member_id: str
    role: str
    thread_id: str
    task_id: Optional[str]
    objective: str
    task_revision: Optional[int] = None
    reference_context: str = ""
    assistant_prompt: str = ""
    task_brief: str = ""
    predecessors: list[dict[str, Any]] = field(default_factory=list)
    acceptance: list[str] = field(default_factory=list)
    findings: list[dict[str, Any]] = field(default_factory=list)
    prior_result: str = ""
    profile: Optional[MemberProfile] = None
    # 追加输入（成员被消息/任务唤醒时注入；为空表示恢复原 Thread）
    incoming_messages: list[dict[str, Any]] = field(default_factory=list)
    resumed: bool = False


@dataclass
class MemberResult:
    team_run_id: str
    member_id: str
    task_id: Optional[str]
    status: str                      # completed / failed / cancelled / interrupted
    output: str = ""
    artifact_id: Optional[str] = None
    error: str = ""
    model_calls: int = 0
    tool_calls: int = 0
    tokens: int = 0
    duration_ms: int = 0
    thread_id: str = ""
    recovered: bool = False

    @property
    def ok(self) -> bool:
        return self.status == "completed"

    def to_dict(self) -> dict[str, Any]:
        return {
            "team_run_id": self.team_run_id,
            "member_id": self.member_id,
            "task_id": self.task_id,
            "status": self.status,
            "output": self.output[:4000],
            "artifact_id": self.artifact_id,
            "error": self.error[:2000],
            "model_calls": self.model_calls,
            "tool_calls": self.tool_calls,
            "tokens": self.tokens,
            "duration_ms": self.duration_ms,
            "thread_id": self.thread_id,
            "recovered": self.recovered,
        }
