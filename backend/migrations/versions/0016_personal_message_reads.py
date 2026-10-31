"""新增个人阅读证据，不回填历史共享已读或推断收件人。"""
from alembic import op
import sqlalchemy as sa

revision = "0016_personal_message_reads"
down_revision = "0015_runtime_schema_expand"
branch_labels = None
depends_on = None
TABLE = "message_read_receipts"


def upgrade():
    connection = op.get_bind()
    inspector = sa.inspect(connection)
    if not inspector.has_table(TABLE):
        op.create_table(
            TABLE,
            sa.Column("tenant_id", sa.BigInteger(), sa.ForeignKey("tenants.id"), primary_key=True),
            sa.Column("message_id", sa.BigInteger(), sa.ForeignKey("resources.id"), primary_key=True),
            sa.Column("user_id", sa.BigInteger(), sa.ForeignKey("users.id"), primary_key=True),
            sa.Column("read_at", sa.DateTime(timezone=True), nullable=False),
        )
    inspector = sa.inspect(connection)
    columns = {column["name"]: column for column in inspector.get_columns(TABLE)}
    if set(columns) != {"tenant_id", "message_id", "user_id", "read_at"}:
        raise RuntimeError("Incompatible personal message receipt columns")
    if any(not isinstance(columns[name]["type"], sa.Integer) or columns[name]["nullable"]
           for name in ["tenant_id", "message_id", "user_id"]):
        raise RuntimeError("Incompatible personal message receipt ID fields")
    if connection.dialect.name != "sqlite" and any(
            not isinstance(columns[name]["type"], sa.BigInteger)
            for name in ["tenant_id", "message_id", "user_id"]):
        raise RuntimeError("Personal message receipt IDs require BIGINT")
    if not isinstance(columns["read_at"]["type"], sa.DateTime) or columns["read_at"]["nullable"]:
        raise RuntimeError("Incompatible personal message receipt timestamp")
    if set(inspector.get_pk_constraint(TABLE)["constrained_columns"]) != {"tenant_id", "message_id", "user_id"}:
        raise RuntimeError("Personal message receipt uniqueness is missing")
    foreign = {(tuple(item["constrained_columns"]), item["referred_table"], tuple(item["referred_columns"]))
               for item in inspector.get_foreign_keys(TABLE)}
    expected = {(("tenant_id",), "tenants", ("id",)), (("message_id",), "resources", ("id",)),
                (("user_id",), "users", ("id",))}
    if not expected <= foreign:
        raise RuntimeError("Personal message receipt foreign keys are missing")
    indexes = {item["name"]: item for item in inspector.get_indexes(TABLE)}
    name = "ix_message_read_receipts_tenant_user"
    if name not in indexes:
        op.create_index(name, TABLE, ["tenant_id", "user_id"])
    elif indexes[name]["column_names"] != ["tenant_id", "user_id"] or indexes[name]["unique"]:
        raise RuntimeError("Incompatible personal message receipt lookup index")


def downgrade():
    raise RuntimeError("Forward-only: preserve personal reading evidence")
