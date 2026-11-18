"""自进化闭环三表：agent_evaluations / evolution_reports / evolution_change_log。

Revision ID: a7b8c9d0e1f2
Revises: d0e1f2a3b4c5
"""
from alembic import op
import sqlalchemy as sa

revision = "a7b8c9d0e1f2"
down_revision = "d0e1f2a3b4c5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "agent_evaluations",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("run_id", sa.String(36), sa.ForeignKey("agent_runs.id"), nullable=False),
        sa.Column("agent_id", sa.Integer(), sa.ForeignKey("agents.id"), nullable=False),
        sa.Column("agent_version_id", sa.Integer(), sa.ForeignKey("agent_versions.id"), nullable=True),
        sa.Column("session_id", sa.Integer(), sa.ForeignKey("agent_sessions.id"), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("success", sa.Boolean(), nullable=True),
        sa.Column("overall", sa.Float(), nullable=True),
        sa.Column("deterministic", sa.JSON(), nullable=True),
        sa.Column("judge_scores", sa.JSON(), nullable=True),
        sa.Column("judge_model_config_id", sa.Integer(), nullable=True),
        sa.Column("langfuse_trace_id", sa.String(64), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("evaluated_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("run_id", name="uq_agent_eval_run"),
    )
    op.create_index("ix_agent_evaluations_agent_id", "agent_evaluations", ["agent_id"])
    op.create_index("ix_agent_evaluations_agent_version_id", "agent_evaluations", ["agent_version_id"])
    op.create_index("ix_agent_evaluations_status", "agent_evaluations", ["status"])
    op.create_index("ix_agent_eval_agent_created", "agent_evaluations", ["agent_id", "created_at"])

    op.create_table(
        "evolution_reports",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("agent_id", sa.Integer(), sa.ForeignKey("agents.id"), nullable=False),
        sa.Column("window_start", sa.DateTime(), nullable=False),
        sa.Column("window_end", sa.DateTime(), nullable=False),
        sa.Column("baseline_version_no", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="draft"),
        sa.Column("summary_text", sa.Text(), nullable=True),
        sa.Column("suggestions", sa.JSON(), nullable=True),
        sa.Column("metrics_before", sa.JSON(), nullable=True),
        sa.Column("metrics_after", sa.JSON(), nullable=True),
        sa.Column("created_by", sa.String(64), nullable=True),
        sa.Column("validated_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_evolution_reports_agent_id", "evolution_reports", ["agent_id"])
    op.create_index("ix_evolution_reports_status", "evolution_reports", ["status"])
    op.create_index("ix_evolution_report_agent_created", "evolution_reports", ["agent_id", "created_at"])

    op.create_table(
        "evolution_change_log",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("report_id", sa.Integer(), sa.ForeignKey("evolution_reports.id"), nullable=False),
        sa.Column("agent_id", sa.Integer(), sa.ForeignKey("agents.id"), nullable=False),
        sa.Column("change_type", sa.String(24), nullable=False),
        sa.Column("target", sa.String(128), nullable=False),
        sa.Column("before", sa.JSON(), nullable=True),
        sa.Column("after", sa.JSON(), nullable=True),
        sa.Column("applied_version_no", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="applied"),
        sa.Column("validation", sa.JSON(), nullable=True),
        sa.Column("revert_of_change_id", sa.Integer(), nullable=True),
        sa.Column("applied_by", sa.String(64), nullable=True),
        sa.Column("applied_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("reverted_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_evolution_change_log_report_id", "evolution_change_log", ["report_id"])
    op.create_index("ix_evolution_change_log_agent_id", "evolution_change_log", ["agent_id"])
    op.create_index("ix_evolution_change_log_status", "evolution_change_log", ["status"])
    op.create_index("ix_evolution_change_agent_applied", "evolution_change_log", ["agent_id", "applied_at"])


def downgrade() -> None:
    op.drop_table("evolution_change_log")
    op.drop_table("evolution_reports")
    op.drop_table("agent_evaluations")
