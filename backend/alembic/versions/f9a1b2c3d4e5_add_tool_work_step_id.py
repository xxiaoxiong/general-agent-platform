"""Work Orchestrator：工具执行账本增加 work_step_id（nullable，向后兼容）。

Revision ID: f9a1b2c3d4e5
Revises: c3d4e5f6a7b8
Create Date: 2026-11-27

说明：
- 仅新增可空列与索引，旧 Run 的工具账本读取完全不受影响；
- 步骤级验收据此从 Ledger 精确取证（任务书 §8.4）。
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "f9a1b2c3d4e5"
down_revision: Union[str, None] = "c3d4e5f6a7b8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE = "agent_tool_executions"
_COLUMN = "work_step_id"
_INDEX = "ix_agent_tool_executions_work_step_id"


def _columns() -> set:
    return {item["name"] for item in sa.inspect(op.get_bind()).get_columns(_TABLE)}


def _indexes() -> set:
    return {item["name"] for item in sa.inspect(op.get_bind()).get_indexes(_TABLE)}


def upgrade() -> None:
    if _COLUMN not in _columns():
        op.add_column(_TABLE, sa.Column(_COLUMN, sa.String(40), nullable=True))
    if _INDEX not in _indexes():
        op.create_index(_INDEX, _TABLE, [_COLUMN], unique=False)


def downgrade() -> None:
    # SQLite 必须先删索引再删列
    if _INDEX in _indexes():
        op.drop_index(_INDEX, table_name=_TABLE)
    if _COLUMN in _columns():
        op.drop_column(_TABLE, _COLUMN)
