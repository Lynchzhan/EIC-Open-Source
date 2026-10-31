"""Add durable publish intents and consumption receipts, never rewrite legacy data."""
from alembic import op
import sqlalchemy as sa

revision = "0014_transactional_outbox"
down_revision = "0013_id_generation_leases"
branch_labels = None
depends_on = None


def upgrade():
    # 分离修订，不在 worker 启动中执行 DDL。
    connection = op.get_bind()
    if not sa.inspect(connection).has_table("outbox_events"):
        op.create_table(
            "outbox_events",
            sa.Column("id", sa.BigInteger(), primary_key=True),
            sa.Column("tenant_id", sa.BigInteger(), sa.ForeignKey("tenants.id"), nullable=False),
            sa.Column("event_key", sa.String(160), nullable=False),
            sa.Column("kind", sa.String(40), nullable=False),
            sa.Column("entity_id", sa.BigInteger(), nullable=False),
            sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("status", sa.String(24), nullable=False),
            sa.Column("owner_token", sa.String(64), nullable=True),
            sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
            sa.Column("attempts", sa.Integer(), nullable=False),
            sa.Column("job_id", sa.String(160), nullable=True),
            sa.Column("last_error", sa.String(100), nullable=False),
            sa.Column("consume_status", sa.String(24), nullable=False),
            sa.Column("consume_token", sa.String(64), nullable=True),
            sa.Column("consume_until", sa.DateTime(timezone=True), nullable=True),
            sa.Column("result", sa.JSON(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("tenant_id", "event_key", name="uq_outbox_tenant_key"),
        )
    expected = {"id", "tenant_id", "event_key", "kind", "entity_id", "available_at",
                "next_attempt_at", "status", "owner_token", "lease_until", "attempts",
                "job_id", "last_error", "consume_status", "consume_token", "consume_until", "result", "created_at"}
    columns = {c["name"] for c in sa.inspect(connection).get_columns("outbox_events")}
    if "consume_token" not in columns:
        op.add_column("outbox_events", sa.Column("consume_token", sa.String(64), nullable=True))
        columns.add("consume_token")
    if not expected <= columns:
        raise RuntimeError("Existing outbox schema is incompatible")
    unique = sa.inspect(connection).get_unique_constraints("outbox_events")
    if not any(set(item["column_names"]) == {"tenant_id", "event_key"} for item in unique):
        raise RuntimeError("Existing outbox schema lacks durable intent uniqueness")
    indexes = {i["name"] for i in sa.inspect(connection).get_indexes("outbox_events")}
    for column in ("tenant_id", "entity_id", "status"):
        name = "ix_outbox_events_" + column
        if name not in indexes:
            op.create_index(name, "outbox_events", [column])


def downgrade():
    raise RuntimeError("Forward-only: retain unconsumed outbox and durable receipts")
