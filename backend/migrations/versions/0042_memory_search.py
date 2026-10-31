"""独立记忆候选索引；历史回填在有界管理入口执行。"""
from alembic import op
import sqlalchemy as sa

revision = "0042_memory_search"
down_revision = "0041_sparse_index"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("memory_search_documents",
        sa.Column("memory_id", sa.BigInteger(), sa.ForeignKey("agent_memories.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("memory_version", sa.Integer(), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("tokenizer_version", sa.String(40), nullable=False))
    op.create_table("memory_search_terms",
        sa.Column("term_hash", sa.String(64), primary_key=True),
        sa.Column("memory_id", sa.BigInteger(), sa.ForeignKey("memory_search_documents.memory_id", ondelete="CASCADE"), primary_key=True))
    op.create_index("ix_memory_search_term_memory", "memory_search_terms", ["memory_id"])
    op.create_table("memory_projection_receipts",
        sa.Column("vector_id", sa.BigInteger(), sa.ForeignKey("memory_vectors.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("projection_version", sa.String(40), nullable=False),
        sa.Column("generation", sa.BigInteger(), nullable=False))


def downgrade():
    op.drop_table("memory_projection_receipts")
    # MySQL 外键依赖此索引；删除表时数据库会一并删除索引。
    op.drop_table("memory_search_terms")
    op.drop_table("memory_search_documents")
