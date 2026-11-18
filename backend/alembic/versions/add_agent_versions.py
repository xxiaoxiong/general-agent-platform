"""agent_versions（P2-01 不可变发布版本）+ agent_runs.agent_version_id 绑定。

Run 创建时绑定不可变版本快照，执行期只读；存量 Run 的 agent_version_id 为空
（无版本绑定的历史数据，不影响执行与恢复）。

Revision ID: a9b0c1d2e3f4
Revises: e8f9a0b1c2d3
Create Date: 2026-11-13
"""
from alembic import op
import sqlalchemy as sa

revision = "a9b0c1d2e3f4"
down_revision = "e8f9a0b1c2d3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "agent_versions",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("agent_id", sa.Integer(), sa.ForeignKey("agents.id"), nullable=False, index=True),
        sa.Column("version_no", sa.Integer(), nullable=False),
        sa.Column("snapshot", sa.JSON(), nullable=False),
        sa.Column("created_by", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("published_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("agent_id", "version_no", name="uq_agent_versions_no"),
    )
    with op.batch_alter_table("agent_runs") as batch_op:
        batch_op.add_column(sa.Column("agent_version_id", sa.Integer(), nullable=True))
        batch_op.create_index("ix_agent_runs_agent_version_id", ["agent_version_id"])


def downgrade() -> None:
    with op.batch_alter_table("agent_runs") as batch_op:
        batch_op.drop_index("ix_agent_runs_agent_version_id")
        batch_op.drop_column("agent_version_id")
    op.drop_table("agent_versions")
