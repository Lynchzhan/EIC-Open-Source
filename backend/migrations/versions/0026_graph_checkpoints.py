"""持久化真实 LangGraph 状态及 pending writes；业务数据不重写。"""
from alembic import op
import sqlalchemy as sa

revision = "0026_graph_checkpoints"
down_revision = "0025_dependency_controls"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "agent_graph_checkpoints",
        sa.Column("run_id", sa.String(64), sa.ForeignKey("agent_runs.run_id"), primary_key=True),
        sa.Column("namespace", sa.String(128), primary_key=True),
        sa.Column("checkpoint_id", sa.String(64), primary_key=True),
        sa.Column("parent_id", sa.String(64), nullable=True),
        sa.Column("graph_version", sa.String(80), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("payload_type", sa.String(24), nullable=False),
        sa.Column("payload_encrypted", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "agent_graph_writes",
        sa.Column("run_id", sa.String(64), sa.ForeignKey("agent_runs.run_id"), primary_key=True),
        sa.Column("namespace", sa.String(128), primary_key=True),
        sa.Column("checkpoint_id", sa.String(64), primary_key=True),
        sa.Column("task_id", sa.String(64), primary_key=True),
        sa.Column("write_index", sa.Integer(), primary_key=True),
        sa.Column("channel", sa.String(160), nullable=False),
        sa.Column("payload_type", sa.String(24), nullable=False),
        sa.Column("payload_encrypted", sa.Text(), nullable=False),
    )


def downgrade():
    raise RuntimeError("Forward-only: restore the matching pre-release database backup")
