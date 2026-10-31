"""Add department description for organization tree display."""

from alembic import op
import sqlalchemy as sa

revision = "0009_department_description"
down_revision = "0008_user_profile_fields"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("departments", sa.Column("description", sa.String(length=500), nullable=False, server_default=""))


def downgrade() -> None:
    op.drop_column("departments", "description")
