"""研究来源正文改为 LONGTEXT：支持长文档分块读取与证据定位。

Revision ID: a6b7c8d9e0f1
Revises: f4a5b6c7d8e9
Create Date: 2026-11-28

说明：
- 长网页/PDF 需要保存全文供服务端证据 quote 定位，TEXT（64KB）不够；
- MySQL 使用 LONGTEXT；SQLite 的 TEXT 本身无 64KB 限制，无需变更。
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql


revision = "a6b7c8d9e0f1"
down_revision = "f4a5b6c7d8e9"
branch_labels = None
depends_on = None


def _is_mysql() -> bool:
    return op.get_bind().dialect.name == "mysql"


def upgrade() -> None:
    if not _is_mysql():
        return
    op.alter_column(
        "research_sources", "content_excerpt",
        existing_type=sa.Text(), type_=mysql.LONGTEXT(), existing_nullable=True,
    )


def downgrade() -> None:
    if not _is_mysql():
        return
    op.alter_column(
        "research_sources", "content_excerpt",
        existing_type=mysql.LONGTEXT(), type_=sa.Text(), existing_nullable=True,
    )
