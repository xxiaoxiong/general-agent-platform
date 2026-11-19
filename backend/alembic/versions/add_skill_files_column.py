"""技能管理：skills 表增加附属文件列 files（标准 Agent Skills 多文件形态）。

管理面技能由纯 SKILL.md 文本扩展为 SKILL.md + 附属文件
（scripts/、references/ 等，{相对 POSIX 路径: 文本内容}），与
app/models/skill.py 保持一致；开发环境由 init_db()/create_all 自动补齐，
生产环境由本迁移补列（存量行 files 为 NULL = 无附属文件，向后兼容）。

Revision ID: f3a4b5c6d7e8
Revises: c7d8e9f0a1b2
Create Date: 2026-11-19
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'f3a4b5c6d7e8'
down_revision: Union[str, None] = 'c7d8e9f0a1b2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_column(table: str, column: str) -> bool:
    bind = op.get_bind()
    cols = [c["name"] for c in sa.inspect(bind).get_columns(table)]
    return column in cols


def upgrade() -> None:
    if not _has_column("skills", "files"):
        op.add_column("skills", sa.Column("files", sa.JSON(), nullable=True))


def downgrade() -> None:
    if _has_column("skills", "files"):
        op.drop_column("skills", "files")
