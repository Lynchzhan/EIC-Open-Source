"""Add dedicated M2 AI domain tables.

Revision ID: 0002_core_ai_domains
Revises: 0001_initial_schema
Create Date: 2026-09-10
"""

from alembic import op
import sqlalchemy as sa

revision = "0002_core_ai_domains"
down_revision = "0001_initial_schema"
branch_labels = None
depends_on = None


def tenant_table(name, extra_columns):
    return op.create_table(
        name,
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("tenant_id", sa.BigInteger(), sa.ForeignKey("tenants.id"), nullable=False),
        *extra_columns,
    )


def upgrade() -> None:
    tenant_table("ai_apps", [sa.Column("name", sa.String(200), nullable=False), sa.Column("description", sa.String(500), nullable=False), sa.Column("chat_model_id", sa.BigInteger(), sa.ForeignKey("ai_models.id")), sa.Column("prompt_id", sa.BigInteger()), sa.Column("knowledge_base_id", sa.BigInteger()), sa.Column("config", sa.JSON(), nullable=False), sa.Column("enabled", sa.Boolean(), nullable=False)])
    tenant_table("ai_prompts", [sa.Column("name", sa.String(200), nullable=False), sa.Column("content", sa.Text(), nullable=False), sa.Column("enabled", sa.Boolean(), nullable=False)])
    tenant_table("knowledge_bases", [sa.Column("name", sa.String(200), nullable=False), sa.Column("embedding_model_id", sa.BigInteger(), sa.ForeignKey("ai_models.id")), sa.Column("rerank_model_id", sa.BigInteger(), sa.ForeignKey("ai_models.id")), sa.Column("chunk_size", sa.Integer(), nullable=False), sa.Column("chunk_overlap", sa.Integer(), nullable=False), sa.Column("status", sa.String(40), nullable=False)])
    tenant_table("knowledge_documents", [sa.Column("knowledge_base_id", sa.BigInteger(), sa.ForeignKey("knowledge_bases.id"), nullable=False), sa.Column("file_id", sa.BigInteger()), sa.Column("source_name", sa.String(500), nullable=False), sa.Column("content", sa.Text(), nullable=False), sa.Column("status", sa.String(40), nullable=False), sa.Column("error_message", sa.String(1000), nullable=False)])
    tenant_table("skills", [sa.Column("name", sa.String(200), nullable=False), sa.Column("description", sa.String(500), nullable=False), sa.Column("skill_type", sa.String(40), nullable=False), sa.Column("enabled", sa.Boolean(), nullable=False), sa.Column("config", sa.JSON(), nullable=False)])
    op.create_table("chat_sessions", sa.Column("id", sa.BigInteger(), primary_key=True), sa.Column("tenant_id", sa.BigInteger(), sa.ForeignKey("tenants.id"), nullable=False), sa.Column("owner_id", sa.BigInteger(), sa.ForeignKey("users.id"), nullable=False), sa.Column("app_id", sa.BigInteger(), sa.ForeignKey("ai_apps.id")), sa.Column("title", sa.String(500), nullable=False), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False))
    op.create_table("chat_messages", sa.Column("id", sa.BigInteger(), primary_key=True), sa.Column("tenant_id", sa.BigInteger(), sa.ForeignKey("tenants.id"), nullable=False), sa.Column("session_id", sa.BigInteger(), sa.ForeignKey("chat_sessions.id"), nullable=False), sa.Column("role", sa.String(20), nullable=False), sa.Column("content", sa.Text(), nullable=False), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False))
    for table in ("ai_apps", "ai_prompts", "knowledge_bases", "knowledge_documents", "skills", "chat_sessions", "chat_messages"):
        op.create_index(f"ix_{table}_tenant_id", table, ["tenant_id"])


def downgrade() -> None:
    for table in ("chat_messages", "chat_sessions", "skills", "knowledge_documents", "knowledge_bases", "ai_prompts", "ai_apps"):
        op.drop_table(table)
