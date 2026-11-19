"""测评复杂任务体系：数据集用例增加复杂度/任务类型/所需工具/期望步数/多轮 turns，
结果行增加对应快照列与 snap_meta。

Revision ID: c7d8e9f0a1b2
Revises: f2a3b4c5d6e7
Create Date: 2026-11-19
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'c7d8e9f0a1b2'
down_revision: Union[str, None] = 'f2a3b4c5d6e7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_column(table: str, column: str) -> bool:
    bind = op.get_bind()
    cols = [c["name"] for c in sa.inspect(bind).get_columns(table)]
    return column in cols


def upgrade() -> None:
    # eval_dataset_items：复杂任务维度
    if not _has_column("eval_dataset_items", "complexity"):
        op.add_column("eval_dataset_items", sa.Column(
            "complexity", sa.String(16), nullable=False, server_default="L2"))
    if not _has_column("eval_dataset_items", "task_type"):
        op.add_column("eval_dataset_items", sa.Column(
            "task_type", sa.String(32), nullable=False, server_default="direct_qa"))
    if not _has_column("eval_dataset_items", "required_tools"):
        op.add_column("eval_dataset_items", sa.Column(
            "required_tools", sa.JSON(), nullable=True))
    if not _has_column("eval_dataset_items", "expected_min_steps"):
        op.add_column("eval_dataset_items", sa.Column(
            "expected_min_steps", sa.Integer(), nullable=True))
    if not _has_column("eval_dataset_items", "turns"):
        op.add_column("eval_dataset_items", sa.Column(
            "turns", sa.JSON(), nullable=True))

    # eval_run_items：快照列
    if not _has_column("eval_run_items", "snap_complexity"):
        op.add_column("eval_run_items", sa.Column(
            "snap_complexity", sa.String(16), nullable=False, server_default="L2"))
    if not _has_column("eval_run_items", "snap_task_type"):
        op.add_column("eval_run_items", sa.Column(
            "snap_task_type", sa.String(32), nullable=False, server_default="direct_qa"))
    if not _has_column("eval_run_items", "snap_meta"):
        op.add_column("eval_run_items", sa.Column(
            "snap_meta", sa.JSON(), nullable=True))


def downgrade() -> None:
    for col in ("snap_meta", "snap_task_type", "snap_complexity"):
        if _has_column("eval_run_items", col):
            op.drop_column("eval_run_items", col)
    for col in ("turns", "expected_min_steps", "required_tools", "task_type", "complexity"):
        if _has_column("eval_dataset_items", col):
            op.drop_column("eval_dataset_items", col)
