"""Add evaluation tables: eval_datasets（测评数据集）, eval_dataset_items（数据集用例）,
eval_runs（测评任务）, eval_run_items（测评结果）.

Agent 测评模块：数据集 + 按数据集的测评结果体系。
- eval_datasets / eval_dataset_items：测评数据集与用例（含社区通用基准导入）；
- eval_runs / eval_run_items：批量测评任务与逐用例结果（快照 + 评分 + 运行统计）。

新表与 app/models/evaluation.py 保持一致；
开发环境由 init_db()/create_all 自动补齐，生产环境由本迁移创建。

Revision ID: f2a3b4c5d6e7
Revises: b8c9d0e1f2a3
Create Date: 2026-11-18 21:30:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f2a3b4c5d6e7'
down_revision: Union[str, None] = 'b8c9d0e1f2a3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_table(name: str) -> bool:
    bind = op.get_bind()
    return sa.inspect(bind).has_table(name)


def upgrade() -> None:
    if not _has_table('eval_datasets'):
        op.create_table(
            'eval_datasets',
            sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
            sa.Column('name', sa.String(length=120), nullable=False),
            sa.Column('description', sa.Text(), nullable=True),
            sa.Column('source', sa.String(length=32), nullable=False),
            sa.Column('category', sa.String(length=64), nullable=True),
            sa.Column('scoring_method', sa.String(length=24), nullable=False),
            sa.Column('reference', sa.String(length=500), nullable=True),
            sa.Column('created_by', sa.Integer(), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=True),
            sa.Column('updated_at', sa.DateTime(), nullable=True),
            sa.ForeignKeyConstraint(['created_by'], ['users.id']),
            sa.PrimaryKeyConstraint('id'),
        )
        op.create_index('ix_eval_datasets_name', 'eval_datasets', ['name'], unique=True)
        op.create_index('ix_eval_datasets_id', 'eval_datasets', ['id'], unique=False)

    if not _has_table('eval_dataset_items'):
        op.create_table(
            'eval_dataset_items',
            sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
            sa.Column('dataset_id', sa.Integer(), nullable=False),
            sa.Column('position', sa.Integer(), nullable=False),
            sa.Column('input', sa.Text(), nullable=False),
            sa.Column('expected', sa.Text(), nullable=True),
            sa.Column('criteria', sa.Text(), nullable=True),
            sa.Column('scoring_method', sa.String(length=24), nullable=True),
            sa.Column('scoring_config', sa.JSON(), nullable=True),
            sa.Column('metadata', sa.JSON(), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=True),
            sa.Column('updated_at', sa.DateTime(), nullable=True),
            sa.ForeignKeyConstraint(['dataset_id'], ['eval_datasets.id']),
            sa.PrimaryKeyConstraint('id'),
        )
        op.create_index('ix_eval_dataset_items_id', 'eval_dataset_items', ['id'], unique=False)
        op.create_index('ix_eval_dataset_items_dataset_id', 'eval_dataset_items', ['dataset_id'], unique=False)
        op.create_index('ix_eval_dataset_item_dataset_pos', 'eval_dataset_items', ['dataset_id', 'position'], unique=False)

    if not _has_table('eval_runs'):
        op.create_table(
            'eval_runs',
            sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
            sa.Column('name', sa.String(length=160), nullable=False),
            sa.Column('dataset_id', sa.Integer(), nullable=False),
            sa.Column('agent_id', sa.Integer(), nullable=False),
            sa.Column('agent_version_id', sa.Integer(), nullable=True),
            sa.Column('status', sa.String(length=24), nullable=False),
            sa.Column('config', sa.JSON(), nullable=True),
            sa.Column('aggregate', sa.JSON(), nullable=True),
            sa.Column('error', sa.Text(), nullable=True),
            sa.Column('created_by', sa.Integer(), nullable=True),
            sa.Column('started_at', sa.DateTime(), nullable=True),
            sa.Column('finished_at', sa.DateTime(), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=True),
            sa.Column('updated_at', sa.DateTime(), nullable=True),
            sa.ForeignKeyConstraint(['dataset_id'], ['eval_datasets.id']),
            sa.ForeignKeyConstraint(['agent_id'], ['agents.id']),
            sa.ForeignKeyConstraint(['agent_version_id'], ['agent_versions.id']),
            sa.ForeignKeyConstraint(['created_by'], ['users.id']),
            sa.PrimaryKeyConstraint('id'),
        )
        op.create_index('ix_eval_runs_id', 'eval_runs', ['id'], unique=False)
        op.create_index('ix_eval_runs_dataset_id', 'eval_runs', ['dataset_id'], unique=False)
        op.create_index('ix_eval_runs_agent_id', 'eval_runs', ['agent_id'], unique=False)
        op.create_index('ix_eval_runs_status', 'eval_runs', ['status'], unique=False)
        op.create_index('ix_eval_run_dataset_created', 'eval_runs', ['dataset_id', 'created_at'], unique=False)

    if not _has_table('eval_run_items'):
        op.create_table(
            'eval_run_items',
            sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
            sa.Column('eval_run_id', sa.Integer(), nullable=False),
            sa.Column('dataset_item_id', sa.Integer(), nullable=True),
            sa.Column('position', sa.Integer(), nullable=False),
            sa.Column('snap_input', sa.Text(), nullable=False),
            sa.Column('snap_expected', sa.Text(), nullable=True),
            sa.Column('snap_criteria', sa.Text(), nullable=True),
            sa.Column('snap_scoring_method', sa.String(length=24), nullable=False),
            sa.Column('snap_scoring_config', sa.JSON(), nullable=True),
            sa.Column('run_id', sa.String(length=36), nullable=True),
            sa.Column('status', sa.String(length=16), nullable=False),
            sa.Column('actual_output', sa.Text(), nullable=True),
            sa.Column('error', sa.Text(), nullable=True),
            sa.Column('score', sa.Float(), nullable=True),
            sa.Column('passed', sa.Boolean(), nullable=True),
            sa.Column('score_detail', sa.JSON(), nullable=True),
            sa.Column('stats', sa.JSON(), nullable=True),
            sa.Column('started_at', sa.DateTime(), nullable=True),
            sa.Column('finished_at', sa.DateTime(), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=True),
            sa.Column('updated_at', sa.DateTime(), nullable=True),
            sa.ForeignKeyConstraint(['eval_run_id'], ['eval_runs.id']),
            sa.ForeignKeyConstraint(['dataset_item_id'], ['eval_dataset_items.id']),
            sa.PrimaryKeyConstraint('id'),
        )
        op.create_index('ix_eval_run_items_id', 'eval_run_items', ['id'], unique=False)
        op.create_index('ix_eval_run_items_eval_run_id', 'eval_run_items', ['eval_run_id'], unique=False)
        op.create_index('ix_eval_run_items_run_id', 'eval_run_items', ['run_id'], unique=False)
        op.create_index('ix_eval_run_items_status', 'eval_run_items', ['status'], unique=False)
        op.create_index('ix_eval_run_item_run_pos', 'eval_run_items', ['eval_run_id', 'position'], unique=False)


def downgrade() -> None:
    # 从子表向父表删除。MySQL 会随表移除索引，先删索引可能破坏仍生效的外键。
    for table in ('eval_run_items', 'eval_runs', 'eval_dataset_items', 'eval_datasets'):
        if _has_table(table):
            op.drop_table(table)
