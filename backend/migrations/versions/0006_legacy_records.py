"""Add redacted source-row archive for complete migration coverage.

Revision ID: 0006_legacy_records
Revises: 0005_system_organization_domains
Create Date: 2026-09-10
"""

from alembic import op
import sqlalchemy as sa

revision = "0006_legacy_records"
down_revision = "0005_system_organization_domains"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "legacy_records",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("source_table", sa.String(length=160), nullable=False),
        sa.Column("source_id", sa.String(length=160), nullable=False),
        sa.Column("tenant_id", sa.BigInteger(), sa.ForeignKey("tenants.id"), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("imported_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("source_table", "source_id", name="uq_legacy_records_source"),
    )
    op.create_index("ix_legacy_records_source_table", "legacy_records", ["source_table"])
    op.create_index("ix_legacy_records_tenant_id", "legacy_records", ["tenant_id"])


def downgrade() -> None:
    op.drop_table("legacy_records")
