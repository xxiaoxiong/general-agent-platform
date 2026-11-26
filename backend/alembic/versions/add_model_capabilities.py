"""模型能力矩阵（P2-03 §4.4）：结构化输出/视觉/并行工具调用 + 真实探测验证状态。

Revision ID: b0c1d2e3f4a5
Revises: a9b0c1d2e3f4
Create Date: 2026-11-13
"""
from alembic import op
import sqlalchemy as sa

revision = "b0c1d2e3f4a5"
down_revision = "a9b0c1d2e3f4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("model_configs") as batch_op:
        batch_op.add_column(sa.Column("supports_structured_output", sa.Boolean(), nullable=False, server_default=sa.false()))
        batch_op.add_column(sa.Column("supports_vision", sa.Boolean(), nullable=False, server_default=sa.false()))
        batch_op.add_column(sa.Column("supports_parallel_tool_calls", sa.Boolean(), nullable=False, server_default=sa.false()))
        batch_op.add_column(sa.Column("capability_status", sa.String(24), nullable=False, server_default="unverified"))
        batch_op.add_column(sa.Column("capability_verified_at", sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column("capability_probe_message", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("configuration_version", sa.Integer(), nullable=False, server_default=sa.text("1")))


def downgrade() -> None:
    with op.batch_alter_table("model_configs") as batch_op:
        batch_op.drop_column("configuration_version")
        batch_op.drop_column("capability_probe_message")
        batch_op.drop_column("capability_verified_at")
        batch_op.drop_column("capability_status")
        batch_op.drop_column("supports_parallel_tool_calls")
        batch_op.drop_column("supports_vision")
        batch_op.drop_column("supports_structured_output")
