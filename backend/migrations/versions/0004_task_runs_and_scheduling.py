"""Add M6 ARQ scheduling state and task logs.

Revision ID: 0004_task_runs_and_scheduling
Revises: 0003_m3_m5_runtime_domains
Create Date: 2026-09-10
"""

from alembic import op
import sqlalchemy as sa

revision = "0004_task_runs_and_scheduling"
down_revision = "0003_m3_m5_runtime_domains"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("scheduled_tasks") as batch:
        batch.add_column(sa.Column("task_type", sa.String(length=24), nullable=True))
        batch.add_column(sa.Column("run_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("max_retries", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("status", sa.String(length=32), nullable=True))
    op.execute("UPDATE scheduled_tasks SET task_type = 'cron' WHERE task_type IS NULL")
    op.execute("UPDATE scheduled_tasks SET max_retries = 3 WHERE max_retries IS NULL")
    op.execute("UPDATE scheduled_tasks SET status = 'ready' WHERE status IS NULL")
    op.create_table(
        "task_runs",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("tenant_id", sa.BigInteger(), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("task_id", sa.BigInteger(), sa.ForeignKey("scheduled_tasks.id"), nullable=False),
        sa.Column("job_id", sa.String(length=80), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("result", sa.JSON(), nullable=False),
        sa.Column("error_message", sa.String(length=1000), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
    )
    for column in ("tenant_id", "task_id", "job_id"):
        op.create_index(f"ix_task_runs_{column}", "task_runs", [column])


def downgrade() -> None:
    op.drop_table("task_runs")
    with op.batch_alter_table("scheduled_tasks") as batch:
        batch.drop_column("status")
        batch.drop_column("max_retries")
        batch.drop_column("run_at")
        batch.drop_column("task_type")
