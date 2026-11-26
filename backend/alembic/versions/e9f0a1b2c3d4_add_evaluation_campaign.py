"""Agent 测评系统重构：Run 来源、Suite/Campaign/Trial、Verifier/Judge 评分表。

Revision ID: e9f0a1b2c3d4
Revises: d8e9f0a1b2c3
Create Date: 2026-11-26
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "e9f0a1b2c3d4"
down_revision: Union[str, None] = "d8e9f0a1b2c3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_column(table: str, column: str) -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if table not in inspector.get_table_names():
        return False
    return column in [c["name"] for c in inspector.get_columns(table)]


def _has_table(table: str) -> bool:
    return table in sa.inspect(op.get_bind()).get_table_names()


def upgrade() -> None:
    # ── AgentRun 来源标记（Benchmark 与生产运行隔离）──
    if not _has_column("agent_runs", "source"):
        op.add_column("agent_runs", sa.Column(
            "source", sa.String(32), nullable=False, server_default="chat"))
    if _has_column("agent_runs", "source"):
        indexes = [i["name"] for i in sa.inspect(op.get_bind()).get_indexes("agent_runs")]
        if "ix_agent_runs_source" not in indexes:
            op.create_index("ix_agent_runs_source", "agent_runs", ["source"])

    # ── 测评套件 ──
    if not _has_table("eval_suites"):
        op.create_table(
            "eval_suites",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("name", sa.String(120), nullable=False, unique=True),
            sa.Column("description", sa.Text(), nullable=True),
            sa.Column("created_by", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
            sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        )
        op.create_index("ix_eval_suites_name", "eval_suites", ["name"], unique=True)

    if not _has_table("eval_suite_datasets"):
        op.create_table(
            "eval_suite_datasets",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("suite_id", sa.Integer(), sa.ForeignKey("eval_suites.id"), nullable=False),
            sa.Column("dataset_id", sa.Integer(), sa.ForeignKey("eval_datasets.id"), nullable=False),
            sa.Column("position", sa.Integer(), nullable=False, server_default="0"),
        )
        op.create_index("ix_eval_suite_dataset_suite_pos", "eval_suite_datasets",
                        ["suite_id", "position"])

    if not _has_table("eval_suite_versions"):
        op.create_table(
            "eval_suite_versions",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("suite_id", sa.Integer(), sa.ForeignKey("eval_suites.id"), nullable=False),
            sa.Column("version_no", sa.Integer(), nullable=False),
            sa.Column("snapshot", sa.JSON(), nullable=False),
            sa.Column("created_by", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
            sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        )
        op.create_index("ix_eval_suite_version_suite_no", "eval_suite_versions",
                        ["suite_id", "version_no"], unique=True)

    # ── EvalRun（Campaign）扩展 ──
    run_columns = (
        ("suite_id", sa.Integer()),
        ("suite_version_id", sa.Integer()),
        ("trials_per_case", sa.Integer()),
        ("environment", sa.JSON()),
        ("cancel_requested", sa.Boolean()),
        ("worker_id", sa.String(128)),
        ("lease_expires_at", sa.DateTime()),
        ("heartbeat_at", sa.DateTime()),
        ("lease_version", sa.Integer()),
        ("attempt", sa.Integer()),
    )
    for name, type_ in run_columns:
        if not _has_column("eval_runs", name):
            kwargs = {}
            if name == "trials_per_case":
                kwargs["server_default"] = "1"
            elif name == "cancel_requested":
                kwargs["server_default"] = sa.false()
            elif name in ("lease_version", "attempt"):
                kwargs["server_default"] = "0"
            nullable = name in ("suite_id", "suite_version_id", "environment",
                                "worker_id", "lease_expires_at", "heartbeat_at")
            op.add_column("eval_runs", sa.Column(
                name, type_, nullable=nullable, **kwargs))
    run_indexes = [i["name"] for i in sa.inspect(op.get_bind()).get_indexes("eval_runs")]
    if "ix_eval_runs_suite_id" not in run_indexes:
        op.create_index("ix_eval_runs_suite_id", "eval_runs", ["suite_id"])
    if "ix_eval_runs_suite_version_id" not in run_indexes:
        op.create_index("ix_eval_runs_suite_version_id", "eval_runs", ["suite_version_id"])
    if "ix_eval_run_status_lease" not in run_indexes:
        op.create_index("ix_eval_run_status_lease", "eval_runs",
                        ["status", "lease_expires_at"])

    # ── EvalRunItem（Trial）扩展 ──
    item_columns = (
        ("dataset_id", sa.Integer(), True),
        ("trial_index", sa.Integer(), False),
        ("attempt", sa.Integer(), False),
        ("execution_status", sa.String(24), False),
        ("verification_status", sa.String(16), False),
        ("grading_status", sa.String(16), False),
        ("outcome", sa.String(24), False),
        ("snap_capability", sa.String(32), True),
    )
    for name, type_, nullable in item_columns:
        if not _has_column("eval_run_items", name):
            kwargs = {}
            if name in ("trial_index", "attempt"):
                kwargs["server_default"] = "0"
            elif name == "execution_status":
                kwargs["server_default"] = "pending"
            elif name in ("verification_status", "grading_status", "outcome"):
                kwargs["server_default"] = "pending"
            op.add_column("eval_run_items", sa.Column(
                name, type_, nullable=nullable, **kwargs))
    item_indexes = [i["name"] for i in sa.inspect(op.get_bind()).get_indexes("eval_run_items")]
    if "ix_eval_run_items_outcome" not in item_indexes:
        op.create_index("ix_eval_run_items_outcome", "eval_run_items", ["outcome"])
    if "ix_eval_run_item_case_trial" not in item_indexes:
        op.create_index("ix_eval_run_item_case_trial", "eval_run_items",
                        ["eval_run_id", "dataset_item_id", "trial_index"])

    # ── 数据集用例：能力维度 ──
    if not _has_column("eval_dataset_items", "capability"):
        op.add_column("eval_dataset_items", sa.Column(
            "capability", sa.String(32), nullable=True))

    # ── 评分记录（Verifier / Judge 历史）──
    if not _has_table("eval_grades"):
        op.create_table(
            "eval_grades",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("trial_id", sa.Integer(), sa.ForeignKey("eval_run_items.id"),
                      nullable=False),
            sa.Column("kind", sa.String(16), nullable=False, server_default="verifier"),
            sa.Column("name", sa.String(64), nullable=False, server_default=""),
            sa.Column("status", sa.String(16), nullable=False, server_default="unscored"),
            sa.Column("score", sa.Float(), nullable=True),
            sa.Column("passed", sa.Boolean(), nullable=True),
            sa.Column("detail", sa.JSON(), nullable=True),
            sa.Column("evidence", sa.JSON(), nullable=True),
            sa.Column("model_config_id", sa.Integer(), nullable=True),
            sa.Column("prompt_version", sa.String(32), nullable=True),
            sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        )
        op.create_index("ix_eval_grades_trial_id", "eval_grades", ["trial_id"])
        op.create_index("ix_eval_grade_trial_kind", "eval_grades", ["trial_id", "kind"])


def downgrade() -> None:
    def _indexes(table: str) -> list:
        if not _has_table(table):
            return []
        return [i["name"] for i in sa.inspect(op.get_bind()).get_indexes(table)]

    if _has_table("eval_grades"):
        op.drop_table("eval_grades")
    if _has_column("eval_dataset_items", "capability"):
        op.drop_column("eval_dataset_items", "capability")
    # 先删索引再删列（SQLite 不允许悬空索引引用被删除的列）
    item_indexes = _indexes("eval_run_items")
    for index_name in ("ix_eval_run_items_outcome", "ix_eval_run_item_case_trial"):
        if index_name in item_indexes:
            op.drop_index(index_name, table_name="eval_run_items")
    for name in ("snap_capability", "outcome", "grading_status",
                 "verification_status", "execution_status", "attempt",
                 "trial_index", "dataset_id"):
        if _has_column("eval_run_items", name):
            op.drop_column("eval_run_items", name)
    run_indexes = _indexes("eval_runs")
    for index_name in ("ix_eval_runs_suite_id", "ix_eval_runs_suite_version_id",
                       "ix_eval_run_status_lease"):
        if index_name in run_indexes:
            op.drop_index(index_name, table_name="eval_runs")
    for name in ("attempt", "lease_version", "heartbeat_at", "lease_expires_at",
                 "worker_id", "cancel_requested", "environment",
                 "trials_per_case", "suite_version_id", "suite_id"):
        if _has_column("eval_runs", name):
            op.drop_column("eval_runs", name)
    if _has_table("eval_suite_versions"):
        op.drop_table("eval_suite_versions")
    if _has_table("eval_suite_datasets"):
        op.drop_table("eval_suite_datasets")
    if _has_table("eval_suites"):
        op.drop_table("eval_suites")
    if "ix_agent_runs_source" in _indexes("agent_runs"):
        op.drop_index("ix_agent_runs_source", table_name="agent_runs")
    if _has_column("agent_runs", "source"):
        op.drop_column("agent_runs", "source")
