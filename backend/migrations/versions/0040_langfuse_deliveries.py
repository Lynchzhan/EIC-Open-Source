"""持久观测投递账本；未知远端结果不自动重放。"""
from alembic import op
import sqlalchemy as sa

revision = "0040_langfuse_deliveries"
down_revision = "0039_model_search_mode"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("revoked_sessions",
        sa.Column("session_id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False))
    op.create_index("ix_revoked_sessions_tenant_id", "revoked_sessions", ["tenant_id"])
    op.create_index("ix_revoked_sessions_expires_at", "revoked_sessions", ["expires_at"])
    op.create_table("langfuse_deliveries",
        sa.Column("event_key", sa.String(160), primary_key=True),
        sa.Column("destination", sa.String(64), primary_key=True),
        sa.Column("export_key", sa.String(64), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("owner_token", sa.String(64), nullable=True),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("destination", "export_key", name="uq_langfuse_delivery_export"))
    op.create_index("ix_langfuse_delivery_pending", "langfuse_deliveries", ["destination", "status", "next_attempt_at"])


def downgrade():
    op.drop_index("ix_revoked_sessions_expires_at", table_name="revoked_sessions")
    op.drop_index("ix_revoked_sessions_tenant_id", table_name="revoked_sessions")
    op.drop_table("revoked_sessions")
    op.drop_index("ix_langfuse_delivery_pending", table_name="langfuse_deliveries")
    op.drop_table("langfuse_deliveries")
