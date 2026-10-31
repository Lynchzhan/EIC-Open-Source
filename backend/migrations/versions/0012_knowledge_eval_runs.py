"""Persist knowledge evaluation run history."""

from alembic import op
import sqlalchemy as sa

revision = "0012_knowledge_eval_runs"
down_revision = "0011_knowledge_extraction_report"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "knowledge_eval_runs",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("tenant_id", sa.BigInteger(), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("eval_set_id", sa.BigInteger(), sa.ForeignKey("knowledge_eval_sets.id"), nullable=False),
        sa.Column("knowledge_base_id", sa.BigInteger(), sa.ForeignKey("knowledge_bases.id"), nullable=False),
        sa.Column("k", sa.Integer(), nullable=False, server_default="8"),
        sa.Column("strategy", sa.String(length=32), nullable=False, server_default="full"),
        sa.Column("result", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_knowledge_eval_runs_tenant_id", "knowledge_eval_runs", ["tenant_id"])
    op.create_index("ix_knowledge_eval_runs_eval_set_id", "knowledge_eval_runs", ["eval_set_id"])
    op.create_index("ix_knowledge_eval_runs_knowledge_base_id", "knowledge_eval_runs", ["knowledge_base_id"])


def downgrade() -> None:
    op.drop_index("ix_knowledge_eval_runs_knowledge_base_id", table_name="knowledge_eval_runs")
    op.drop_index("ix_knowledge_eval_runs_eval_set_id", table_name="knowledge_eval_runs")
    op.drop_index("ix_knowledge_eval_runs_tenant_id", table_name="knowledge_eval_runs")
    op.drop_table("knowledge_eval_runs")
