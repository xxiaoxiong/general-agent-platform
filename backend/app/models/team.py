"""Agent Team Runtime：团队、成员、任务板、消息与产物模型。

设计边界（任务书 §4 / §6）：
- 数据库保存团队协作事实（Team / Member / Task / Message / Artifact）；
- LangGraph Checkpoint 只保存 Agent 执行状态，两类事实不混用；
- ``team_run_id`` 即 ``agent_runs.id``（一个 Run 一个团队），恢复时无需额外映射表；
- Task Board 用 ``revision`` 做 CAS，用 ``lease_token`` + ``lease_expires_at`` 做租约；
- 成员 Thread 稳定为 ``team:{team_run_id}:member:{member_id}``，可独立恢复。
"""
from datetime import datetime
from typing import Optional

from sqlalchemy import (
    JSON, Column, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint,
)
from sqlalchemy.dialects.mysql import LONGTEXT

from app.models.base import BaseModel


class TeamRun(BaseModel):
    """一次团队协作（与 AgentRun 1:1）。"""

    __tablename__ = "agent_team_runs"
    __table_args__ = (
        UniqueConstraint("team_run_id", name="uq_agent_team_run"),
        Index("ix_agent_team_runs_status_created", "status", "created_at"),
    )

    # 任务书字段 team_run_id：直接使用 AgentRun.id，保证恢复/查询稳定
    team_run_id = Column(
        String(36), ForeignKey("agent_runs.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    objective = Column(Text, default="", nullable=False)
    # team=通用团队；project=软件项目团队（保留原工程图，仅登记团队事实）
    mode = Column(String(16), default="team", nullable=False)
    status = Column(String(24), default="running", nullable=False)   # running/completed/failed/cancelled
    phase = Column(String(24), default="planning", nullable=False)   # 与 team.updated 的阶段一致
    leader_member_id = Column(String(40), nullable=True)
    max_members = Column(Integer, default=4, nullable=False)
    max_parallel = Column(Integer, default=4, nullable=False)
    # 团队级配置快照（不含密钥）
    config_snapshot = Column(JSON, default=dict, nullable=False)
    finished_at = Column(DateTime, nullable=True)


class TeamMember(BaseModel):
    """团队成员（Leader 也以成员身份登记）。"""

    __tablename__ = "agent_team_members"
    __table_args__ = (
        # member_id 在团队内唯一（不同 Run 都可有 leader）
        UniqueConstraint("team_run_id", "member_id", name="uq_agent_team_member"),
        UniqueConstraint("thread_id", name="uq_agent_team_member_thread"),
        Index("ix_agent_team_members_team_status", "team_run_id", "status"),
        Index("ix_agent_team_members_task", "team_run_id", "task_id"),
    )

    member_id = Column(String(40), nullable=False)
    team_run_id = Column(
        String(36), ForeignKey("agent_team_runs.team_run_id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    parent_member_id = Column(String(40), nullable=True)
    # 当前执行任务（Leader 为空；成员重新认领时更新）
    task_id = Column(String(40), nullable=True)
    role = Column(String(80), nullable=False)
    name = Column(String(80), nullable=False)
    profile_snapshot = Column(JSON, default=dict, nullable=False)
    # 独立、稳定的 Checkpoint Thread
    thread_id = Column(String(160), nullable=False)
    model_snapshot = Column(JSON, default=dict, nullable=False)
    status = Column(String(24), default="created", nullable=False)
    # 预算：{"max_model_calls":8,"max_tool_calls":12,"timeout_seconds":300}
    budget = Column(JSON, default=dict, nullable=False)
    model_calls = Column(Integer, default=0, nullable=False)
    tool_calls = Column(Integer, default=0, nullable=False)
    tokens = Column(Integer, default=0, nullable=False)
    recovery_count = Column(Integer, default=0, nullable=False)
    last_error = Column(Text, nullable=True)
    started_at = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)


class TeamTask(BaseModel):
    """Task Board 任务（协作事实来源）。"""

    __tablename__ = "agent_team_tasks"
    __table_args__ = (
        # task_id 在团队内唯一（不同团队可使用相同 ID，如 research）
        UniqueConstraint("team_run_id", "task_id", name="uq_agent_team_task"),
        Index("ix_agent_team_tasks_team_status", "team_run_id", "status"),
        Index("ix_agent_team_tasks_owner_status", "owner_member_id", "status"),
        Index("ix_agent_team_tasks_lease", "status", "lease_expires_at"),
    )

    task_id = Column(String(40), nullable=False)
    team_run_id = Column(
        String(36), ForeignKey("agent_team_runs.team_run_id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    parent_task_id = Column(String(40), nullable=True)
    subject = Column(String(200), nullable=False)
    description = Column(Text, default="", nullable=False)
    # 执行角色（规划时快照，成员按角色生成 Profile）
    role = Column(String(80), default="", nullable=False)
    # pending/ready/claimed/running/blocked/completed/failed/cancelled/skipped
    status = Column(String(24), default="pending", nullable=False)
    owner_member_id = Column(String(40), nullable=True)
    priority = Column(Integer, default=0, nullable=False)
    # CAS 版本：任何更新成功 +1，调用方必须携带 expected_revision
    revision = Column(Integer, default=1, nullable=False)
    write_scopes = Column(JSON, default=list, nullable=False)
    retry_count = Column(Integer, default=0, nullable=False)
    max_retries = Column(Integer, default=2, nullable=False)
    lease_token = Column(String(64), nullable=True)
    lease_expires_at = Column(DateTime, nullable=True)
    blocked_reason = Column(Text, nullable=True)
    result_artifact_id = Column(String(40), nullable=True)
    error = Column(Text, nullable=True)
    completed_at = Column(DateTime, nullable=True)


class TeamTaskDependency(BaseModel):
    """任务依赖（同一团队内；禁止自依赖/重复/循环，由服务层校验）。"""

    __tablename__ = "agent_team_task_dependencies"
    __table_args__ = (
        UniqueConstraint(
            "team_run_id", "task_id", "depends_on_task_id",
            name="uq_agent_team_task_dependency",
        ),
        Index("ix_agent_team_task_deps_team", "team_run_id", "task_id"),
    )

    team_run_id = Column(
        String(36), ForeignKey("agent_team_runs.team_run_id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    task_id = Column(String(40), nullable=False)
    depends_on_task_id = Column(String(40), nullable=False)


class TeamMessage(BaseModel):
    """持久化消息（Inbox/Ack；至少一次投递，按 message_id/idempotency_key 去重）。"""

    __tablename__ = "agent_team_messages"
    __table_args__ = (
        UniqueConstraint("message_id", name="uq_agent_team_message"),
        UniqueConstraint(
            "team_run_id", "idempotency_key", name="uq_agent_team_message_idem",
        ),
        Index(
            "ix_agent_team_messages_inbox",
            "team_run_id", "recipient_member_id", "status", "created_at",
        ),
    )

    message_id = Column(String(40), nullable=False)
    team_run_id = Column(
        String(36), ForeignKey("agent_team_runs.team_run_id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    sender_member_id = Column(String(40), nullable=False)
    recipient_member_id = Column(String(40), nullable=True)   # 空=广播给 Leader/团队
    message_type = Column(String(40), default="text", nullable=False)
    payload = Column(JSON, default=dict, nullable=False)
    artifact_id = Column(String(40), nullable=True)
    correlation_id = Column(String(64), nullable=True)
    idempotency_key = Column(String(128), nullable=True)
    # pending/delivered/acked/failed
    status = Column(String(24), default="pending", nullable=False)
    error = Column(Text, nullable=True)
    delivered_at = Column(DateTime, nullable=True)
    acked_at = Column(DateTime, nullable=True)


class TeamArtifact(BaseModel):
    """成员成果（引用式共享；消息/任务只保存 artifact_id）。"""

    __tablename__ = "agent_team_artifacts"
    __table_args__ = (
        UniqueConstraint("artifact_id", name="uq_agent_team_artifact"),
        Index("ix_agent_team_artifacts_team", "team_run_id", "created_at"),
        Index("ix_agent_team_artifacts_task", "team_run_id", "task_id"),
    )

    artifact_id = Column(String(40), nullable=False)
    team_run_id = Column(
        String(36), ForeignKey("agent_team_runs.team_run_id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    task_id = Column(String(40), nullable=True)
    member_id = Column(String(40), nullable=True)
    name = Column(String(200), default="", nullable=False)
    content_type = Column(String(80), default="text/plain", nullable=False)
    # 正文上限由服务层控制（AGENT_TEAM_ARTIFACT_MAX_BYTES）
    content = Column(Text().with_variant(LONGTEXT(), "mysql", "mariadb"), default="", nullable=False)
    content_preview = Column(Text, default="", nullable=False)
    size_bytes = Column(Integer, default=0, nullable=False)
    checksum = Column(String(64), default="", nullable=False)
