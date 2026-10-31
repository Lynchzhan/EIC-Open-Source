"""P13 会话记忆分层和冲突事实排序字段。"""
from alembic import op
import sqlalchemy as sa


revision = "0024_memory_scopes"
down_revision = "0023_memory_lifecycle"
branch_labels = None
depends_on = None


def upgrade():
    specifications = [
        ("session_id", sa.BigInteger(), None),
        ("memory_key", sa.String(160), None),
        ("source_priority", sa.Integer(), "0"),
    ]
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {column["name"]: column for column in inspector.get_columns("agent_memories")}
    missing = []
    for name, kind, default in specifications:
        nullable = default is None
        if name not in columns:
            missing.append((name, kind, nullable, default))
            continue
        column = columns[name]
        if column["nullable"] != nullable or column["type"]._type_affinity != kind._type_affinity:
            raise RuntimeError("Incompatible memory scope column: " + name)

    # SQLite 不能通过 ALTER TABLE ADD COLUMN 直接添加新的外键约束。
    # 使用 Alembic batch 模式重建表，复制历史行并保留新的关系约束。
    if bind.dialect.name == "sqlite" and missing:
        with op.batch_alter_table("agent_memories", recreate="always") as batch:
            for name, kind, nullable, default in missing:
                foreign_key = (
                    sa.ForeignKey(
                        "chat_sessions.id",
                        name="fk_agent_memories_session_id_chat_sessions",
                    )
                    if name == "session_id"
                    else None
                )
                batch.add_column(sa.Column(
                    name, kind, foreign_key, nullable=nullable, server_default=default,
                ))
    else:
        for name, kind, nullable, default in missing:
            foreign_key = (
                sa.ForeignKey(
                    "chat_sessions.id",
                    name="fk_agent_memories_session_id_chat_sessions",
                )
                if name == "session_id"
                else None
            )
            op.add_column(
                "agent_memories",
                sa.Column(name, kind, foreign_key, nullable=nullable, server_default=default),
            )

    indexes = {item["name"]: item["column_names"] for item in sa.inspect(bind).get_indexes("agent_memories")}
    for name, column in {
        "ix_agent_memories_session_id": "session_id",
        "ix_agent_memories_memory_key": "memory_key",
        "ix_agent_memories_source_priority": "source_priority",
    }.items():
        if name not in indexes:
            op.create_index(name, "agent_memories", [column])
        elif indexes[name] != [column]:
            raise RuntimeError("Incompatible memory scope index: " + name)


def downgrade():
    raise RuntimeError("Forward-only: preserve memory scope and conflict evidence")
