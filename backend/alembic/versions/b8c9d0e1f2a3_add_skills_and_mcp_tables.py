"""Add extension-governance tables: skills（技能管理）, mcp_servers（MCP 管理）.

扩展性治理：技能管理模块 + MCP 管理模块。
- skills：管理面创建的技能（SKILL.md 内容 + Agent 级绑定 + 启停）；
- mcp_servers：MCP 服务连接配置、安全策略（fail-closed 默认需审批/禁重试）、
  工具发现缓存与 Agent 绑定。

新表与 app/models/skill.py、app/models/mcp_server.py 保持一致；
开发环境由 init_db()/create_all 自动补齐，生产环境由本迁移创建。

Revision ID: b8c9d0e1f2a3
Revises: a7b8c9d0e1f2
Create Date: 2026-11-18 20:10:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b8c9d0e1f2a3'
down_revision: Union[str, None] = 'a7b8c9d0e1f2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_table(name: str) -> bool:
    bind = op.get_bind()
    return sa.inspect(bind).has_table(name)


def upgrade() -> None:
    if not _has_table('skills'):
        op.create_table(
            'skills',
            sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
            sa.Column('name', sa.String(length=100), nullable=False),
            sa.Column('description', sa.Text(), nullable=True),
            sa.Column('version', sa.String(length=20), nullable=True),
            sa.Column('body', sa.Text(), nullable=False),
            sa.Column('enabled', sa.Boolean(), nullable=False),
            sa.Column('agent_ids', sa.JSON(), nullable=True),
            sa.Column('source', sa.String(length=32), nullable=False),
            sa.Column('created_by', sa.Integer(), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=True),
            sa.Column('updated_at', sa.DateTime(), nullable=True),
            sa.ForeignKeyConstraint(['created_by'], ['users.id']),
            sa.PrimaryKeyConstraint('id'),
        )
        op.create_index('ix_skills_name', 'skills', ['name'], unique=True)
        op.create_index('ix_skills_id', 'skills', ['id'], unique=False)

    if not _has_table('mcp_servers'):
        op.create_table(
            'mcp_servers',
            sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
            sa.Column('name', sa.String(length=100), nullable=False),
            sa.Column('description', sa.Text(), nullable=True),
            sa.Column('transport', sa.String(length=24), nullable=False),
            sa.Column('command', sa.String(length=255), nullable=True),
            sa.Column('args', sa.JSON(), nullable=True),
            sa.Column('env', sa.JSON(), nullable=True),
            sa.Column('url', sa.String(length=500), nullable=True),
            sa.Column('headers', sa.JSON(), nullable=True),
            sa.Column('enabled', sa.Boolean(), nullable=False),
            sa.Column('require_approval', sa.Boolean(), nullable=False),
            sa.Column('side_effect_class', sa.String(length=32), nullable=False),
            sa.Column('tool_settings', sa.JSON(), nullable=True),
            sa.Column('discovered_tools', sa.JSON(), nullable=True),
            sa.Column('agent_ids', sa.JSON(), nullable=True),
            sa.Column('last_test_at', sa.DateTime(), nullable=True),
            sa.Column('last_test_error', sa.Text(), nullable=True),
            sa.Column('created_by', sa.Integer(), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=True),
            sa.Column('updated_at', sa.DateTime(), nullable=True),
            sa.ForeignKeyConstraint(['created_by'], ['users.id']),
            sa.PrimaryKeyConstraint('id'),
        )
        op.create_index('ix_mcp_servers_name', 'mcp_servers', ['name'], unique=True)
        op.create_index('ix_mcp_servers_id', 'mcp_servers', ['id'], unique=False)


def downgrade() -> None:
    if _has_table('mcp_servers'):
        op.drop_index('ix_mcp_servers_id', table_name='mcp_servers')
        op.drop_index('ix_mcp_servers_name', table_name='mcp_servers')
        op.drop_table('mcp_servers')
    if _has_table('skills'):
        op.drop_index('ix_skills_id', table_name='skills')
        op.drop_index('ix_skills_name', table_name='skills')
        op.drop_table('skills')
