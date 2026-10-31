"""Add user profile fields for phone, email, organization, and avatar."""

from alembic import op
import sqlalchemy as sa

revision = "0008_user_profile_fields"
down_revision = "0007_resource_legacy_namespace"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("phone", sa.String(length=32), nullable=False, server_default=""))
    op.add_column("users", sa.Column("email", sa.String(length=160), nullable=False, server_default=""))
    op.add_column("users", sa.Column("department_id", sa.BigInteger(), nullable=True))
    op.add_column("users", sa.Column("position_id", sa.BigInteger(), nullable=True))
    op.add_column("users", sa.Column("avatar_url", sa.Text(), nullable=True))
    op.add_column("users", sa.Column("profile", sa.JSON(), nullable=True))
    op.create_index("ix_users_department_id", "users", ["department_id"])
    op.create_index("ix_users_position_id", "users", ["position_id"])


def downgrade() -> None:
    op.drop_index("ix_users_position_id", table_name="users")
    op.drop_index("ix_users_department_id", table_name="users")
    op.drop_column("users", "profile")
    op.drop_column("users", "avatar_url")
    op.drop_column("users", "position_id")
    op.drop_column("users", "department_id")
    op.drop_column("users", "email")
    op.drop_column("users", "phone")
