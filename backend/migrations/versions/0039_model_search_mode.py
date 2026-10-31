"""模型联网模式；原有模型默认关闭。"""
from alembic import op
import sqlalchemy as sa

revision = '0039_model_search_mode'
down_revision = '0036_model_reasoning_policy'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('ai_models', sa.Column('search_mode', sa.String(24), nullable=False, server_default='disabled'))


def downgrade():
    op.drop_column('ai_models', 'search_mode')
