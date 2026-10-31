"""持久稀疏索引；历史正文不在 schema 迁移内无界回填。"""
from alembic import op
import sqlalchemy as sa

revision = "0041_sparse_index"
down_revision = "0040_langfuse_deliveries"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("knowledge_sparse_chunks",
        sa.Column("chunk_id", sa.BigInteger(), sa.ForeignKey("knowledge_chunks.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("tokenizer_version", sa.String(64), nullable=False),
        sa.Column("token_count", sa.Integer(), nullable=False))
    op.create_table("knowledge_sparse_terms",
        sa.Column("term_hash", sa.String(64), primary_key=True),
        sa.Column("chunk_id", sa.BigInteger(), sa.ForeignKey("knowledge_sparse_chunks.chunk_id", ondelete="CASCADE"), primary_key=True),
        sa.Column("frequency", sa.Integer(), nullable=False))
    op.create_index("ix_sparse_terms_chunk", "knowledge_sparse_terms", ["chunk_id"])


def downgrade():
    # MySQL 外键依赖此索引；删除表时数据库会一并删除索引。
    op.drop_table("knowledge_sparse_terms")
    op.drop_table("knowledge_sparse_chunks")
