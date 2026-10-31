"""显式模型思考能力，旧模型不额外发送思考参数。"""
from alembic import op
import sqlalchemy as sa

revision = "0036_model_reasoning_policy"
down_revision = "0035_memory_indexes"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("ai_models", sa.Column("reasoning_policy", sa.JSON(), nullable=True))


def downgrade():
    op.drop_column("ai_models", "reasoning_policy")
