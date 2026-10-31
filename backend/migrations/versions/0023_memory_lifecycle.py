"""P13 记忆生命周期字段、有效期和归档状态。"""
from alembic import op
import sqlalchemy as sa

revision = "0023_memory_lifecycle"
down_revision = "0022_agent_recovery"
branch_labels = None
depends_on = None


def upgrade():
    specifications = [
        ("memory_type", sa.String(32), "long_term"),
        ("importance", sa.Float(), "0.5"),
        ("confidence", sa.Float(), "0.5"),
        ("source_message_id", sa.BigInteger(), None),
        ("valid_from", sa.DateTime(timezone=True), None),
        ("valid_until", sa.DateTime(timezone=True), None),
        ("expires_at", sa.DateTime(timezone=True), None),
        ("status", sa.String(24), "active"),
    ]
    columns = {c["name"]: c for c in sa.inspect(op.get_bind()).get_columns("agent_memories")}
    for name, kind, default in specifications:
        nullable = default is None
        if name not in columns:
            op.add_column("agent_memories", sa.Column(name, kind, nullable=nullable, server_default=default))
        else:
            column = columns[name]
            if column["nullable"] != nullable or column["type"]._type_affinity != kind._type_affinity:
                raise RuntimeError("Incompatible memory lifecycle column: " + name)
    inspector = sa.inspect(op.get_bind())
    indexes = {item["name"]: item["column_names"] for item in inspector.get_indexes("agent_memories")}
    for name, column in {
        "ix_agent_memories_source_message_id": "source_message_id",
        "ix_agent_memories_valid_from": "valid_from",
        "ix_agent_memories_valid_until": "valid_until",
        "ix_agent_memories_expires_at": "expires_at",
        "ix_agent_memories_status": "status",
    }.items():
        if name not in indexes:
            op.create_index(name, "agent_memories", [column])
        elif indexes[name] != [column]:
            raise RuntimeError("Incompatible memory lifecycle index: " + name)


def downgrade():
    raise RuntimeError("Forward-only: preserve memory lifecycle and archive evidence")
