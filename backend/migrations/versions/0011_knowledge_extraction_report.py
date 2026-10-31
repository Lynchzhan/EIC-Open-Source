"""Add knowledge document extraction report."""

from alembic import op
import sqlalchemy as sa

revision = "0011_knowledge_extraction_report"
down_revision = "0010_knowledge_retrieval_and_eval"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("knowledge_documents") as batch:
        batch.add_column(sa.Column("extraction_report", sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("knowledge_documents") as batch:
        batch.drop_column("extraction_report")

