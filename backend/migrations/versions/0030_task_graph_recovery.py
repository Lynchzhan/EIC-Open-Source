"""Durable scheduled worker graph checkpoints with task scoped fencing."""
from alembic import op
import sqlalchemy as sa

revision = "0030_task_graph_recovery"
down_revision = "0029_tool_execution_intents"
branch_labels = None
depends_on = None

def upgrade():
    op.create_table("task_graph_writes",
        sa.Column("run_id", sa.BigInteger(), sa.ForeignKey("task_runs.id"), primary_key=True),
        sa.Column("checkpoint_id", sa.String(64), primary_key=True),
        sa.Column("task_id", sa.String(64), primary_key=True),
        sa.Column("write_index", sa.Integer(), primary_key=True),
        sa.Column("channel", sa.String(160), nullable=False),
        sa.Column("worker_token", sa.String(64), nullable=False),
        sa.Column("fencing_version", sa.Integer(), nullable=False),
        sa.Column("payload_type", sa.String(24), nullable=False),
        sa.Column("payload_encrypted", sa.Text(), nullable=False),
    )
    op.create_table("task_graph_leases",
        sa.Column("run_id", sa.BigInteger(), sa.ForeignKey("task_runs.id"), primary_key=True),
        sa.Column("binding_digest", sa.String(64), nullable=False),
        sa.Column("worker_token", sa.String(64), nullable=False),
        sa.Column("fencing_version", sa.Integer(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("state", sa.String(24), nullable=False),
    )
    op.create_table("task_graph_checkpoints",
        sa.Column("run_id", sa.BigInteger(), sa.ForeignKey("task_runs.id"), primary_key=True),
        sa.Column("namespace", sa.String(128), primary_key=True, default=""),
        sa.Column("checkpoint_id", sa.String(64), primary_key=True),
        sa.Column("parent_id", sa.String(64), nullable=True),
        sa.Column("tenant_id", sa.BigInteger(), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("task_id", sa.BigInteger(), sa.ForeignKey("scheduled_tasks.id"), nullable=False),
        sa.Column("schedule_revision", sa.Integer(), nullable=False),
        sa.Column("worker_token", sa.String(64), nullable=False),
        sa.Column("fencing_version", sa.Integer(), nullable=False),
        sa.Column("graph_version", sa.String(80), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("payload_type", sa.String(24), nullable=False),
        sa.Column("payload_encrypted", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_task_graph_checkpoints_run_created", "task_graph_checkpoints", ["run_id", "created_at"])

def downgrade():
    op.drop_table("task_graph_writes")
    op.drop_table("task_graph_checkpoints")
    op.drop_table("task_graph_leases")
