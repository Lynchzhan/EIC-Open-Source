"""前向扩展计划与快照；旧任务不自动启用或补跑。"""
from alembic import op
import sqlalchemy as sa

revision = "0020_task_contract"
down_revision = "0019_index_lifecycle"
branch_labels = None
depends_on = None


def upgrade():
    specifications = {
        "scheduled_tasks": [
            ("next_run_at", sa.DateTime(timezone=True), None),
            ("schedule_revision", sa.Integer(), "0"),
            ("time_zone", sa.String(80), "Asia/Shanghai"),
            ("schedule_policy", sa.String(24), "hold"),
            ("deleted_at", sa.DateTime(timezone=True), None),
        ],
        "task_runs": [
            ("schedule_revision", sa.Integer(), "0"),
            ("scheduled_for", sa.DateTime(timezone=True), None),
            ("input_snapshot", sa.JSON(), "{}"),
        ],
    }
    for table, fields in specifications.items():
        columns = {c["name"]: c for c in sa.inspect(op.get_bind()).get_columns(table)}
        for name, kind, default in fields:
            nullable = default is None
            if isinstance(kind, sa.JSON) and op.get_bind().dialect.name == "mysql":
                default = sa.text("('{}')")
            if name not in columns:
                op.add_column(table, sa.Column(name, kind, nullable=nullable, server_default=default))
            elif (columns[name]["nullable"] != nullable
                  or columns[name]["type"]._type_affinity != kind._type_affinity
                  or isinstance(kind, sa.String) and columns[name]["type"].length != kind.length):
                raise RuntimeError("Incompatible task contract column: " + table + "." + name)
    indexes = {i["name"]: i["column_names"] for i in sa.inspect(op.get_bind()).get_indexes("scheduled_tasks")}
    if "ix_scheduled_tasks_next_run_at" not in indexes:
        op.create_index("ix_scheduled_tasks_next_run_at", "scheduled_tasks", ["next_run_at"])
    elif indexes["ix_scheduled_tasks_next_run_at"] != ["next_run_at"]:
        raise RuntimeError("Incompatible next run index")


def downgrade():
    raise RuntimeError("Forward-only: preserve task runs, snapshots and schedule revisions")
