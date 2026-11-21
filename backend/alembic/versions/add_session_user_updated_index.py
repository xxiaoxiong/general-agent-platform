"""agent_sessions 增加 (user_id, updated_at) 复合索引

会话列表 WHERE user_id=? ORDER BY updated_at DESC 走索引序扫描，
消除 filesort（大 messages JSON 参与排序会撑爆 MySQL sort buffer 报 1038）。

Revision ID: d0e1f2a3b4c5
Revises: c1d2e3f4a5b6
"""
from alembic import op
import sqlalchemy as sa

revision = "d0e1f2a3b4c5"
down_revision = "c1d2e3f4a5b6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    existing = {ix["name"] for ix in sa.inspect(bind).get_indexes("agent_sessions")}
    if "ix_agent_sessions_user_updated" not in existing:
        op.create_index(
            "ix_agent_sessions_user_updated",
            "agent_sessions",
            ["user_id", "updated_at"],
        )


def downgrade() -> None:
    bind = op.get_bind()
    indexes = sa.inspect(bind).get_indexes("agent_sessions")
    composite = "ix_agent_sessions_user_updated"
    if not any(index["name"] == composite for index in indexes):
        return
    if bind.dialect.name == "mysql" and not any(
        index["name"] != composite and index.get("column_names", [])[:1] == ["user_id"]
        for index in indexes
    ):
        # InnoDB 可能用复合索引替换原隐式外键索引，回滚时先恢复覆盖 user_id 的索引。
        op.create_index("ix_agent_sessions_user_id", "agent_sessions", ["user_id"])
    op.drop_index(composite, table_name="agent_sessions")
