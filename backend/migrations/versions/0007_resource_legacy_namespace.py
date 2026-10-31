"""Track the legacy identity of compatibility resources.

The compatibility ``resources`` table keeps a single numeric primary key for
API compatibility.  A source table and source ID are nevertheless a separate
identity, so two legacy tables may legally contain the same numeric ID.
"""

from alembic import op
import sqlalchemy as sa

revision = "0007_resource_legacy_namespace"
down_revision = "0006_legacy_records"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("resources", sa.Column("legacy_source_id", sa.BigInteger(), nullable=True))
    op.create_index("ix_resources_legacy_source_id", "resources", ["legacy_source_id"])


def downgrade() -> None:
    op.drop_index("ix_resources_legacy_source_id", table_name="resources")
    op.drop_column("resources", "legacy_source_id")
