"""持久化会话置顶与手动标题。

Revision ID: b3a51d0e24cf
Revises: e4d9074233fa
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.mysql import DATETIME

revision = "b3a51d0e24cf"
down_revision = "e4d9074233fa"
branch_labels = None
depends_on = None

_TABLE = "agent_sessions"
_INDEX = "ix_agent_sessions_user_pinned_updated"


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    columns = {column["name"] for column in inspector.get_columns(_TABLE)}
    if "title_edited" not in columns:
        op.add_column(_TABLE, sa.Column("title_edited", sa.Boolean(), nullable=False, server_default=sa.false()))
    if "pinned_at" not in columns:
        stamp = sa.DateTime().with_variant(DATETIME(fsp=6), "mysql", "mariadb")
        op.add_column(_TABLE, sa.Column("pinned_at", stamp, nullable=True))
    if not any(index["name"] == _INDEX for index in inspector.get_indexes(_TABLE)):
        op.create_index(_INDEX, _TABLE, ["user_id", "pinned_at", "updated_at", "id"])


def downgrade() -> None:
    op.drop_index(_INDEX, table_name=_TABLE)
    op.drop_column(_TABLE, "pinned_at")
    op.drop_column(_TABLE, "title_edited")
