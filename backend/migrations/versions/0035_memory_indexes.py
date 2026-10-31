"""独立记忆索引、修订链及防止自动复活的遗忘标记。"""
from alembic import op
import sqlalchemy as sa

revision = "0035_memory_indexes"
down_revision = "0034_context_memory_jobs"
branch_labels = None
depends_on = None


def upgrade():
    for column in (
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("content_hash", sa.String(64), nullable=False, server_default=""),
        sa.Column("supersedes_id", sa.BigInteger(), nullable=True),
        sa.Column("index_status", sa.String(24), nullable=False, server_default="unconfigured"),
        sa.Column("embedding_fingerprint", sa.String(64), nullable=True),
        sa.Column("invalidation_reason", sa.String(120), nullable=False, server_default=""),
    ):
        op.add_column("agent_memories", column)
    op.create_index("ix_agent_memories_supersedes_id", "agent_memories", ["supersedes_id"])
    op.create_table("memory_vectors",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("tenant_id", sa.BigInteger(), nullable=False),
        sa.Column("owner_id", sa.BigInteger(), nullable=False),
        sa.Column("memory_id", sa.BigInteger(), nullable=False),
        sa.Column("memory_version", sa.Integer(), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("model_fingerprint", sa.String(64), nullable=False),
        sa.Column("dimensions", sa.Integer(), nullable=False),
        sa.Column("vector", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.UniqueConstraint("memory_id", "memory_version", "model_fingerprint", name="uq_memory_vector_binding"))
    op.create_table("memory_tombstones",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("tenant_id", sa.BigInteger(), nullable=False),
        sa.Column("owner_id", sa.BigInteger(), nullable=False),
        sa.Column("agent_id", sa.BigInteger(), nullable=True),
        sa.Column("memory_id", sa.BigInteger(), nullable=False),
        sa.Column("memory_key", sa.String(160), nullable=True),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("source_through_id", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False))
    for table, columns in {"memory_vectors": ["tenant_id", "owner_id", "memory_id"],
                           "memory_tombstones": ["tenant_id", "owner_id", "agent_id"]}.items():
        for column in columns:
            op.create_index(f"ix_{table}_{column}", table, [column])


def downgrade():
    op.drop_table("memory_tombstones")
    op.drop_table("memory_vectors")
    op.drop_index("ix_agent_memories_supersedes_id", table_name="agent_memories")
    for column in ("invalidation_reason", "embedding_fingerprint", "index_status", "supersedes_id", "content_hash", "version"):
        op.drop_column("agent_memories", column)
