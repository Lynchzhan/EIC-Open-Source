"""前向扩展索引版本与墓碑，保留全部旧块和 ingestion history。"""
from alembic import op
import sqlalchemy as sa

revision = "0019_index_lifecycle"
down_revision = "0018_chat_parameter_profile"
branch_labels = None
depends_on = None


def upgrade():
    specifications = {
        "knowledge_documents": [
            ("is_deleted", sa.Boolean(), sa.false()),
            ("source_revision", sa.Integer(), "0"),
            ("active_source_revision", sa.Integer(), "0"),
            ("active_index_version", sa.BigInteger(), "0"),
            ("pending_index_version", sa.BigInteger(), None),
            ("parse_status", sa.String(32), "pending"),
            ("embedding_status", sa.String(32), "pending"),
            ("index_status", sa.String(32), "pending"),
            ("index_error", sa.JSON(), "{}"),
        ],
        "knowledge_chunks": [("index_version", sa.BigInteger(), "0")],
        "knowledge_ingestion_runs": [
            ("source_revision", sa.Integer(), "0"),
            ("parameter_hash", sa.String(64), ""),
            ("parameters", sa.JSON(), "{}"),
            ("lease_until", sa.DateTime(timezone=True), None),
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
            elif columns[name]["nullable"] != nullable or columns[name]["type"]._type_affinity != kind._type_affinity:
                raise RuntimeError("Incompatible index lifecycle column: " + table + "." + name)
    inspector = sa.inspect(op.get_bind())
    unique = {u["name"]: u["column_names"] for u in inspector.get_unique_constraints("knowledge_chunks")}
    expected = ["document_id", "index_version", "chunk_index"]
    if "uq_knowledge_chunks_version_index" in unique:
        if unique["uq_knowledge_chunks_version_index"] != expected:
            raise RuntimeError("Incompatible versioned chunk uniqueness")
    else:
        with op.batch_alter_table("knowledge_chunks") as batch:
            if "uq_knowledge_chunks_document_index" in unique:
                batch.drop_constraint("uq_knowledge_chunks_document_index", type_="unique")
            batch.create_unique_constraint("uq_knowledge_chunks_version_index", expected)
    # 旧 ready 只证明切块；没有投影完整性证据，不推断 embedding 已成功。
    op.execute(sa.text(
        "UPDATE knowledge_documents SET parse_status='legacy', embedding_status='unverified', "
        "index_status='legacy' WHERE active_index_version=0 AND parse_status='pending'"
    ))


def downgrade():
    raise RuntimeError("Forward-only: preserve index versions and ingestion history")
