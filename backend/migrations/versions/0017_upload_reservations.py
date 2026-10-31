"""持久上传预占，不迁移文件、不配置业务配额或清理保留期。"""
from alembic import op
import sqlalchemy as sa

revision = "0017_upload_reservations"
down_revision = "0016_personal_message_reads"
branch_labels = None
depends_on = None
TABLE = "upload_reservations"


def upgrade():
    connection = op.get_bind()
    if not sa.inspect(connection).has_table(TABLE):
        op.create_table(
            TABLE,
            sa.Column("token", sa.String(64), primary_key=True),
            sa.Column("tenant_id", sa.BigInteger(), sa.ForeignKey("tenants.id"), nullable=False),
            sa.Column("reserved_bytes", sa.BigInteger(), nullable=False),
            sa.Column("file_count", sa.Integer(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        )
    inspector = sa.inspect(connection)
    columns = {item["name"]: item for item in inspector.get_columns(TABLE)}
    if set(columns) != {"token", "tenant_id", "reserved_bytes", "file_count", "created_at"}:
        raise RuntimeError("Incompatible upload reservation columns")
    if any(columns[name]["nullable"] for name in columns):
        raise RuntimeError("Upload reservation fields must be non-null")
    if not isinstance(columns["token"]["type"], sa.String) or columns["token"]["type"].length != 64:
        raise RuntimeError("Incompatible upload reservation token")
    if not isinstance(columns["created_at"]["type"], sa.DateTime):
        raise RuntimeError("Incompatible upload reservation timestamp")
    for name in ["tenant_id", "reserved_bytes", "file_count"]:
        if not isinstance(columns[name]["type"], sa.Integer):
            raise RuntimeError("Incompatible upload reservation numeric fields")
        if name != "file_count" and connection.dialect.name != "sqlite" and not isinstance(columns[name]["type"], sa.BigInteger):
            raise RuntimeError("Upload reservation size/tenant require BIGINT")
    if inspector.get_pk_constraint(TABLE)["constrained_columns"] != ["token"]:
        raise RuntimeError("Upload reservation uniqueness is missing")
    foreign = {(tuple(item["constrained_columns"]), item["referred_table"], tuple(item["referred_columns"]))
               for item in inspector.get_foreign_keys(TABLE)}
    if (("tenant_id",), "tenants", ("id",)) not in foreign:
        raise RuntimeError("Upload reservation tenant foreign key is missing")
    indexes = {item["name"]: item for item in inspector.get_indexes(TABLE)}
    name = "ix_upload_reservations_tenant_id"
    if name not in indexes:
        op.create_index(name, TABLE, ["tenant_id"])
    elif indexes[name]["column_names"] != ["tenant_id"] or indexes[name]["unique"]:
        raise RuntimeError("Incompatible upload reservation lookup index")


def downgrade():
    raise RuntimeError("Forward-only: preserve unresolved upload reservations")
