"""持久摘要、后台模型任务与可授权回读的工具结果。"""
from alembic import op
import sqlalchemy as sa

revision = "0034_context_memory_jobs"
down_revision = "0033_conversation_budgets"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("context_jobs",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("tenant_id", sa.BigInteger(), nullable=False),
        sa.Column("owner_id", sa.BigInteger(), nullable=False),
        sa.Column("session_id", sa.BigInteger(), nullable=True),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("job_key", sa.String(160), nullable=False),
        sa.Column("source_hash", sa.String(64), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("run_id", sa.String(64), nullable=True),
        sa.Column("lease_token", sa.String(64), nullable=True),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("result_encrypted", sa.Text(), nullable=True),
        sa.Column("usage", sa.JSON(), nullable=False),
        sa.Column("error_code", sa.String(120), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("tenant_id", "job_key", name="uq_context_job_key"))
    op.create_table("chat_summaries",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("tenant_id", sa.BigInteger(), nullable=False),
        sa.Column("owner_id", sa.BigInteger(), nullable=False),
        sa.Column("session_id", sa.BigInteger(), nullable=False),
        sa.Column("job_id", sa.BigInteger(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("through_id", sa.BigInteger(), nullable=False),
        sa.Column("source_ids", sa.JSON(), nullable=False),
        sa.Column("source_hash", sa.String(64), nullable=False),
        sa.Column("config_hash", sa.String(64), nullable=False),
        sa.Column("content", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("session_id", "version", name="uq_chat_summary_version"))
    op.create_table("context_artifacts",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("tenant_id", sa.BigInteger(), nullable=False),
        sa.Column("owner_id", sa.BigInteger(), nullable=False),
        sa.Column("session_id", sa.BigInteger(), nullable=False),
        sa.Column("request_id", sa.String(80), nullable=False),
        sa.Column("call_id", sa.String(120), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("content_encrypted", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False))
    for table, columns in {
        "context_jobs": ["tenant_id", "owner_id", "session_id", "status"],
        "chat_summaries": ["tenant_id", "owner_id", "session_id", "status"],
        "context_artifacts": ["tenant_id", "owner_id", "session_id", "request_id"],
    }.items():
        for column in columns:
            op.create_index(f"ix_{table}_{column}", table, [column])


def downgrade():
    for table in ("context_artifacts", "chat_summaries", "context_jobs"):
        op.drop_table(table)
