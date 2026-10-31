"""会话累计调用预算，未知用量与在途预留持久化。"""
from alembic import op
import sqlalchemy as sa

revision = "0033_conversation_budgets"
down_revision = "0032_tenant_model_defaults"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("conversation_budgets",
                    sa.Column("session_id", sa.BigInteger(), primary_key=True),
                    sa.Column("tenant_id", sa.BigInteger(), nullable=False),
                    sa.Column("owner_id", sa.BigInteger(), nullable=False),
                    sa.Column("state", sa.JSON(), nullable=False))
    op.create_index("ix_conversation_budgets_tenant_id", "conversation_budgets", ["tenant_id"])


def downgrade():
    op.drop_index("ix_conversation_budgets_tenant_id", table_name="conversation_budgets")
    op.drop_table("conversation_budgets")
