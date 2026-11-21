"""增加请求关联、审计快照和查询索引。"""

from alembic import op
import sqlalchemy as sa

revision = "c7a20d4be621"
down_revision = "b3a51d0e24cf"
branch_labels = None
depends_on = None

_TABLE = "audit_logs"
_FIELDS = (
    ("event_type", sa.String(20)), ("request_id", sa.String(64)),
    ("http_method", sa.String(10)), ("request_path", sa.String(300)), ("status_code", sa.Integer()),
    ("before_data", sa.JSON(none_as_null=True)), ("after_data", sa.JSON(none_as_null=True)),
    ("changed_fields", sa.JSON(none_as_null=True)),
)
_INDEXES = {
    "ix_audit_logs_request_id": ["request_id"],
    "ix_audit_logs_created_at": ["created_at"],
    "ix_audit_logs_resource": ["resource_type", "resource_id"],
}


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    columns = {column["name"] for column in inspector.get_columns(_TABLE)}
    for name, type_ in _FIELDS:
        if name not in columns:
            op.add_column(_TABLE, sa.Column(name, type_, nullable=True))
    indexes = {index["name"] for index in inspector.get_indexes(_TABLE)}
    for name, fields in _INDEXES.items():
        if name not in indexes:
            op.create_index(name, _TABLE, fields)


def downgrade() -> None:
    for name in _INDEXES:
        op.drop_index(name, table_name=_TABLE)
    for name, _ in reversed(_FIELDS):
        op.drop_column(_TABLE, name)
