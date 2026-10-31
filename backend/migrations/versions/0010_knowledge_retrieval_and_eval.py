"""Add knowledge retrieval strategy, document timestamps, and eval tables."""

from alembic import op
import sqlalchemy as sa

revision = "0010_knowledge_retrieval_and_eval"
down_revision = "0009_department_description"
branch_labels = None
depends_on = None


def upgrade() -> None:
    connection = op.get_bind()

    def add_missing(table, columns):
        existing = {column["name"]: column for column in sa.inspect(connection).get_columns(table)}
        for column in columns:
            if column.name in existing:
                if not isinstance(existing[column.name]["type"], type(column.type)):
                    raise RuntimeError("Incompatible existing retrieval field: " + table + "." + column.name)
            else:
                op.add_column(table, column)

    def create_missing(table, *columns):
        if sa.inspect(connection).has_table(table):
            existing = {column["name"] for column in sa.inspect(connection).get_columns(table)}
            if not {column.name for column in columns} <= existing:
                raise RuntimeError("Incompatible existing evaluation table: " + table)
        else:
            op.create_table(table, *columns)

    def ensure_index(name, table, columns):
        existing = {item["name"]: item for item in sa.inspect(connection).get_indexes(table)}
        if name in existing:
            if existing[name]["column_names"] != columns or existing[name]["unique"]:
                raise RuntimeError("Incompatible existing evaluation index: " + name)
        else:
            op.create_index(name, table, columns)

    # 保留同一 revision，补齐失败后已存在的扩展字段，不重建或覆盖原表。
    add_missing("knowledge_bases", [
        sa.Column("retrieval_strategy", sa.String(length=32), nullable=False, server_default="full"),
        sa.Column("retrieval_config", sa.JSON(), nullable=True),
    ])
    add_missing("knowledge_documents", [
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    ])
    create_missing(
        "knowledge_eval_sets",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("tenant_id", sa.BigInteger(), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("knowledge_base_id", sa.BigInteger(), sa.ForeignKey("knowledge_bases.id"), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("description", sa.String(length=500), nullable=False, server_default=""),
        sa.Column("last_result", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    )
    ensure_index("ix_knowledge_eval_sets_tenant_id", "knowledge_eval_sets", ["tenant_id"])
    ensure_index("ix_knowledge_eval_sets_knowledge_base_id", "knowledge_eval_sets", ["knowledge_base_id"])
    create_missing(
        "knowledge_eval_cases",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("tenant_id", sa.BigInteger(), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("eval_set_id", sa.BigInteger(), sa.ForeignKey("knowledge_eval_sets.id"), nullable=False),
        sa.Column("query", sa.Text(), nullable=False),
        sa.Column("expected_document_ids", sa.JSON(), nullable=True),
        sa.Column("expected_chunk_ids", sa.JSON(), nullable=True),
        sa.Column("expected_answer", sa.Text(), nullable=False,
                  server_default=sa.text("('')") if connection.dialect.name == "mysql" else ""),
        sa.Column("complexity", sa.String(length=32), nullable=False, server_default="medium"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
    )
    ensure_index("ix_knowledge_eval_cases_tenant_id", "knowledge_eval_cases", ["tenant_id"])
    ensure_index("ix_knowledge_eval_cases_eval_set_id", "knowledge_eval_cases", ["eval_set_id"])


def downgrade() -> None:
    op.drop_index("ix_knowledge_eval_cases_eval_set_id", table_name="knowledge_eval_cases")
    op.drop_index("ix_knowledge_eval_cases_tenant_id", table_name="knowledge_eval_cases")
    op.drop_table("knowledge_eval_cases")
    op.drop_index("ix_knowledge_eval_sets_knowledge_base_id", table_name="knowledge_eval_sets")
    op.drop_index("ix_knowledge_eval_sets_tenant_id", table_name="knowledge_eval_sets")
    op.drop_table("knowledge_eval_sets")
    with op.batch_alter_table("knowledge_documents") as batch:
        batch.drop_column("updated_at")
        batch.drop_column("created_at")
    with op.batch_alter_table("knowledge_bases") as batch:
        batch.drop_column("retrieval_config")
        batch.drop_column("retrieval_strategy")
