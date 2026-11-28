"""Run 暂停意图：Goal Runtime 用户主动暂停。

Revision ID: f4a5b6c7d8e9
Revises: e1f2a3b4c5d6
Create Date: 2026-11-28

说明：
- 暂停不是取消：Worker 在下一个模型调用安全边界收敛为 limit_exceeded，
  保留已完成结果与目标契约，可凭“继续”在新 Run 续做；
- 只新增 nullable/default 列，历史 Run 默认未请求暂停。
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "f4a5b6c7d8e9"
down_revision = "e1f2a3b4c5d6"
branch_labels = None
depends_on = None


def _has_column(table: str, name: str) -> bool:
    try:
        columns = sa.inspect(op.get_bind()).get_columns(table)
    except Exception:  # noqa: BLE001 表不存在
        return False
    return any(item.get("name") == name for item in columns)


def _has_fk(table: str, name: str) -> bool:
    try:
        return any(
            item.get("name") == name
            for item in sa.inspect(op.get_bind()).get_foreign_keys(table)
        )
    except Exception:  # noqa: BLE001
        return False


def upgrade() -> None:
    # SQLite 不支持 ALTER 添加外键；batch_alter_table 在 SQLite 走 copy-and-move，
    # 在 MySQL/PostgreSQL 仍是普通 ALTER。
    with op.batch_alter_table("agent_runs") as batch:
        if not _has_column("agent_runs", "pause_requested"):
            batch.add_column(
                sa.Column("pause_requested", sa.Boolean(), nullable=False,
                          server_default=sa.false()),
            )
        if not _has_column("agent_runs", "pause_requested_at"):
            batch.add_column(sa.Column("pause_requested_at", sa.DateTime(), nullable=True))
        if not _has_column("agent_runs", "pause_requested_by"):
            batch.add_column(sa.Column("pause_requested_by", sa.Integer(), nullable=True))
        if not _has_fk("agent_runs", "fk_agent_runs_pause_requested_by"):
            batch.create_foreign_key(
                "fk_agent_runs_pause_requested_by", "users",
                ["pause_requested_by"], ["id"],
            )


def downgrade() -> None:
    with op.batch_alter_table("agent_runs") as batch:
        if _has_fk("agent_runs", "fk_agent_runs_pause_requested_by"):
            batch.drop_constraint("fk_agent_runs_pause_requested_by", type_="foreignkey")
        for name in ("pause_requested_by", "pause_requested_at", "pause_requested"):
            if _has_column("agent_runs", name):
                batch.drop_column(name)
