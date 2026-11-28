"""Goal Runtime 与 Deep Research：目标契约、迭代、研究计划/来源/证据/主张。

Revision ID: e1f2a3b4c5d6
Revises: c8d9e0f1a2b3
Create Date: 2026-11-28

说明：
- ``goal_contracts`` 与 ``goal_iterations`` 是 Goal Runtime 的持久化事实来源；
- ``research_plans`` / ``research_sources`` / ``research_evidence`` /
  ``research_claims`` 组成 Source → Evidence → Claim 的可追溯链；
- 历史 Run 不回溯建目标/研究行；新表由 ``init_db`` 在非生产环境自动补齐。
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "e1f2a3b4c5d6"
down_revision = "c8d9e0f1a2b3"
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
    if not _has_table("goal_contracts"):
        op.create_table(
            "goal_contracts",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.Column("goal_id", sa.String(length=36), nullable=False),
            sa.Column("run_id", sa.String(length=36), nullable=False),
            sa.Column("origin_run_id", sa.String(length=36), nullable=True),
            sa.Column("session_id", sa.Integer(), nullable=True),
            sa.Column("user_id", sa.Integer(), nullable=False),
            sa.Column("agent_id", sa.Integer(), nullable=True),
            sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("objective", sa.Text(), nullable=False),
            sa.Column("acceptance_criteria", sa.JSON(), nullable=False),
            sa.Column("constraints", sa.JSON(), nullable=False),
            sa.Column("deliverables", sa.JSON(), nullable=False),
            sa.Column("stop_conditions", sa.JSON(), nullable=False),
            sa.Column("revisions", sa.JSON(), nullable=False),
            sa.Column("status", sa.String(length=24), nullable=False, server_default="active"),
            sa.Column("iterations", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("progress", sa.JSON(), nullable=False),
            sa.Column("last_iteration_at", sa.DateTime(), nullable=True),
            sa.Column("finished_at", sa.DateTime(), nullable=True),
            sa.ForeignKeyConstraint(["run_id"], ["agent_runs.id"]),
            sa.ForeignKeyConstraint(["session_id"], ["agent_sessions.id"]),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
            sa.UniqueConstraint("goal_id", name="uq_goal_contracts_goal"),
        )
    for name, cols in (
        ("ix_goal_contracts_run", ["run_id"]),
        ("ix_goal_contracts_status", ["status"]),
        ("ix_goal_contracts_session_status", ["session_id", "status"]),
        ("ix_goal_contracts_user_created", ["user_id", "created_at"]),
    ):
        if not _has_index("goal_contracts", name):
            op.create_index(name, "goal_contracts", cols)

    if not _has_table("goal_iterations"):
        op.create_table(
            "goal_iterations",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.Column("goal_id", sa.String(length=36), nullable=False),
            sa.Column("run_id", sa.String(length=36), nullable=False),
            sa.Column("iteration_no", sa.Integer(), nullable=False),
            sa.Column("status", sa.String(length=24), nullable=False, server_default="running"),
            sa.Column("executor", sa.String(length=32), nullable=False, server_default="deep_agent"),
            sa.Column("plan", sa.JSON(), nullable=False),
            sa.Column("result", sa.JSON(), nullable=False),
            sa.Column("evidence_refs", sa.JSON(), nullable=False),
            sa.Column("stall_signature", sa.String(length=64), nullable=True),
            sa.Column("started_at", sa.DateTime(), nullable=True),
            sa.Column("finished_at", sa.DateTime(), nullable=True),
            sa.ForeignKeyConstraint(["goal_id"], ["goal_contracts.goal_id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["run_id"], ["agent_runs.id"]),
            sa.UniqueConstraint("goal_id", "iteration_no", name="uq_goal_iteration_no"),
        )
    for name, cols in (
        ("ix_goal_iterations_goal_id", ["goal_id"]),
        ("ix_goal_iterations_run", ["run_id"]),
    ):
        if not _has_index("goal_iterations", name):
            op.create_index(name, "goal_iterations", cols)

    if not _has_table("research_plans"):
        op.create_table(
            "research_plans",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.Column("research_id", sa.String(length=36), nullable=False),
            sa.Column("goal_id", sa.String(length=36), nullable=True),
            sa.Column("run_id", sa.String(length=36), nullable=False),
            sa.Column("session_id", sa.Integer(), nullable=True),
            sa.Column("user_id", sa.Integer(), nullable=False),
            sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("topic", sa.Text(), nullable=False),
            sa.Column("scope", sa.JSON(), nullable=False),
            sa.Column("questions", sa.JSON(), nullable=False),
            sa.Column("report_outline", sa.JSON(), nullable=False),
            sa.Column("status", sa.String(length=24), nullable=False, server_default="planning"),
            sa.Column("revisions", sa.JSON(), nullable=False),
            sa.Column("finished_at", sa.DateTime(), nullable=True),
            sa.UniqueConstraint("research_id", name="uq_research_plans_research"),
        )
    for name, cols in (
        ("ix_research_plans_goal", ["goal_id"]),
        ("ix_research_plans_run", ["run_id"]),
        ("ix_research_plans_session", ["session_id", "status"]),
    ):
        if not _has_index("research_plans", name):
            op.create_index(name, "research_plans", cols)

    if not _has_table("research_sources"):
        op.create_table(
            "research_sources",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.Column("research_id", sa.String(length=36), nullable=False),
            sa.Column("source_id", sa.String(length=40), nullable=False),
            sa.Column("url", sa.Text(), nullable=False),
            sa.Column("title", sa.Text(), nullable=False),
            sa.Column("source_type", sa.String(length=32), nullable=False, server_default="other"),
            sa.Column("author", sa.String(length=200), nullable=True),
            sa.Column("published_at", sa.String(length=64), nullable=True),
            sa.Column("retrieved_at", sa.DateTime(), nullable=False),
            sa.Column("domain", sa.String(length=255), nullable=True),
            sa.Column("http_status", sa.Integer(), nullable=True),
            sa.Column("content_hash", sa.String(length=64), nullable=True),
            sa.Column("content_location", sa.String(length=500), nullable=True),
            sa.Column("content_excerpt", sa.Text(), nullable=True),
            sa.Column("status", sa.String(length=24), nullable=False, server_default="discovered"),
            sa.UniqueConstraint("research_id", "source_id", name="uq_research_source"),
        )
    if not _has_index("research_sources", "ix_research_sources_research"):
        op.create_index("ix_research_sources_research", "research_sources", ["research_id"])

    if not _has_table("research_evidence"):
        op.create_table(
            "research_evidence",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.Column("research_id", sa.String(length=36), nullable=False),
            sa.Column("evidence_id", sa.String(length=40), nullable=False),
            sa.Column("source_id", sa.String(length=40), nullable=False),
            sa.Column("question_id", sa.String(length=40), nullable=True),
            sa.Column("quote", sa.Text(), nullable=False),
            sa.Column("locator", sa.String(length=500), nullable=True),
            sa.Column("status", sa.String(length=24), nullable=False, server_default="extracted"),
            sa.Column("extracted_at", sa.DateTime(), nullable=False),
            sa.UniqueConstraint("research_id", "evidence_id", name="uq_research_evidence"),
        )
    for name, cols in (
        ("ix_research_evidence_research", ["research_id"]),
        ("ix_research_evidence_question", ["research_id", "question_id"]),
    ):
        if not _has_index("research_evidence", name):
            op.create_index(name, "research_evidence", cols)

    if not _has_table("research_claims"):
        op.create_table(
            "research_claims",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.Column("research_id", sa.String(length=36), nullable=False),
            sa.Column("claim_id", sa.String(length=40), nullable=False),
            sa.Column("question_id", sa.String(length=40), nullable=True),
            sa.Column("statement", sa.Text(), nullable=False),
            sa.Column("status", sa.String(length=24), nullable=False, server_default="pending"),
            sa.Column("support_evidence", sa.JSON(), nullable=False),
            sa.Column("contradict_evidence", sa.JSON(), nullable=False),
            sa.Column("verification", sa.JSON(), nullable=False),
            sa.UniqueConstraint("research_id", "claim_id", name="uq_research_claim"),
        )
    for name, cols in (
        ("ix_research_claims_research", ["research_id"]),
        ("ix_research_claims_question", ["research_id", "question_id"]),
    ):
        if not _has_index("research_claims", name):
            op.create_index(name, "research_claims", cols)


def downgrade() -> None:
    for table in ("research_claims", "research_evidence", "research_sources",
                  "research_plans", "goal_iterations", "goal_contracts"):
        if _has_table(table):
            op.drop_table(table)
