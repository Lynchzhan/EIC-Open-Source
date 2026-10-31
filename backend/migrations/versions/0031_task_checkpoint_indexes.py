"""补齐任务图检查点的租户与任务查询索引。"""
from alembic import op

revision = "0031_task_checkpoint_indexes"
down_revision = "0030_task_graph_recovery"
branch_labels = None
depends_on = None


def upgrade():
    op.create_index(
        "ix_task_graph_checkpoints_tenant_id",
        "task_graph_checkpoints",
        ["tenant_id"],
        unique=False,
    )
    op.create_index(
        "ix_task_graph_checkpoints_task_id",
        "task_graph_checkpoints",
        ["task_id"],
        unique=False,
    )


def downgrade():
    op.drop_index(
        "ix_task_graph_checkpoints_task_id",
        table_name="task_graph_checkpoints",
    )
    op.drop_index(
        "ix_task_graph_checkpoints_tenant_id",
        table_name="task_graph_checkpoints",
    )
