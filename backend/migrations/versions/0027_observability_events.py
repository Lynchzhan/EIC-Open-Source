"""仅新增观测表；旧业务数据不回填、不重写。"""
from alembic import op
import sqlalchemy as sa

revision = "0027_observability_events"
down_revision = "0026_graph_checkpoints"
branch_labels = None
depends_on = None


def upgrade():
    columns = [
        sa.Column("event_key", sa.String(160), primary_key=True),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("project", sa.String(40), nullable=False),
        sa.Column("tenant_id", sa.BigInteger(), nullable=False),
    ]
    nullable_strings = {
        "workspace_id": 80, "owner_key": 64, "request_id": 80, "chat_id": 80,
        "run_id": 64, "trace_id": 64, "span_id": 64, "parent_span_id": 64,
        "agent_id": 80, "model_id": 80, "tool_name": 200, "error_code": 120,
        "cache_outcome": 16,
    }
    columns += [sa.Column(name, sa.String(size), nullable=True) for name, size in nullable_strings.items()]
    columns += [sa.Column(name, sa.String(size), nullable=False) for name, size in {
        "category": 24, "stage": 80, "status": 32, "source": 80, "usage_source": 40,
    }.items()]
    columns += [sa.Column(name, sa.Integer(), nullable=True) for name in ("attempt", "source_sequence")]
    columns += [sa.Column(name, sa.DateTime(timezone=True), nullable=False)
                for name in ("occurred_at", "collected_at")]
    columns += [sa.Column("duration_ms", sa.Float(), nullable=True)]
    columns += [sa.Column(name, sa.BigInteger(), nullable=True) for name in
                ("input_tokens", "output_tokens", "total_tokens", "cached_tokens", "saved_tokens")]
    columns += [sa.Column("summary", sa.JSON(), nullable=False)]
    op.create_table("observability_events", *columns)
    for suffix, names in (
        ("tenant", ["tenant_id", "project", "occurred_at"]),
        ("owner", ["tenant_id", "owner_key", "occurred_at"]),
        ("trace", ["tenant_id", "trace_id", "occurred_at"]),
        ("run", ["tenant_id", "run_id", "occurred_at"]),
    ):
        op.create_index(f"ix_observation_{suffix}_time", "observability_events", names)


def downgrade():
    # 只用于隔离副本演练；发布仍采用匹配版本的数据库备份恢复。
    op.drop_table("observability_events")
