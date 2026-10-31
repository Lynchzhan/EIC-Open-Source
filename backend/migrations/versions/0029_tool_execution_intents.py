"""Persist atomic, non-replayable tool execution intents."""
from alembic import op
import sqlalchemy as sa

revision = "0029_tool_execution_intents"
down_revision = "0028_registration_codes"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "tool_execution_intents",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("tenant_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("tool_name", sa.String(200), nullable=False),
        sa.Column("idempotency_key_digest", sa.String(64), nullable=False),
        sa.Column("input_digest", sa.String(64), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("result_encrypted", sa.Text(), nullable=True),
        sa.Column("result_digest", sa.String(64), nullable=True),
        sa.Column("error_code", sa.String(80), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "tenant_id", "user_id", "idempotency_key_digest",
            name="uq_tool_intent_scope_key",
        ),
    )
    op.create_index("ix_tool_intents_tenant_created", "tool_execution_intents", ["tenant_id", "created_at"])


def downgrade():
    op.drop_table("tool_execution_intents")
