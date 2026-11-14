"""agents 增加 capability_ids（能力包绑定）与 is_default（默认助手标记）。

P3-01：默认通用助手按用户懒创建并标记 is_default；
capability_ids 空 = 存量 Agent 保持显式 tools 语义，不影响现有行为。

Revision ID: c1d2e3f4a5b6
Revises: b0c1d2e3f4a5
Create Date: 2026-11-14
"""
from alembic import op
import sqlalchemy as sa

revision = "c1d2e3f4a5b6"
down_revision = "b0c1d2e3f4a5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("agents") as batch_op:
        batch_op.add_column(sa.Column("capability_ids", sa.JSON(), nullable=True))
        batch_op.add_column(
            sa.Column("is_default", sa.Boolean(), nullable=False, server_default=sa.text("0"))
        )


def downgrade() -> None:
    with op.batch_alter_table("agents") as batch_op:
        batch_op.drop_column("is_default")
        batch_op.drop_column("capability_ids")
