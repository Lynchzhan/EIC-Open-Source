"""共享熔断及准入预算；不存储密钥、URL 或模型请求正文。"""
from alembic import op
import sqlalchemy as sa

revision = "0025_dependency_controls"
down_revision = "0024_memory_scopes"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    # 历史迁移不导入可继续演进的 ORM 定义。
    metadata = sa.MetaData()
    tables = [
        sa.Table("dependency_circuits", metadata,
                 sa.Column("key", sa.String(64), primary_key=True),
                 sa.Column("state", sa.String(16), nullable=False),
                 sa.Column("failures", sa.Integer(), nullable=False),
                 sa.Column("window_started", sa.DateTime(timezone=True), nullable=False),
                 sa.Column("open_until", sa.DateTime(timezone=True)),
                 sa.Column("probe_token", sa.String(64)),
                 sa.Column("probe_until", sa.DateTime(timezone=True)),
                 sa.Column("epoch", sa.Integer(), nullable=False)),
        sa.Table("admission_windows", metadata,
                 sa.Column("key", sa.String(64), primary_key=True),
                 sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
                 sa.Column("requests", sa.Integer(), nullable=False),
                 sa.Column("reserved_units", sa.BigInteger(), nullable=False)),
        sa.Table("admission_leases", metadata,
                 sa.Column("token", sa.String(64), primary_key=True),
                 sa.Column("tenant_id", sa.BigInteger(), nullable=False, index=True),
                 sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False, index=True),
                 sa.Column("finished", sa.Boolean(), nullable=False)),
    ]
    for table in tables:
        inspector = sa.inspect(bind)
        if not inspector.has_table(table.name):
            table.create(bind)
        else:
            columns = {item["name"]: item for item in inspector.get_columns(table.name)}
            if set(columns) != set(table.columns.keys()):
                raise RuntimeError("Incompatible dependency control table: " + table.name)
            for column in table.columns:
                actual = columns[column.name]
                if (actual["type"]._type_affinity != column.type._type_affinity
                        or actual["nullable"] != column.nullable):
                    raise RuntimeError("Incompatible dependency control column: " + column.name)
            if inspector.get_pk_constraint(table.name)["constrained_columns"] != [table.primary_key.columns.keys()[0]]:
                raise RuntimeError("Incompatible dependency control primary key")


def downgrade():
    raise RuntimeError("Forward-only: restore the protected database backup for rollback")
