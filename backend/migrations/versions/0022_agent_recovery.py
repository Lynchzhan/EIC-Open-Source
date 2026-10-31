"""P11 Agent 节点恢复和 Worker fencing。"""
from alembic import op
import sqlalchemy as sa


revision = "0022_agent_recovery"
down_revision = "0021_agent_runtime"
branch_labels = None
depends_on = None


def _create_index_if_missing(name, table, columns):
    indexes = {
        item["name"]: item["column_names"]
        for item in sa.inspect(op.get_bind()).get_indexes(table)
    }
    if name not in indexes:
        op.create_index(name, table, columns)
    elif indexes[name] != columns:
        raise RuntimeError("Incompatible Agent Recovery index: " + name)


def upgrade():
    inspector = sa.inspect(op.get_bind())
    if "agent_run_leases" not in inspector.get_table_names():
        op.create_table(
            "agent_run_leases",
            sa.Column(
                "run_id",
                sa.String(64),
                sa.ForeignKey("agent_runs.run_id"),
                primary_key=True,
            ),
            sa.Column("worker_token", sa.String(64), nullable=False, server_default=""),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("state", sa.String(24), nullable=False, server_default="released"),
            sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("attempt", sa.Integer(), nullable=False, server_default="0"),
        )
    _create_index_if_missing(
        "ix_agent_run_leases_expires_at", "agent_run_leases", ["expires_at"]
    )
    _create_index_if_missing(
        "ix_agent_run_leases_state", "agent_run_leases", ["state"]
    )


def downgrade():
    raise RuntimeError("Forward-only: preserve Agent Recovery leases and evidence")
