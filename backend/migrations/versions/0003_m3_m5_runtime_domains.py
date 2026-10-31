"""Add durable M3-M5 runtime records.

Revision ID: 0003_m3_m5_runtime_domains
Revises: 0002_core_ai_domains
Create Date: 2026-09-10
"""

from alembic import op
import sqlalchemy as sa

revision = "0003_m3_m5_runtime_domains"
down_revision = "0002_core_ai_domains"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("chat_sessions") as batch:
        batch.add_column(sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True))
    with op.batch_alter_table("chat_messages") as batch:
        batch.add_column(sa.Column("request_id", sa.String(length=80), nullable=True))
        batch.add_column(sa.Column("token_usage", sa.JSON(), nullable=True))
        batch.add_column(sa.Column("status", sa.String(length=32), nullable=True))
    op.execute("UPDATE chat_sessions SET updated_at = created_at WHERE updated_at IS NULL")
    op.execute("UPDATE chat_messages SET token_usage = '{}' WHERE token_usage IS NULL")
    op.execute("UPDATE chat_messages SET status = 'completed' WHERE status IS NULL")
    op.create_index("ix_chat_messages_request_id", "chat_messages", ["request_id"])
    op.create_table(
        "knowledge_chunks",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("tenant_id", sa.BigInteger(), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("knowledge_base_id", sa.BigInteger(), sa.ForeignKey("knowledge_bases.id"), nullable=False),
        sa.Column("document_id", sa.BigInteger(), sa.ForeignKey("knowledge_documents.id"), nullable=False),
        sa.Column("parent_chunk_id", sa.BigInteger(), nullable=True),
        sa.Column("chunk_index", sa.Integer(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("metadata", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("document_id", "chunk_index", name="uq_knowledge_chunks_document_index"),
    )
    op.create_table(
        "knowledge_ingestion_runs",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("tenant_id", sa.BigInteger(), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("document_id", sa.BigInteger(), sa.ForeignKey("knowledge_documents.id"), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("chunk_count", sa.Integer(), nullable=False),
        sa.Column("embedding_count", sa.Integer(), nullable=False),
        sa.Column("error_message", sa.String(length=1000), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_table(
        "agent_memories",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("tenant_id", sa.BigInteger(), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("owner_id", sa.BigInteger(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("agent_id", sa.BigInteger(), nullable=True),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("metadata", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "tool_audits",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("tenant_id", sa.BigInteger(), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("user_id", sa.BigInteger(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("chat_id", sa.BigInteger(), nullable=True),
        sa.Column("tool_name", sa.String(length=200), nullable=False),
        sa.Column("tool_type", sa.String(length=40), nullable=False),
        sa.Column("outcome", sa.String(length=32), nullable=False),
        sa.Column("input_digest", sa.String(length=64), nullable=False),
        sa.Column("output_digest", sa.String(length=64), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=False),
        sa.Column("error_message", sa.String(length=1000), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    for table, columns in {
        "knowledge_chunks": ["tenant_id", "knowledge_base_id", "document_id", "parent_chunk_id", "content_hash"],
        "knowledge_ingestion_runs": ["tenant_id", "document_id"],
        "agent_memories": ["tenant_id", "owner_id", "agent_id"],
        "tool_audits": ["tenant_id", "user_id", "chat_id"],
    }.items():
        for column in columns:
            op.create_index(f"ix_{table}_{column}", table, [column])


def downgrade() -> None:
    op.drop_table("tool_audits")
    op.drop_table("agent_memories")
    op.drop_table("knowledge_ingestion_runs")
    op.drop_table("knowledge_chunks")
    op.drop_index("ix_chat_messages_request_id", table_name="chat_messages")
    with op.batch_alter_table("chat_messages") as batch:
        batch.drop_column("status")
        batch.drop_column("token_usage")
        batch.drop_column("request_id")
    with op.batch_alter_table("chat_sessions") as batch:
        batch.drop_column("updated_at")
