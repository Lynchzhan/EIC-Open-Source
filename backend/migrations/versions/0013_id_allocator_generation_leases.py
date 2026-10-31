"""Shared ID allocator and durable generation ownership.

Requires old API/worker writers to be stopped before upgrade.
"""
from datetime import UTC, datetime
import uuid

from alembic import op
import sqlalchemy as sa

revision = "0013_id_generation_leases"
down_revision = "0012_knowledge_eval_runs"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "id_allocator", sa.Column("name", sa.String(32), primary_key=True),
        sa.Column("value", sa.BigInteger(), nullable=False),
    )
    op.create_table(
        "generation_leases",
        sa.Column("message_id", sa.BigInteger(), primary_key=True),
        sa.Column("tenant_id", sa.BigInteger(), nullable=False),
        sa.Column("request_id", sa.String(80), nullable=False),
        sa.Column("owner_token", sa.String(64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("state", sa.String(32), nullable=False),
    )
    for column in ("tenant_id", "request_id", "expires_at"):
        op.create_index("ix_generation_leases_" + column, "generation_leases", [column])
    connection = op.get_bind()
    metadata = sa.MetaData()
    metadata.reflect(connection)
    high = 2_000_000_000_000_000 - 1
    for table in metadata.sorted_tables:
        column = table.c.get("id")
        if column is not None and isinstance(column.type, sa.Integer):
            high = max(high, connection.scalar(sa.select(sa.func.max(column))) or 0)
    if high >= 2**63 - 1:
        raise RuntimeError("Existing IDs exhaust signed 64-bit allocator")
    connection.execute(metadata.tables["id_allocator"].insert().values(name="global", value=high))
    # 旧 writer 已停止；遗留 generating 纳入过期恢复，不重发 provider。
    messages = metadata.tables["chat_messages"]
    now = connection.scalar(sa.select(sa.func.current_timestamp()))
    if isinstance(now, str):
        now = datetime.fromisoformat(now).replace(tzinfo=UTC)
    for row in connection.execute(sa.select(messages).where(messages.c.status == "generating")).mappings():
        connection.execute(metadata.tables["generation_leases"].insert().values(
            message_id=row["id"], tenant_id=row["tenant_id"], request_id=row["request_id"],
            owner_token=uuid.uuid4().hex, expires_at=now, state="active",
        ))


def downgrade():
    raise RuntimeError("Forward-only: dropping allocator can reuse IDs; restore application with compatible schema")
