"""P10 Agent Run、Trace、Checkpoint 和工具调用摘要。"""
import sqlalchemy as sa
from alembic import op

revision = "0021_agent_runtime"
down_revision = "0020_task_contract"
branch_labels = None
depends_on = None


def _create_index_if_missing(name, table, columns):
    indexes = {item["name"]: item["column_names"] for item in sa.inspect(op.get_bind()).get_indexes(table)}
    if name not in indexes:
        op.create_index(name, table, columns)
    elif indexes[name] != columns:
        raise RuntimeError("Incompatible Agent Runtime index: " + name)


def upgrade():
    inspector = sa.inspect(op.get_bind())
    if "agent_runs" not in inspector.get_table_names():
        op.create_table(
            "agent_runs",
            sa.Column("run_id", sa.String(64), primary_key=True),
            sa.Column("tenant_id", sa.BigInteger(), sa.ForeignKey("tenants.id"), nullable=False),
            sa.Column("session_id", sa.BigInteger(), sa.ForeignKey("chat_sessions.id"), nullable=True),
            sa.Column("request_id", sa.String(80), nullable=True),
            sa.Column("trace_id", sa.String(64), nullable=False),
            sa.Column("graph_version", sa.String(80), nullable=False),
            sa.Column("status", sa.String(32), nullable=False, server_default="running"),
            sa.Column("current_node", sa.String(80), nullable=False, server_default="start"),
            sa.Column("event_seq", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("checkpoint_seq", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("step_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("error_code", sa.String(120), nullable=False, server_default=""),
            sa.Column("metadata", sa.JSON(), nullable=False),
            sa.Column("result", sa.JSON(), nullable=False),
            sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        )
    if "agent_events" not in inspector.get_table_names():
        op.create_table(
            "agent_events",
            sa.Column("id", sa.BigInteger(), primary_key=True),
            sa.Column("run_id", sa.String(64), sa.ForeignKey("agent_runs.run_id"), nullable=False),
            sa.Column("sequence", sa.Integer(), nullable=False),
            sa.Column("node_name", sa.String(80), nullable=False),
            sa.Column("event_type", sa.String(80), nullable=False),
            sa.Column("status", sa.String(32), nullable=False, server_default="completed"),
            sa.Column("payload", sa.JSON(), nullable=False),
            sa.Column("payload_hash", sa.String(64), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("run_id", "sequence", name="uq_agent_events_run_sequence"),
        )
    if "agent_checkpoints" not in inspector.get_table_names():
        op.create_table(
            "agent_checkpoints",
            sa.Column("id", sa.BigInteger(), primary_key=True),
            sa.Column("run_id", sa.String(64), sa.ForeignKey("agent_runs.run_id"), nullable=False),
            sa.Column("checkpoint_seq", sa.Integer(), nullable=False),
            sa.Column("node_name", sa.String(80), nullable=False),
            sa.Column("state_schema_version", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("state_json", sa.JSON(), nullable=False),
            sa.Column("state_hash", sa.String(64), nullable=False),
            sa.Column("resume_policy", sa.String(32), nullable=False, server_default="safe_replay"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("run_id", "checkpoint_seq", name="uq_agent_checkpoints_run_sequence"),
        )
    if "agent_tool_calls" not in inspector.get_table_names():
        op.create_table(
            "agent_tool_calls",
            sa.Column("id", sa.BigInteger(), primary_key=True),
            sa.Column("run_id", sa.String(64), sa.ForeignKey("agent_runs.run_id"), nullable=False),
            sa.Column("call_id", sa.String(120), nullable=False),
            sa.Column("tool_name", sa.String(200), nullable=False),
            sa.Column("input_digest", sa.String(64), nullable=False),
            sa.Column("output_digest", sa.String(64), nullable=True),
            sa.Column("status", sa.String(32), nullable=False),
            sa.Column("timeout_ms", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("retry_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("effect_status", sa.String(32), nullable=False, server_default="none"),
            sa.Column("error_code", sa.String(120), nullable=False, server_default=""),
            sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("run_id", "call_id", name="uq_agent_tool_calls_run_call"),
        )
    _create_index_if_missing("ix_agent_runs_tenant_id", "agent_runs", ["tenant_id"])
    _create_index_if_missing("ix_agent_runs_session_id", "agent_runs", ["session_id"])
    _create_index_if_missing("ix_agent_runs_request_id", "agent_runs", ["request_id"])
    _create_index_if_missing("ix_agent_runs_trace_id", "agent_runs", ["trace_id"])
    _create_index_if_missing("ix_agent_runs_status", "agent_runs", ["status"])
    _create_index_if_missing("ix_agent_runs_heartbeat_at", "agent_runs", ["heartbeat_at"])
    _create_index_if_missing("ix_agent_events_run_id", "agent_events", ["run_id"])
    _create_index_if_missing("ix_agent_events_run_created", "agent_events", ["run_id", "created_at"])
    _create_index_if_missing("ix_agent_checkpoints_run_id", "agent_checkpoints", ["run_id"])
    _create_index_if_missing("ix_agent_checkpoints_run_created", "agent_checkpoints", ["run_id", "created_at"])
    _create_index_if_missing("ix_agent_tool_calls_run_id", "agent_tool_calls", ["run_id"])
    _create_index_if_missing("ix_agent_tool_calls_run_created", "agent_tool_calls", ["run_id", "created_at"])


def downgrade():
    raise RuntimeError("Forward-only: preserve Agent Runtime trace and recovery evidence")
