"""Agent Team Runtime：团队、成员、任务板、消息与产物表。

Revision ID: c8d9e0f1a2b3
Revises: b6c7d8e9f0a1
Create Date: 2026-11-27

说明：
- ``agent_team_runs.team_run_id`` 即 ``agent_runs.id``（一个 Run 一个团队），
  外键级联删除；历史 Run 不回溯建团队行。
- 所有表使用项目现有 ORM/迁移规范；索引覆盖 Team / Member / Task / Inbox / 租约查询。
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql


revision = "c8d9e0f1a2b3"
down_revision = "b6c7d8e9f0a1"
branch_labels = None
depends_on = None


def _has_table(name: str) -> bool:
    return name in sa.inspect(op.get_bind()).get_table_names()


def _has_index(table: str, name: str) -> bool:
    try:
        return any(item.get("name") == name for item in sa.inspect(op.get_bind()).get_indexes(table))
    except Exception:  # noqa: BLE001 表不存在/方言差异
        return False


def upgrade() -> None:
    if not _has_table("agent_team_runs"):
        op.create_table(
            "agent_team_runs",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.Column("team_run_id", sa.String(length=36), nullable=False),
            sa.Column("objective", sa.Text(), nullable=False),
            sa.Column("mode", sa.String(length=16), nullable=False, server_default="team"),
            sa.Column("status", sa.String(length=24), nullable=False, server_default="running"),
            sa.Column("phase", sa.String(length=24), nullable=False, server_default="planning"),
            sa.Column("leader_member_id", sa.String(length=40), nullable=True),
            sa.Column("max_members", sa.Integer(), nullable=False, server_default="4"),
            sa.Column("max_parallel", sa.Integer(), nullable=False, server_default="4"),
            sa.Column("config_snapshot", sa.JSON(), nullable=False),
            sa.Column("finished_at", sa.DateTime(), nullable=True),
            sa.ForeignKeyConstraint(["team_run_id"], ["agent_runs.id"], ondelete="CASCADE"),
            sa.UniqueConstraint("team_run_id", name="uq_agent_team_run"),
        )
    if not _has_index("agent_team_runs", "ix_agent_team_runs_team_run_id"):
        op.create_index("ix_agent_team_runs_team_run_id", "agent_team_runs", ["team_run_id"])
    if not _has_index("agent_team_runs", "ix_agent_team_runs_status_created"):
        op.create_index("ix_agent_team_runs_status_created", "agent_team_runs", ["status", "created_at"])

    if not _has_table("agent_team_members"):
        op.create_table(
            "agent_team_members",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.Column("member_id", sa.String(length=40), nullable=False),
            sa.Column("team_run_id", sa.String(length=36), nullable=False),
            sa.Column("parent_member_id", sa.String(length=40), nullable=True),
            sa.Column("task_id", sa.String(length=40), nullable=True),
            sa.Column("role", sa.String(length=80), nullable=False),
            sa.Column("name", sa.String(length=80), nullable=False),
            sa.Column("profile_snapshot", sa.JSON(), nullable=False),
            sa.Column("thread_id", sa.String(length=160), nullable=False),
            sa.Column("model_snapshot", sa.JSON(), nullable=False),
            sa.Column("status", sa.String(length=24), nullable=False, server_default="created"),
            sa.Column("budget", sa.JSON(), nullable=False),
            sa.Column("model_calls", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("tool_calls", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("tokens", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("recovery_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("last_error", sa.Text(), nullable=True),
            sa.Column("started_at", sa.DateTime(), nullable=True),
            sa.Column("completed_at", sa.DateTime(), nullable=True),
            sa.ForeignKeyConstraint(
                ["team_run_id"], ["agent_team_runs.team_run_id"], ondelete="CASCADE",
            ),
            sa.UniqueConstraint("team_run_id", "member_id", name="uq_agent_team_member"),
            sa.UniqueConstraint("thread_id", name="uq_agent_team_member_thread"),
        )
    if not _has_index("agent_team_members", "ix_agent_team_members_team_run_id"):
        op.create_index("ix_agent_team_members_team_run_id", "agent_team_members", ["team_run_id"])
    if not _has_index("agent_team_members", "ix_agent_team_members_team_status"):
        op.create_index("ix_agent_team_members_team_status", "agent_team_members",
                        ["team_run_id", "status"])
    if not _has_index("agent_team_members", "ix_agent_team_members_task"):
        op.create_index("ix_agent_team_members_task", "agent_team_members",
                        ["team_run_id", "task_id"])

    if not _has_table("agent_team_tasks"):
        op.create_table(
            "agent_team_tasks",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.Column("task_id", sa.String(length=40), nullable=False),
            sa.Column("team_run_id", sa.String(length=36), nullable=False),
            sa.Column("parent_task_id", sa.String(length=40), nullable=True),
            sa.Column("subject", sa.String(length=200), nullable=False),
            sa.Column("description", sa.Text(), nullable=False),
            sa.Column("role", sa.String(length=80), nullable=False, server_default=""),
            sa.Column("status", sa.String(length=24), nullable=False, server_default="pending"),
            sa.Column("owner_member_id", sa.String(length=40), nullable=True),
            sa.Column("priority", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("write_scopes", sa.JSON(), nullable=False),
            sa.Column("retry_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("max_retries", sa.Integer(), nullable=False, server_default="2"),
            sa.Column("lease_token", sa.String(length=64), nullable=True),
            sa.Column("lease_expires_at", sa.DateTime(), nullable=True),
            sa.Column("blocked_reason", sa.Text(), nullable=True),
            sa.Column("result_artifact_id", sa.String(length=40), nullable=True),
            sa.Column("error", sa.Text(), nullable=True),
            sa.Column("completed_at", sa.DateTime(), nullable=True),
            sa.ForeignKeyConstraint(
                ["team_run_id"], ["agent_team_runs.team_run_id"], ondelete="CASCADE",
            ),
            sa.UniqueConstraint("team_run_id", "task_id", name="uq_agent_team_task"),
        )
    if not _has_index("agent_team_tasks", "ix_agent_team_tasks_team_run_id"):
        op.create_index("ix_agent_team_tasks_team_run_id", "agent_team_tasks", ["team_run_id"])
    if not _has_index("agent_team_tasks", "ix_agent_team_tasks_team_status"):
        op.create_index("ix_agent_team_tasks_team_status", "agent_team_tasks",
                        ["team_run_id", "status"])
    if not _has_index("agent_team_tasks", "ix_agent_team_tasks_owner_status"):
        op.create_index("ix_agent_team_tasks_owner_status", "agent_team_tasks",
                        ["owner_member_id", "status"])
    if not _has_index("agent_team_tasks", "ix_agent_team_tasks_lease"):
        op.create_index("ix_agent_team_tasks_lease", "agent_team_tasks",
                        ["status", "lease_expires_at"])

    if not _has_table("agent_team_task_dependencies"):
        op.create_table(
            "agent_team_task_dependencies",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.Column("team_run_id", sa.String(length=36), nullable=False),
            sa.Column("task_id", sa.String(length=40), nullable=False),
            sa.Column("depends_on_task_id", sa.String(length=40), nullable=False),
            sa.ForeignKeyConstraint(
                ["team_run_id"], ["agent_team_runs.team_run_id"], ondelete="CASCADE",
            ),
            sa.UniqueConstraint(
                "team_run_id", "task_id", "depends_on_task_id",
                name="uq_agent_team_task_dependency",
            ),
        )
    if not _has_index("agent_team_task_dependencies", "ix_agent_team_task_deps_team"):
        op.create_index("ix_agent_team_task_deps_team", "agent_team_task_dependencies",
                        ["team_run_id", "task_id"])
    if not _has_index("agent_team_task_dependencies", "ix_agent_team_task_dependencies_team_run_id"):
        op.create_index("ix_agent_team_task_dependencies_team_run_id",
                        "agent_team_task_dependencies", ["team_run_id"])

    if not _has_table("agent_team_messages"):
        op.create_table(
            "agent_team_messages",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.Column("message_id", sa.String(length=40), nullable=False),
            sa.Column("team_run_id", sa.String(length=36), nullable=False),
            sa.Column("sender_member_id", sa.String(length=40), nullable=False),
            sa.Column("recipient_member_id", sa.String(length=40), nullable=True),
            sa.Column("message_type", sa.String(length=40), nullable=False, server_default="text"),
            sa.Column("payload", sa.JSON(), nullable=False),
            sa.Column("artifact_id", sa.String(length=40), nullable=True),
            sa.Column("correlation_id", sa.String(length=64), nullable=True),
            sa.Column("idempotency_key", sa.String(length=128), nullable=True),
            sa.Column("status", sa.String(length=24), nullable=False, server_default="pending"),
            sa.Column("error", sa.Text(), nullable=True),
            sa.Column("delivered_at", sa.DateTime(), nullable=True),
            sa.Column("acked_at", sa.DateTime(), nullable=True),
            sa.ForeignKeyConstraint(
                ["team_run_id"], ["agent_team_runs.team_run_id"], ondelete="CASCADE",
            ),
            sa.UniqueConstraint("message_id", name="uq_agent_team_message"),
            sa.UniqueConstraint(
                "team_run_id", "idempotency_key", name="uq_agent_team_message_idem",
            ),
        )
    if not _has_index("agent_team_messages", "ix_agent_team_messages_team_run_id"):
        op.create_index("ix_agent_team_messages_team_run_id", "agent_team_messages",
                        ["team_run_id"])
    if not _has_index("agent_team_messages", "ix_agent_team_messages_inbox"):
        op.create_index("ix_agent_team_messages_inbox", "agent_team_messages",
                        ["team_run_id", "recipient_member_id", "status", "created_at"])

    if not _has_table("agent_team_artifacts"):
        content_type = sa.Text().with_variant(mysql.LONGTEXT(), "mysql", "mariadb")
        op.create_table(
            "agent_team_artifacts",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.Column("artifact_id", sa.String(length=40), nullable=False),
            sa.Column("team_run_id", sa.String(length=36), nullable=False),
            sa.Column("task_id", sa.String(length=40), nullable=True),
            sa.Column("member_id", sa.String(length=40), nullable=True),
            sa.Column("name", sa.String(length=200), nullable=False, server_default=""),
            sa.Column("content_type", sa.String(length=80), nullable=False,
                      server_default="text/plain"),
            sa.Column("content", content_type, nullable=False),
            sa.Column("content_preview", sa.Text(), nullable=False),
            sa.Column("size_bytes", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("checksum", sa.String(length=64), nullable=False, server_default=""),
            sa.ForeignKeyConstraint(
                ["team_run_id"], ["agent_team_runs.team_run_id"], ondelete="CASCADE",
            ),
            sa.UniqueConstraint("artifact_id", name="uq_agent_team_artifact"),
        )
    if not _has_index("agent_team_artifacts", "ix_agent_team_artifacts_team_run_id"):
        op.create_index("ix_agent_team_artifacts_team_run_id", "agent_team_artifacts",
                        ["team_run_id"])
    if not _has_index("agent_team_artifacts", "ix_agent_team_artifacts_team"):
        op.create_index("ix_agent_team_artifacts_team", "agent_team_artifacts",
                        ["team_run_id", "created_at"])
    if not _has_index("agent_team_artifacts", "ix_agent_team_artifacts_task"):
        op.create_index("ix_agent_team_artifacts_task", "agent_team_artifacts",
                        ["team_run_id", "task_id"])


def downgrade() -> None:
    for table in (
        "agent_team_artifacts", "agent_team_messages", "agent_team_task_dependencies",
        "agent_team_tasks", "agent_team_members", "agent_team_runs",
    ):
        if _has_table(table):
            op.drop_table(table)
