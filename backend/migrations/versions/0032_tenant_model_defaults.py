"""租户模型默认设置，独立于已有知识库索引绑定。"""
from alembic import op
import sqlalchemy as sa

revision = "0032_tenant_model_defaults"
down_revision = "0031_task_checkpoint_indexes"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("knowledge_bases", sa.Column("inherits_embedding_model", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("knowledge_bases", sa.Column("inherits_rerank_model", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.create_table("tenant_model_defaults",
                    sa.Column("tenant_id", sa.BigInteger(), primary_key=True),
                    sa.Column("embedding_model_id", sa.BigInteger(), nullable=True),
                    sa.Column("rerank_model_id", sa.BigInteger(), nullable=True))


def downgrade():
    op.drop_table("tenant_model_defaults")
    op.drop_column("knowledge_bases", "inherits_rerank_model")
    op.drop_column("knowledge_bases", "inherits_embedding_model")
