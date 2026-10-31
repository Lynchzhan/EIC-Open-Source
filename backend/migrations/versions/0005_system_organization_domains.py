"""Add dedicated organization and RBAC relation tables.

Revision ID: 0005_system_organization_domains
Revises: 0004_task_runs_and_scheduling
Create Date: 2026-09-10
"""

from alembic import op
import sqlalchemy as sa

revision = "0005_system_organization_domains"
down_revision = "0004_task_runs_and_scheduling"
branch_labels = None
depends_on = None


def tenant_table(name: str, columns: list[sa.Column]) -> None:
    op.create_table(
        name,
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("tenant_id", sa.BigInteger(), sa.ForeignKey("tenants.id"), nullable=False),
        *columns,
    )


def upgrade() -> None:
    op.create_table(
        "role_permissions",
        sa.Column("role_id", sa.BigInteger(), sa.ForeignKey("roles.id"), primary_key=True),
        sa.Column("permission_id", sa.String(160), sa.ForeignKey("permissions.id"), primary_key=True),
    )
    tenant_table("departments", [sa.Column("parent_id", sa.BigInteger(), nullable=True), sa.Column("name", sa.String(200), nullable=False), sa.Column("active", sa.Boolean(), nullable=False), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False)])
    tenant_table("positions", [sa.Column("name", sa.String(200), nullable=False), sa.Column("description", sa.String(500), nullable=False), sa.Column("active", sa.Boolean(), nullable=False), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False)])
    for table, columns in {"departments": ["tenant_id", "parent_id"], "positions": ["tenant_id"], "role_permissions": ["role_id", "permission_id"]}.items():
        for column in columns:
            op.create_index(f"ix_{table}_{column}", table, [column])


def downgrade() -> None:
    op.drop_table("positions")
    op.drop_table("departments")
    op.drop_table("role_permissions")
