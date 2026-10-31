"""显式对话参数能力；不改变密钥、历史或既有预算。"""
from alembic import op
import sqlalchemy as sa

revision = "0018_chat_parameter_profile"
down_revision = "0017_upload_reservations"
branch_labels = None
depends_on = None


def upgrade():
    columns = {c["name"]: c for c in sa.inspect(op.get_bind()).get_columns("ai_models")}
    if "chat_parameter_profile" not in columns:
        op.add_column("ai_models", sa.Column("chat_parameter_profile", sa.String(40),
                                            nullable=False, server_default="auto"))
    else:
        column = columns["chat_parameter_profile"]
        if column["nullable"] or not isinstance(column["type"], sa.String) or column["type"].length != 40:
            raise RuntimeError("Incompatible chat parameter profile column")


def downgrade():
    raise RuntimeError("Forward-only: preserve explicit provider configuration")
