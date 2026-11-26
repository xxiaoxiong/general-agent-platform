"""目标模式执行中补充要求：agent_run_steering 表。

Revision ID: d8e9f0a1b2c3
Revises: c7a20d4be621
Create Date: 2026-11-25
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'd8e9f0a1b2c3'
down_revision: Union[str, None] = 'c7a20d4be621'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 开发环境可能已由 init_db()/create_all 建表：存在即跳过，保证迁移可重复执行
    if sa.inspect(op.get_bind()).has_table("agent_run_steering"):
        return
    op.create_table(
        "agent_run_steering",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("run_id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("message_id", sa.String(length=64), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("delivered_at", sa.DateTime(), nullable=True),
        sa.Column("delivered_via", sa.String(length=32), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("message_id", name="uq_agent_run_steering_message"),
    )
    op.create_index("ix_run_steering_run_id", "agent_run_steering", ["run_id", "id"], unique=False)


def downgrade() -> None:
    if not sa.inspect(op.get_bind()).has_table("agent_run_steering"):
        return
    op.drop_index("ix_run_steering_run_id", table_name="agent_run_steering")
    op.drop_table("agent_run_steering")
