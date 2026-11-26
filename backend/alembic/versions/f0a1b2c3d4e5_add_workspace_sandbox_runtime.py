"""Workspace Sandbox Runtime：Project/Workspace/Sandbox/Job/Service/Preview 表。

Revision ID: f0a1b2c3d4e5
Revises: e9f0a1b2c3d4
Create Date: 2026-11-27

说明：
- 全部为新增表 + `agent_sessions.project_id` 可空列，存量数据不受影响；
- 未归属项目的旧会话继续使用旧文件后端（任务书 §18）；
- 生产由 entrypoint 的 `alembic upgrade head` 执行，开发由 init_db 自动补新表。
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "f0a1b2c3d4e5"
down_revision: Union[str, None] = "e9f0a1b2c3d4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_table(table: str) -> bool:
    return table in sa.inspect(op.get_bind()).get_table_names()


def _has_column(table: str, column: str) -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if table not in inspector.get_table_names():
        return False
    return column in [c["name"] for c in inspector.get_columns(table)]


def _index_names(table: str) -> set:
    if not _has_table(table):
        return set()
    return {i["name"] for i in sa.inspect(op.get_bind()).get_indexes(table)}


def upgrade() -> None:
    bind = op.get_bind()
    is_sqlite = bind.dialect.name == "sqlite"

    # ── Projects / Members ────────────────────────────────────────────
    if not _has_table("projects"):
        op.create_table(
            "projects",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("name", sa.String(120), nullable=False),
            sa.Column("description", sa.Text(), nullable=True),
            sa.Column("owner_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
            sa.Column("tenant_id", sa.String(64), nullable=True),
            sa.Column("status", sa.String(24), nullable=False, server_default="active"),
            sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
            sa.Column("deleted_at", sa.DateTime(), nullable=True),
        )
        op.create_index("ix_projects_owner_id", "projects", ["owner_id"])
        op.create_index("ix_projects_tenant_id", "projects", ["tenant_id"])
        op.create_index("ix_projects_owner_status", "projects", ["owner_id", "status"])

    if not _has_table("project_members"):
        op.create_table(
            "project_members",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("project_id", sa.Integer(), sa.ForeignKey("projects.id"), nullable=False),
            sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
            sa.Column("role", sa.String(16), nullable=False, server_default="operator"),
            sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
            sa.UniqueConstraint("project_id", "user_id", name="uq_project_member"),
        )
        op.create_index("ix_project_members_project_id", "project_members", ["project_id"])
        op.create_index("ix_project_members_user_id", "project_members", ["user_id"])

    # ── 资源档位 ──────────────────────────────────────────────────────
    if not _has_table("sandbox_resource_profiles"):
        op.create_table(
            "sandbox_resource_profiles",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("name", sa.String(48), nullable=False, unique=True),
            sa.Column("display_name", sa.String(80), nullable=True),
            sa.Column("cpu_request_millis", sa.Integer(), nullable=False, server_default="500"),
            sa.Column("cpu_limit_millis", sa.Integer(), nullable=False, server_default="1000"),
            sa.Column("memory_request_bytes", sa.BigInteger(), nullable=False, server_default="536870912"),
            sa.Column("memory_limit_bytes", sa.BigInteger(), nullable=False, server_default="1073741824"),
            sa.Column("disk_quota_bytes", sa.BigInteger(), nullable=False, server_default="5368709120"),
            sa.Column("pid_limit", sa.Integer(), nullable=False, server_default="256"),
            sa.Column("max_processes", sa.Integer(), nullable=False, server_default="128"),
            sa.Column("max_services", sa.Integer(), nullable=False, server_default="4"),
            sa.Column("max_ports", sa.Integer(), nullable=False, server_default="4"),
            sa.Column("idle_timeout_seconds", sa.Integer(), nullable=False, server_default="1800"),
            sa.Column("network_policy", sa.String(24), nullable=False, server_default="egress"),
            sa.Column("is_default", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        )
        op.create_index("ix_sandbox_resource_profiles_name",
                        "sandbox_resource_profiles", ["name"], unique=True)

    # ── Workspaces ────────────────────────────────────────────────────
    if not _has_table("workspaces"):
        op.create_table(
            "workspaces",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("project_id", sa.Integer(), sa.ForeignKey("projects.id"), nullable=False),
            sa.Column("tenant_id", sa.String(64), nullable=True),
            sa.Column("owner_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
            sa.Column("status", sa.String(24), nullable=False, server_default="provisioning"),
            sa.Column("volume_id", sa.String(128), nullable=False),
            sa.Column("volume_driver", sa.String(32), nullable=False, server_default="local"),
            sa.Column("storage_quota_bytes", sa.BigInteger(), nullable=False,
                      server_default="5368709120"),
            sa.Column("resource_profile_id", sa.Integer(),
                      sa.ForeignKey("sandbox_resource_profiles.id"), nullable=True),
            sa.Column("current_sandbox_id", sa.Integer(), nullable=True),
            sa.Column("write_lease_owner", sa.String(128), nullable=True),
            sa.Column("write_lease_token", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("write_lease_expires_at", sa.DateTime(), nullable=True),
            sa.Column("write_lease_heartbeat_at", sa.DateTime(), nullable=True),
            sa.Column("last_active_at", sa.DateTime(), nullable=True),
            sa.Column("version", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
            sa.Column("deleted_at", sa.DateTime(), nullable=True),
            sa.UniqueConstraint("project_id", name="uq_workspace_project"),
        )
        op.create_index("ix_workspaces_project_id", "workspaces", ["project_id"], unique=True)
        op.create_index("ix_workspaces_tenant_id", "workspaces", ["tenant_id"])
        op.create_index("ix_workspaces_owner_user_id", "workspaces", ["owner_user_id"])
        op.create_index("ix_workspaces_volume_id", "workspaces", ["volume_id"], unique=True)

    # ── Runner ────────────────────────────────────────────────────────
    if not _has_table("sandbox_runners"):
        op.create_table(
            "sandbox_runners",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("node_name", sa.String(128), nullable=False, unique=True),
            sa.Column("status", sa.String(16), nullable=False, server_default="online"),
            sa.Column("runtime_provider", sa.String(32), nullable=False, server_default="docker"),
            sa.Column("runtime_version", sa.String(64), nullable=True),
            sa.Column("cpu_total_millis", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("cpu_allocatable_millis", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("cpu_reserved_millis", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("memory_total_bytes", sa.BigInteger(), nullable=False, server_default="0"),
            sa.Column("memory_allocatable_bytes", sa.BigInteger(), nullable=False, server_default="0"),
            sa.Column("memory_reserved_bytes", sa.BigInteger(), nullable=False, server_default="0"),
            sa.Column("disk_total_bytes", sa.BigInteger(), nullable=False, server_default="0"),
            sa.Column("disk_allocatable_bytes", sa.BigInteger(), nullable=False, server_default="0"),
            sa.Column("disk_reserved_bytes", sa.BigInteger(), nullable=False, server_default="0"),
            sa.Column("active_sandbox_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("last_heartbeat_at", sa.DateTime(), nullable=True),
            sa.Column("capabilities_json", sa.JSON(), nullable=True),
            sa.Column("version", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        )
        op.create_index("ix_sandbox_runners_node_name", "sandbox_runners",
                        ["node_name"], unique=True)

    # ── Sandbox Instances ─────────────────────────────────────────────
    if not _has_table("sandbox_instances"):
        op.create_table(
            "sandbox_instances",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("workspace_id", sa.Integer(), sa.ForeignKey("workspaces.id"), nullable=False),
            sa.Column("generation", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("runner_id", sa.Integer(), sa.ForeignKey("sandbox_runners.id"), nullable=True),
            sa.Column("container_id", sa.String(128), nullable=True),
            sa.Column("container_name", sa.String(128), nullable=True),
            sa.Column("container_ip", sa.String(64), nullable=True),
            sa.Column("image_digest", sa.String(160), nullable=True),
            sa.Column("runtime_provider", sa.String(32), nullable=False, server_default="docker"),
            sa.Column("desired_state", sa.String(16), nullable=False, server_default="running"),
            sa.Column("observed_state", sa.String(24), nullable=False, server_default="queued"),
            sa.Column("lifecycle_version", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("cpu_request_millis", sa.Integer(), nullable=False, server_default="500"),
            sa.Column("cpu_limit_millis", sa.Integer(), nullable=False, server_default="1000"),
            sa.Column("memory_request_bytes", sa.BigInteger(), nullable=False,
                      server_default="536870912"),
            sa.Column("memory_limit_bytes", sa.BigInteger(), nullable=False,
                      server_default="1073741824"),
            sa.Column("disk_quota_bytes", sa.BigInteger(), nullable=False,
                      server_default="5368709120"),
            sa.Column("pid_limit", sa.Integer(), nullable=False, server_default="256"),
            sa.Column("last_heartbeat_at", sa.DateTime(), nullable=True),
            sa.Column("last_observed_at", sa.DateTime(), nullable=True),
            sa.Column("failure_code", sa.String(64), nullable=True),
            sa.Column("failure_message", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
            sa.UniqueConstraint("workspace_id", "generation", name="uq_sandbox_ws_generation"),
        )
        op.create_index("ix_sandbox_instances_workspace_id", "sandbox_instances", ["workspace_id"])
        op.create_index("ix_sandbox_instances_runner_id", "sandbox_instances", ["runner_id"])
        op.create_index("ix_sandbox_instances_container_id", "sandbox_instances", ["container_id"])
        op.create_index("ix_sandbox_instances_container_name", "sandbox_instances",
                        ["container_name"], unique=True)
        op.create_index("ix_sandbox_ws_observed", "sandbox_instances",
                        ["workspace_id", "observed_state"])

    # ── Jobs ──────────────────────────────────────────────────────────
    if not _has_table("sandbox_jobs"):
        op.create_table(
            "sandbox_jobs",
            sa.Column("id", sa.String(32), primary_key=True),
            sa.Column("workspace_id", sa.Integer(), sa.ForeignKey("workspaces.id"), nullable=False),
            sa.Column("sandbox_id", sa.Integer(), sa.ForeignKey("sandbox_instances.id"), nullable=True),
            sa.Column("target_runner_id", sa.Integer(), sa.ForeignKey("sandbox_runners.id"), nullable=True),
            sa.Column("job_type", sa.String(24), nullable=False),
            sa.Column("status", sa.String(16), nullable=False, server_default="queued"),
            sa.Column("priority", sa.Integer(), nullable=False, server_default="100"),
            sa.Column("idempotency_key", sa.String(160), nullable=False, unique=True),
            sa.Column("attempt", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="3"),
            sa.Column("lease_owner", sa.String(128), nullable=True),
            sa.Column("lease_token", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("lease_expires_at", sa.DateTime(), nullable=True),
            sa.Column("payload_json", sa.JSON(), nullable=True),
            sa.Column("result_json", sa.JSON(), nullable=True),
            sa.Column("error_code", sa.String(64), nullable=True),
            sa.Column("error_message", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
            sa.Column("started_at", sa.DateTime(), nullable=True),
            sa.Column("finished_at", sa.DateTime(), nullable=True),
            sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        )
        op.create_index("ix_sandbox_jobs_workspace_id", "sandbox_jobs", ["workspace_id"])
        op.create_index("ix_sandbox_jobs_sandbox_id", "sandbox_jobs", ["sandbox_id"])
        op.create_index("ix_sandbox_jobs_target_runner_id", "sandbox_jobs", ["target_runner_id"])
        op.create_index("ix_sandbox_jobs_job_type", "sandbox_jobs", ["job_type"])
        op.create_index("ix_sandbox_jobs_status", "sandbox_jobs", ["status"])
        op.create_index("ix_sandbox_jobs_idempotency_key", "sandbox_jobs",
                        ["idempotency_key"], unique=True)
        op.create_index("ix_sandbox_jobs_queue", "sandbox_jobs",
                        ["status", "priority", "created_at"])
        op.create_index("ix_sandbox_jobs_lease", "sandbox_jobs",
                        ["status", "lease_expires_at"])

    # ── 资源预留 ──────────────────────────────────────────────────────
    if not _has_table("sandbox_resource_reservations"):
        op.create_table(
            "sandbox_resource_reservations",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("runner_id", sa.Integer(), sa.ForeignKey("sandbox_runners.id"), nullable=False),
            sa.Column("workspace_id", sa.Integer(), sa.ForeignKey("workspaces.id"), nullable=False),
            sa.Column("sandbox_id", sa.Integer(), nullable=True),
            sa.Column("cpu_reserved_millis", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("memory_reserved_bytes", sa.BigInteger(), nullable=False, server_default="0"),
            sa.Column("disk_reserved_bytes", sa.BigInteger(), nullable=False, server_default="0"),
            sa.Column("status", sa.String(16), nullable=False, server_default="reserved"),
            sa.Column("reservation_token", sa.String(64), nullable=False, unique=True),
            sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
            sa.Column("released_at", sa.DateTime(), nullable=True),
        )
        op.create_index("ix_sandbox_resource_reservations_runner_id",
                        "sandbox_resource_reservations", ["runner_id"])
        op.create_index("ix_sandbox_resource_reservations_workspace_id",
                        "sandbox_resource_reservations", ["workspace_id"])
        op.create_index("ix_sandbox_resource_reservations_sandbox_id",
                        "sandbox_resource_reservations", ["sandbox_id"])
        op.create_index("ix_sandbox_reservation_token", "sandbox_resource_reservations",
                        ["reservation_token"], unique=True)

    # ── Services ──────────────────────────────────────────────────────
    if not _has_table("workspace_services"):
        op.create_table(
            "workspace_services",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("workspace_id", sa.Integer(), sa.ForeignKey("workspaces.id"), nullable=False),
            sa.Column("sandbox_id", sa.Integer(), sa.ForeignKey("sandbox_instances.id"), nullable=True),
            sa.Column("name", sa.String(80), nullable=False),
            sa.Column("command", sa.Text(), nullable=False),
            sa.Column("working_directory", sa.String(255), nullable=False, server_default="/workspace"),
            sa.Column("port", sa.Integer(), nullable=False),
            sa.Column("protocol", sa.String(16), nullable=False, server_default="http"),
            sa.Column("health_check_type", sa.String(16), nullable=False, server_default="http"),
            sa.Column("health_check_path", sa.String(255), nullable=False, server_default="/"),
            sa.Column("health_check_timeout", sa.Integer(), nullable=False, server_default="60"),
            sa.Column("desired_state", sa.String(16), nullable=False, server_default="running"),
            sa.Column("observed_state", sa.String(24), nullable=False, server_default="created"),
            sa.Column("auto_restart", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("auto_start_on_sandbox_start", sa.Boolean(), nullable=False,
                      server_default=sa.true()),
            sa.Column("process_id", sa.String(64), nullable=True),
            sa.Column("exit_code", sa.Integer(), nullable=True),
            sa.Column("last_started_at", sa.DateTime(), nullable=True),
            sa.Column("last_exited_at", sa.DateTime(), nullable=True),
            sa.Column("log_reference", sa.String(255), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
            sa.UniqueConstraint("workspace_id", "name", name="uq_workspace_service_name"),
            sa.UniqueConstraint("workspace_id", "port", name="uq_workspace_service_port"),
        )
        op.create_index("ix_workspace_services_workspace_id", "workspace_services", ["workspace_id"])
        op.create_index("ix_workspace_services_sandbox_id", "workspace_services", ["sandbox_id"])

    # ── Preview Grants ────────────────────────────────────────────────
    if not _has_table("preview_grants"):
        op.create_table(
            "preview_grants",
            sa.Column("id", sa.String(32), primary_key=True),
            sa.Column("workspace_id", sa.Integer(), sa.ForeignKey("workspaces.id"), nullable=False),
            sa.Column("sandbox_id", sa.Integer(), sa.ForeignKey("sandbox_instances.id"), nullable=False),
            sa.Column("service_id", sa.Integer(), sa.ForeignKey("workspace_services.id"), nullable=False),
            sa.Column("port", sa.Integer(), nullable=False),
            sa.Column("token_hash", sa.String(64), nullable=False),
            sa.Column("expires_at", sa.DateTime(), nullable=True),
            sa.Column("revoked_at", sa.DateTime(), nullable=True),
            sa.Column("created_by_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
            sa.Column("last_accessed_at", sa.DateTime(), nullable=True),
        )
        op.create_index("ix_preview_grants_workspace_id", "preview_grants", ["workspace_id"])
        op.create_index("ix_preview_grants_sandbox_id", "preview_grants", ["sandbox_id"])
        op.create_index("ix_preview_grants_service_id", "preview_grants", ["service_id"])
        op.create_index("ix_preview_grants_token_hash", "preview_grants",
                        ["token_hash"], unique=True)

    # ── 命令操作 ──────────────────────────────────────────────────────
    if not _has_table("sandbox_command_operations"):
        op.create_table(
            "sandbox_command_operations",
            sa.Column("id", sa.String(32), primary_key=True),
            sa.Column("workspace_id", sa.Integer(), sa.ForeignKey("workspaces.id"), nullable=False),
            sa.Column("sandbox_id", sa.Integer(), sa.ForeignKey("sandbox_instances.id"), nullable=True),
            sa.Column("sandbox_generation", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("run_id", sa.String(36), nullable=True),
            sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
            sa.Column("command", sa.Text(), nullable=False),
            sa.Column("working_directory", sa.String(255), nullable=False, server_default="/workspace"),
            sa.Column("status", sa.String(16), nullable=False, server_default="running"),
            sa.Column("exit_code", sa.Integer(), nullable=True),
            sa.Column("stdout_text", sa.Text(), nullable=True),
            sa.Column("stderr_text", sa.Text(), nullable=True),
            sa.Column("truncated", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("timeout_seconds", sa.Integer(), nullable=False, server_default="600"),
            sa.Column("started_at", sa.DateTime(), nullable=True),
            sa.Column("finished_at", sa.DateTime(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        )
        op.create_index("ix_sandbox_command_operations_workspace_id",
                        "sandbox_command_operations", ["workspace_id"])
        op.create_index("ix_sandbox_command_operations_sandbox_id",
                        "sandbox_command_operations", ["sandbox_id"])
        op.create_index("ix_sandbox_command_operations_run_id",
                        "sandbox_command_operations", ["run_id"])

    # ── 审计事件 ──────────────────────────────────────────────────────
    if not _has_table("sandbox_events"):
        op.create_table(
            "sandbox_events",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("event_type", sa.String(64), nullable=False),
            sa.Column("tenant_id", sa.String(64), nullable=True),
            sa.Column("user_id", sa.Integer(), nullable=True),
            sa.Column("project_id", sa.Integer(), nullable=True),
            sa.Column("workspace_id", sa.Integer(), nullable=True),
            sa.Column("sandbox_id", sa.Integer(), nullable=True),
            sa.Column("generation", sa.Integer(), nullable=True),
            sa.Column("run_id", sa.String(36), nullable=True),
            sa.Column("job_id", sa.String(32), nullable=True),
            sa.Column("payload", sa.JSON(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        )
        op.create_index("ix_sandbox_events_event_type", "sandbox_events", ["event_type"])
        op.create_index("ix_sandbox_events_user_id", "sandbox_events", ["user_id"])
        op.create_index("ix_sandbox_events_project_id", "sandbox_events", ["project_id"])
        op.create_index("ix_sandbox_events_workspace_id", "sandbox_events", ["workspace_id"])
        op.create_index("ix_sandbox_events_sandbox_id", "sandbox_events", ["sandbox_id"])
        op.create_index("ix_sandbox_events_run_id", "sandbox_events", ["run_id"])
        op.create_index("ix_sandbox_events_job_id", "sandbox_events", ["job_id"])
        op.create_index("ix_sandbox_events_ws_id", "sandbox_events", ["workspace_id", "id"])

    # ── 会话项目归属（存量会话保持 NULL）──────────────────────────────
    if not _has_column("agent_sessions", "project_id"):
        if is_sqlite:
            # SQLite 无法直接 add FK 列；与 init_db 的新库一致即可（测试库）
            op.add_column("agent_sessions", sa.Column("project_id", sa.Integer(), nullable=True))
        else:
            op.add_column("agent_sessions", sa.Column(
                "project_id", sa.Integer(), sa.ForeignKey("projects.id"), nullable=True))
        op.create_index("ix_agent_sessions_project_id", "agent_sessions", ["project_id"])


def downgrade() -> None:
    if _has_column("agent_sessions", "project_id"):
        if "ix_agent_sessions_project_id" in _index_names("agent_sessions"):
            op.drop_index("ix_agent_sessions_project_id", table_name="agent_sessions")
        op.drop_column("agent_sessions", "project_id")

    for table in (
        "sandbox_events", "sandbox_command_operations", "preview_grants",
        "workspace_services", "sandbox_resource_reservations", "sandbox_jobs",
        "sandbox_instances", "sandbox_runners", "workspaces",
        "sandbox_resource_profiles", "project_members", "projects",
    ):
        if _has_table(table):
            op.drop_table(table)
