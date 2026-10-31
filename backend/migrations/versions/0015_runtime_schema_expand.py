"""Expand missing runtime fields without claiming legacy task ownership."""
from alembic import op
import sqlalchemy as sa

revision = "0015_runtime_schema_expand"
down_revision = "0014_transactional_outbox"
branch_labels = None
depends_on = None


def upgrade() -> None:
    connection = op.get_bind()
    inspector = sa.inspect(connection)
    # 先核验两个表，遇到不兼容旧列时不先写入另一张表。
    users = {column["name"]: column for column in inspector.get_columns("users")}
    tasks = {column["name"]: column for column in inspector.get_columns("scheduled_tasks")}
    approval = users.get("approval_status")
    owner = tasks.get("owner_id")
    if approval is not None:
        column_type = approval["type"]
        if not isinstance(column_type, sa.String) or (column_type.length or 0) < 20:
            raise RuntimeError("Incompatible users.approval_status; manual review required")
    if owner is not None:
        compatible = isinstance(owner["type"], sa.BigInteger)
        if connection.dialect.name == "sqlite":
            compatible = isinstance(owner["type"], sa.Integer)
        if not compatible or not owner["nullable"]:
            raise RuntimeError("Incompatible scheduled_tasks.owner_id; manual review required")
    indexes = {item["name"]: item for item in inspector.get_indexes("scheduled_tasks")}
    index = indexes.get("ix_scheduled_tasks_owner_id")
    if index is not None and (index["column_names"] != ["owner_id"] or index["unique"]):
        raise RuntimeError("Incompatible scheduled task owner index; manual review required")
    if approval is None:
        op.add_column("users", sa.Column(
            "approval_status", sa.String(20), nullable=False, server_default="approved",
        ))
    if owner is None:
        op.add_column("scheduled_tasks", sa.Column("owner_id", sa.BigInteger(), nullable=True))
    if index is None:
        op.create_index("ix_scheduled_tasks_owner_id", "scheduled_tasks", ["owner_id"])


def downgrade() -> None:
    raise RuntimeError("Forward-only: retain expanded fields and legacy approval/ownership data")
