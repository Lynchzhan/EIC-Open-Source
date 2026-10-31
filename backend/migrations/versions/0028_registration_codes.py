"""新增一次性注册码，仅保存摘要与使用状态。"""
from alembic import op
import sqlalchemy as sa

revision = "0028_registration_codes"
down_revision = "0027_observability_events"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "registration_codes",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("tenant_id", sa.BigInteger(), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("code_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("created_by", sa.BigInteger(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("used_by", sa.BigInteger(), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_registration_codes_tenant_id", "registration_codes", ["tenant_id"])


def downgrade():
    op.drop_table("registration_codes")
