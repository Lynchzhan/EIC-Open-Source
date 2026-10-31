from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class TenantModelDefaults(Base):
    __tablename__ = "tenant_model_defaults"

    tenant_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    embedding_model_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    rerank_model_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)


class ConversationBudget(Base):
    __tablename__ = "conversation_budgets"

    session_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(BigInteger, index=True)
    owner_id: Mapped[int] = mapped_column(BigInteger)
    state: Mapped[dict] = mapped_column(JSON, default=dict)


class RevokedSession(Base):
    __tablename__ = "revoked_sessions"
    session_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[int] = mapped_column(BigInteger, index=True)
    user_id: Mapped[int] = mapped_column(BigInteger)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class LangfuseDelivery(Base):
    __tablename__ = "langfuse_deliveries"
    event_key: Mapped[str] = mapped_column(String(160), primary_key=True)
    destination: Mapped[str] = mapped_column(String(64), primary_key=True)
    export_key: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(24), default="pending")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    owner_token: Mapped[str | None] = mapped_column(String(64), nullable=True)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    __table_args__ = (
        Index("ix_langfuse_delivery_pending", "destination", "status", "next_attempt_at"),
        UniqueConstraint("destination", "export_key", name="uq_langfuse_delivery_export"),
    )


class ObservabilityEvent(Base):
    """独立观测记录；未知维度/usage 为空，不复制业务正文。"""
    __tablename__ = "observability_events"

    event_key: Mapped[str] = mapped_column(String(160), primary_key=True)
    schema_version: Mapped[int] = mapped_column(Integer, default=1)
    project: Mapped[str] = mapped_column(String(40), default="eic")
    tenant_id: Mapped[int] = mapped_column(BigInteger)
    workspace_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    owner_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    request_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    chat_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    run_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    trace_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    span_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    parent_span_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    agent_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    model_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    tool_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    category: Mapped[str] = mapped_column(String(24))
    stage: Mapped[str] = mapped_column(String(80))
    status: Mapped[str] = mapped_column(String(32))
    error_code: Mapped[str | None] = mapped_column(String(120), nullable=True)
    source: Mapped[str] = mapped_column(String(80))
    attempt: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source_sequence: Mapped[int | None] = mapped_column(Integer, nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    collected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    duration_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    input_tokens: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    total_tokens: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    cached_tokens: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    saved_tokens: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    usage_source: Mapped[str] = mapped_column(String(40), default="not_provided")
    cache_outcome: Mapped[str | None] = mapped_column(String(16), nullable=True)
    summary: Mapped[dict] = mapped_column(JSON, default=dict)
    __table_args__ = (
        Index("ix_observation_tenant_time", "tenant_id", "project", "occurred_at"),
        Index("ix_observation_owner_time", "tenant_id", "owner_key", "occurred_at"),
        Index("ix_observation_trace_time", "tenant_id", "trace_id", "occurred_at"),
        Index("ix_observation_run_time", "tenant_id", "run_id", "occurred_at"),
    )


class IdAllocator(Base):
    __tablename__ = "id_allocator"
    name: Mapped[str] = mapped_column(String(32), primary_key=True)
    value: Mapped[int] = mapped_column(BigInteger)


class GenerationLease(Base):
    __tablename__ = "generation_leases"
    message_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(BigInteger, index=True)
    request_id: Mapped[str] = mapped_column(String(80), index=True)
    owner_token: Mapped[str] = mapped_column(String(64))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    state: Mapped[str] = mapped_column(String(32), default="active")


class OutboxEvent(Base):
    __tablename__ = "outbox_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), index=True)
    event_key: Mapped[str] = mapped_column(String(160))
    kind: Mapped[str] = mapped_column(String(40))
    entity_id: Mapped[int] = mapped_column(BigInteger, index=True)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(24), default="pending", index=True)
    owner_token: Mapped[str | None] = mapped_column(String(64), nullable=True)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    job_id: Mapped[str | None] = mapped_column(String(160), nullable=True)
    last_error: Mapped[str] = mapped_column(String(100), default="")
    consume_status: Mapped[str] = mapped_column(String(24), default="pending")
    consume_token: Mapped[str | None] = mapped_column(String(64), nullable=True)
    consume_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    result: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    __table_args__ = (UniqueConstraint("tenant_id", "event_key", name="uq_outbox_tenant_key"),)


class DependencyCircuit(Base):
    __tablename__ = "dependency_circuits"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    state: Mapped[str] = mapped_column(String(16), default="CLOSED")
    failures: Mapped[int] = mapped_column(Integer, default=0)
    window_started: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    open_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    probe_token: Mapped[str | None] = mapped_column(String(64), nullable=True)
    probe_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    epoch: Mapped[int] = mapped_column(Integer, default=0)


class AdmissionWindow(Base):
    __tablename__ = "admission_windows"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    requests: Mapped[int] = mapped_column(Integer, default=0)
    reserved_units: Mapped[int] = mapped_column(BigInteger, default=0)


class AdmissionLease(Base):
    __tablename__ = "admission_leases"
    token: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[int] = mapped_column(BigInteger, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    finished: Mapped[bool] = mapped_column(Boolean, default=False)


class Tenant(Base):
    __tablename__ = "tenants"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    code: Mapped[int] = mapped_column(Integer, unique=True, index=True)
    name: Mapped[str] = mapped_column(String(120))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class UploadReservation(Base):
    __tablename__ = "upload_reservations"
    token: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), index=True)
    reserved_bytes: Mapped[int] = mapped_column(BigInteger)
    file_count: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), index=True)
    username: Mapped[str] = mapped_column(String(120), index=True)
    nickname: Mapped[str] = mapped_column(String(120), default="")
    password_hash: Mapped[str] = mapped_column(String(255))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    is_super_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    phone: Mapped[str] = mapped_column(String(32), default="")
    email: Mapped[str] = mapped_column(String(160), default="")
    department_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)
    position_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)
    avatar_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    approval_status: Mapped[str] = mapped_column(String(20), default="approved")
    profile: Mapped[dict] = mapped_column(JSON, default=dict)

    __table_args__ = (UniqueConstraint("tenant_id", "username", name="uq_users_tenant_username"),)


class RegistrationCode(Base):
    __tablename__ = "registration_codes"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), index=True)
    code_hash: Mapped[str] = mapped_column(String(64), unique=True)
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    used_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Role(Base):
    __tablename__ = "roles"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), index=True)
    name: Mapped[str] = mapped_column(String(120))
    description: Mapped[str] = mapped_column(String(500), default="")


class Permission(Base):
    __tablename__ = "permissions"

    id: Mapped[str] = mapped_column(String(160), primary_key=True)
    code: Mapped[str] = mapped_column(String(160), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(160))
    category: Mapped[str] = mapped_column(String(80), default="api")


class UserRole(Base):
    __tablename__ = "user_roles"

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), primary_key=True)
    role_id: Mapped[int] = mapped_column(ForeignKey("roles.id"), primary_key=True)


class RolePermission(Base):
    __tablename__ = "role_permissions"

    role_id: Mapped[int] = mapped_column(ForeignKey("roles.id"), primary_key=True)
    permission_id: Mapped[str] = mapped_column(ForeignKey("permissions.id"), primary_key=True)


class Department(Base):
    __tablename__ = "departments"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), index=True)
    parent_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(String(500), default="")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Position(Base):
    __tablename__ = "positions"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(String(500), default="")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AIModel(Base):
    __tablename__ = "ai_models"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), index=True)
    provider: Mapped[str] = mapped_column(String(80), default="openai_compatible")
    model_type: Mapped[str] = mapped_column(String(40), default="chat")
    endpoint: Mapped[str] = mapped_column(String(500))
    model_name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(String(500), default="")
    embedding_dimensions: Mapped[int | None] = mapped_column(Integer, nullable=True)
    max_input_tokens: Mapped[int] = mapped_column(Integer, default=131072)
    max_output_tokens: Mapped[int] = mapped_column(Integer, default=8192)
    chat_parameter_profile: Mapped[str] = mapped_column(String(40), default="auto", server_default="auto")
    reasoning_policy: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    search_mode: Mapped[str] = mapped_column(String(24), default='disabled', server_default='disabled')
    secret_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    secret_hint: Mapped[str | None] = mapped_column(String(24), nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class Resource(Base):
    """Tenant-bound records for AI apps, prompts, knowledge bases, skills, and organization data.

    This compatibility store keeps initial empty-state development operational. It
    is intentionally namespaced so later M2 Alembic migrations can normalize a
    resource type into dedicated tables without changing legacy API routes.
    """

    __tablename__ = "resources"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), index=True)
    resource_type: Mapped[str] = mapped_column(String(80), index=True)
    legacy_source_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)
    name: Mapped[str] = mapped_column(String(500), default="")
    owner_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True, index=True)
    data: Mapped[dict] = mapped_column(JSON, default=dict)
    is_deleted: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class MessageReadReceipt(Base):
    __tablename__ = "message_read_receipts"

    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), primary_key=True)
    message_id: Mapped[int] = mapped_column(ForeignKey("resources.id"), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), primary_key=True)
    read_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    __table_args__ = (Index("ix_message_read_receipts_tenant_user", "tenant_id", "user_id"),)


class LegacyRecord(Base):
    """Redacted source-row archive used to retain fields with no active target model."""

    __tablename__ = "legacy_records"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    source_table: Mapped[str] = mapped_column(String(160), index=True)
    source_id: Mapped[str] = mapped_column(String(160))
    tenant_id: Mapped[int | None] = mapped_column(ForeignKey("tenants.id"), nullable=True, index=True)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    imported_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        UniqueConstraint("source_table", "source_id", name="uq_legacy_records_source"),
    )


class ChatEvent(Base):
    __tablename__ = "chat_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), index=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, index=True)
    request_id: Mapped[str] = mapped_column(String(80), index=True)
    event_type: Mapped[str] = mapped_column(String(80))
    payload: Mapped[str] = mapped_column(Text)
    sequence: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ScheduledTask(Base):
    __tablename__ = "scheduled_tasks"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    cron: Mapped[str] = mapped_column(String(120))
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    task_type: Mapped[str] = mapped_column(String(24), default="cron")
    run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    max_retries: Mapped[int] = mapped_column(Integer, default=3)
    status: Mapped[str] = mapped_column(String(32), default="ready")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    owner_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    schedule_revision: Mapped[int] = mapped_column(Integer, default=0)
    time_zone: Mapped[str] = mapped_column(String(80), default="Asia/Shanghai")
    schedule_policy: Mapped[str] = mapped_column(String(24), default="hold")
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class TaskRun(Base):
    __tablename__ = "task_runs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), index=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("scheduled_tasks.id"), index=True)
    job_id: Mapped[str | None] = mapped_column(String(80), nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(32), default="pending")
    attempt: Mapped[int] = mapped_column(Integer, default=0)
    result: Mapped[dict] = mapped_column(JSON, default=dict)
    error_message: Mapped[str] = mapped_column(String(1000), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    schedule_revision: Mapped[int] = mapped_column(Integer, default=0)
    scheduled_for: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    input_snapshot: Mapped[dict] = mapped_column(JSON, default=dict)


class TaskGraphLease(Base):
    __tablename__ = "task_graph_leases"

    run_id: Mapped[int] = mapped_column(ForeignKey("task_runs.id"), primary_key=True)
    binding_digest: Mapped[str] = mapped_column(String(64))
    worker_token: Mapped[str] = mapped_column(String(64))
    fencing_version: Mapped[int] = mapped_column(Integer)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    state: Mapped[str] = mapped_column(String(24), default="active")


class TaskGraphWrite(Base):
    __tablename__ = "task_graph_writes"

    run_id: Mapped[int] = mapped_column(ForeignKey("task_runs.id"), primary_key=True)
    checkpoint_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    task_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    write_index: Mapped[int] = mapped_column(Integer, primary_key=True)
    channel: Mapped[str] = mapped_column(String(160))
    worker_token: Mapped[str] = mapped_column(String(64))
    fencing_version: Mapped[int] = mapped_column(Integer)
    payload_type: Mapped[str] = mapped_column(String(24))
    payload_encrypted: Mapped[str] = mapped_column(Text)


class TaskGraphCheckpoint(Base):
    __tablename__ = "task_graph_checkpoints"

    run_id: Mapped[int] = mapped_column(ForeignKey("task_runs.id"), primary_key=True)
    namespace: Mapped[str] = mapped_column(String(128), primary_key=True, default="")
    checkpoint_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    parent_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), index=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("scheduled_tasks.id"), index=True)
    schedule_revision: Mapped[int] = mapped_column(Integer)
    worker_token: Mapped[str] = mapped_column(String(64))
    fencing_version: Mapped[int] = mapped_column(Integer)
    graph_version: Mapped[str] = mapped_column(String(80))
    schema_version: Mapped[int] = mapped_column(Integer, default=1)
    payload_type: Mapped[str] = mapped_column(String(24))
    payload_encrypted: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (Index("ix_task_graph_checkpoints_run_created", "run_id", "created_at"),)


class AIApp(Base):
    __tablename__ = "ai_apps"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(String(500), default="")
    chat_model_id: Mapped[int | None] = mapped_column(ForeignKey("ai_models.id"), nullable=True)
    prompt_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    knowledge_base_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    config: Mapped[dict] = mapped_column(JSON, default=dict)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)


class AIPrompt(Base):
    __tablename__ = "ai_prompts"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    content: Mapped[str] = mapped_column(Text, default="")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)


class KnowledgeBase(Base):
    __tablename__ = "knowledge_bases"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    embedding_model_id: Mapped[int | None] = mapped_column(ForeignKey("ai_models.id"), nullable=True)
    rerank_model_id: Mapped[int | None] = mapped_column(ForeignKey("ai_models.id"), nullable=True)
    inherits_embedding_model: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    inherits_rerank_model: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    chunk_size: Mapped[int] = mapped_column(Integer, default=1200)
    chunk_overlap: Mapped[int] = mapped_column(Integer, default=180)
    status: Mapped[str] = mapped_column(String(40), default="ready")
    retrieval_strategy: Mapped[str] = mapped_column(String(32), default="full")
    retrieval_config: Mapped[dict] = mapped_column(JSON, default=dict)


class KnowledgeDocument(Base):
    __tablename__ = "knowledge_documents"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), index=True)
    knowledge_base_id: Mapped[int] = mapped_column(ForeignKey("knowledge_bases.id"), index=True)
    file_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    source_name: Mapped[str] = mapped_column(String(500))
    content: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(40), default="pending")
    error_message: Mapped[str] = mapped_column(String(1000), default="")
    extraction_report: Mapped[dict] = mapped_column(JSON, default=dict)
    is_deleted: Mapped[bool] = mapped_column(Boolean, default=False)
    source_revision: Mapped[int] = mapped_column(Integer, default=0)
    active_source_revision: Mapped[int] = mapped_column(Integer, default=0)
    active_index_version: Mapped[int] = mapped_column(BigInteger, default=0)
    pending_index_version: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    parse_status: Mapped[str] = mapped_column(String(32), default="pending")
    embedding_status: Mapped[str] = mapped_column(String(32), default="pending")
    index_status: Mapped[str] = mapped_column(String(32), default="pending")
    index_error: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class Skill(Base):
    __tablename__ = "skills"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(String(500), default="")
    skill_type: Mapped[str] = mapped_column(String(40), default="tool")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    config: Mapped[dict] = mapped_column(JSON, default=dict)


class ChatSession(Base):
    __tablename__ = "chat_sessions"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), index=True)
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    app_id: Mapped[int | None] = mapped_column(ForeignKey("ai_apps.id"), nullable=True)
    title: Mapped[str] = mapped_column(String(500), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class ChatMessage(Base):
    __tablename__ = "chat_messages"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), index=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("chat_sessions.id"), index=True)
    role: Mapped[str] = mapped_column(String(20))
    content: Mapped[str] = mapped_column(Text)
    request_id: Mapped[str | None] = mapped_column(String(80), nullable=True, index=True)
    token_usage: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(32), default="completed")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ContextJob(Base):
    __tablename__ = "context_jobs"
    __table_args__ = (UniqueConstraint("tenant_id", "job_key", name="uq_context_job_key"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(BigInteger, index=True)
    owner_id: Mapped[int] = mapped_column(BigInteger, index=True)
    session_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)
    kind: Mapped[str] = mapped_column(String(32))
    job_key: Mapped[str] = mapped_column(String(160))
    source_hash: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    run_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    lease_token: Mapped[str | None] = mapped_column(String(64), nullable=True)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    result_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    usage: Mapped[dict] = mapped_column(JSON, default=dict)
    error_code: Mapped[str] = mapped_column(String(120), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ChatSummary(Base):
    __tablename__ = "chat_summaries"
    __table_args__ = (UniqueConstraint("session_id", "version", name="uq_chat_summary_version"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(BigInteger, index=True)
    owner_id: Mapped[int] = mapped_column(BigInteger, index=True)
    session_id: Mapped[int] = mapped_column(BigInteger, index=True)
    job_id: Mapped[int] = mapped_column(BigInteger)
    version: Mapped[int] = mapped_column(Integer)
    through_id: Mapped[int] = mapped_column(BigInteger)
    source_ids: Mapped[list] = mapped_column(JSON, default=list)
    source_hash: Mapped[str] = mapped_column(String(64))
    config_hash: Mapped[str] = mapped_column(String(64))
    content: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(24), default="active", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ContextArtifact(Base):
    __tablename__ = "context_artifacts"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(BigInteger, index=True)
    owner_id: Mapped[int] = mapped_column(BigInteger, index=True)
    session_id: Mapped[int] = mapped_column(BigInteger, index=True)
    request_id: Mapped[str] = mapped_column(String(80), index=True)
    call_id: Mapped[str] = mapped_column(String(120))
    content_hash: Mapped[str] = mapped_column(String(64))
    content_encrypted: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AgentRun(Base):
    __tablename__ = "agent_runs"

    run_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), index=True)
    session_id: Mapped[int | None] = mapped_column(ForeignKey("chat_sessions.id"), nullable=True, index=True)
    request_id: Mapped[str | None] = mapped_column(String(80), nullable=True, index=True)
    trace_id: Mapped[str] = mapped_column(String(64), index=True)
    graph_version: Mapped[str] = mapped_column(String(80))
    status: Mapped[str] = mapped_column(String(32), default="running", index=True)
    current_node: Mapped[str] = mapped_column(String(80), default="start")
    event_seq: Mapped[int] = mapped_column(Integer, default=0)
    checkpoint_seq: Mapped[int] = mapped_column(Integer, default=0)
    step_count: Mapped[int] = mapped_column(Integer, default=0)
    error_code: Mapped[str] = mapped_column(String(120), default="")
    run_metadata: Mapped[dict] = mapped_column("metadata", JSON, default=dict)
    result: Mapped[dict] = mapped_column(JSON, default=dict)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    heartbeat_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AgentRunLease(Base):
    __tablename__ = "agent_run_leases"

    run_id: Mapped[str] = mapped_column(ForeignKey("agent_runs.run_id"), primary_key=True)
    worker_token: Mapped[str] = mapped_column(String(64), default="")
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    state: Mapped[str] = mapped_column(String(24), default="released", index=True)
    heartbeat_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    attempt: Mapped[int] = mapped_column(Integer, default=0)


class AgentEvent(Base):
    __tablename__ = "agent_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("agent_runs.run_id"), index=True)
    sequence: Mapped[int] = mapped_column(Integer)
    node_name: Mapped[str] = mapped_column(String(80))
    event_type: Mapped[str] = mapped_column(String(80))
    status: Mapped[str] = mapped_column(String(32), default="completed")
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    payload_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        UniqueConstraint("run_id", "sequence", name="uq_agent_events_run_sequence"),
        Index("ix_agent_events_run_created", "run_id", "created_at"),
    )


class AgentCheckpoint(Base):
    __tablename__ = "agent_checkpoints"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("agent_runs.run_id"), index=True)
    checkpoint_seq: Mapped[int] = mapped_column(Integer)
    node_name: Mapped[str] = mapped_column(String(80))
    state_schema_version: Mapped[int] = mapped_column(Integer, default=1)
    state_json: Mapped[dict] = mapped_column(JSON, default=dict)
    state_hash: Mapped[str] = mapped_column(String(64))
    resume_policy: Mapped[str] = mapped_column(String(32), default="safe_replay")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        UniqueConstraint("run_id", "checkpoint_seq", name="uq_agent_checkpoints_run_sequence"),
        Index("ix_agent_checkpoints_run_created", "run_id", "created_at"),
    )


class AgentGraphCheckpoint(Base):
    __tablename__ = "agent_graph_checkpoints"

    run_id: Mapped[str] = mapped_column(ForeignKey("agent_runs.run_id"), primary_key=True)
    namespace: Mapped[str] = mapped_column(String(128), primary_key=True, default="")
    checkpoint_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    parent_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    graph_version: Mapped[str] = mapped_column(String(80))
    schema_version: Mapped[int] = mapped_column(Integer, default=1)
    payload_type: Mapped[str] = mapped_column(String(24))
    payload_encrypted: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AgentGraphWrite(Base):
    __tablename__ = "agent_graph_writes"

    run_id: Mapped[str] = mapped_column(ForeignKey("agent_runs.run_id"), primary_key=True)
    namespace: Mapped[str] = mapped_column(String(128), primary_key=True, default="")
    checkpoint_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    task_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    write_index: Mapped[int] = mapped_column(Integer, primary_key=True)
    channel: Mapped[str] = mapped_column(String(160))
    payload_type: Mapped[str] = mapped_column(String(24))
    payload_encrypted: Mapped[str] = mapped_column(Text)


class AgentToolCall(Base):
    __tablename__ = "agent_tool_calls"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("agent_runs.run_id"), index=True)
    call_id: Mapped[str] = mapped_column(String(120))
    tool_name: Mapped[str] = mapped_column(String(200))
    input_digest: Mapped[str] = mapped_column(String(64))
    output_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(32))
    timeout_ms: Mapped[int] = mapped_column(Integer, default=0)
    retry_count: Mapped[int] = mapped_column(Integer, default=0)
    effect_status: Mapped[str] = mapped_column(String(32), default="none")
    error_code: Mapped[str] = mapped_column(String(120), default="")
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        UniqueConstraint("run_id", "call_id", name="uq_agent_tool_calls_run_call"),
        Index("ix_agent_tool_calls_run_created", "run_id", "created_at"),
    )


class KnowledgeChunk(Base):
    """Durable ingestion metadata; embeddings themselves are stored in PGVector."""

    __tablename__ = "knowledge_chunks"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), index=True)
    knowledge_base_id: Mapped[int] = mapped_column(ForeignKey("knowledge_bases.id"), index=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("knowledge_documents.id"), index=True)
    parent_chunk_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)
    chunk_index: Mapped[int] = mapped_column(Integer)
    index_version: Mapped[int] = mapped_column(BigInteger, default=0)
    content: Mapped[str] = mapped_column(Text)
    content_hash: Mapped[str] = mapped_column(String(64), index=True)
    attributes: Mapped[dict] = mapped_column("metadata", JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        UniqueConstraint("document_id", "index_version", "chunk_index", name="uq_knowledge_chunks_version_index"),
    )


class KnowledgeSparseChunk(Base):
    __tablename__ = "knowledge_sparse_chunks"

    chunk_id: Mapped[int] = mapped_column(ForeignKey("knowledge_chunks.id", ondelete="CASCADE"), primary_key=True)
    content_hash: Mapped[str] = mapped_column(String(64))
    tokenizer_version: Mapped[str] = mapped_column(String(64))
    token_count: Mapped[int] = mapped_column(Integer)


class KnowledgeSparseTerm(Base):
    __tablename__ = "knowledge_sparse_terms"

    term_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    chunk_id: Mapped[int] = mapped_column(ForeignKey("knowledge_sparse_chunks.chunk_id", ondelete="CASCADE"), primary_key=True)
    frequency: Mapped[int] = mapped_column(Integer)
    __table_args__ = (Index("ix_sparse_terms_chunk", "chunk_id"),)


class KnowledgeIngestionRun(Base):
    __tablename__ = "knowledge_ingestion_runs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), index=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("knowledge_documents.id"), index=True)
    status: Mapped[str] = mapped_column(String(32), default="pending")
    chunk_count: Mapped[int] = mapped_column(Integer, default=0)
    embedding_count: Mapped[int] = mapped_column(Integer, default=0)
    error_message: Mapped[str] = mapped_column(String(1000), default="")
    source_revision: Mapped[int] = mapped_column(Integer, default=0)
    parameter_hash: Mapped[str] = mapped_column(String(64), default="")
    parameters: Mapped[dict] = mapped_column(JSON, default=dict)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)



class KnowledgeEvalSet(Base):
    __tablename__ = "knowledge_eval_sets"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), index=True)
    knowledge_base_id: Mapped[int] = mapped_column(ForeignKey("knowledge_bases.id"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(String(500), default="")
    last_result: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class KnowledgeEvalCase(Base):
    __tablename__ = "knowledge_eval_cases"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), index=True)
    eval_set_id: Mapped[int] = mapped_column(ForeignKey("knowledge_eval_sets.id"), index=True)
    query: Mapped[str] = mapped_column(Text)
    expected_document_ids: Mapped[list] = mapped_column(JSON, default=list)
    expected_chunk_ids: Mapped[list] = mapped_column(JSON, default=list)
    expected_answer: Mapped[str] = mapped_column(Text, default="")
    complexity: Mapped[str] = mapped_column(String(32), default="medium")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class KnowledgeEvalRun(Base):
    __tablename__ = "knowledge_eval_runs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), index=True)
    eval_set_id: Mapped[int] = mapped_column(ForeignKey("knowledge_eval_sets.id"), index=True)
    knowledge_base_id: Mapped[int] = mapped_column(ForeignKey("knowledge_bases.id"), index=True)
    k: Mapped[int] = mapped_column(Integer, default=8)
    strategy: Mapped[str] = mapped_column(String(32), default="full")
    result: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AgentMemory(Base):
    __tablename__ = "agent_memories"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), index=True)
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    agent_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)
    session_id: Mapped[int | None] = mapped_column(ForeignKey("chat_sessions.id"), nullable=True, index=True)
    content: Mapped[str] = mapped_column(Text)
    attributes: Mapped[dict] = mapped_column("metadata", JSON, default=dict)
    memory_type: Mapped[str] = mapped_column(String(32), default="long_term")
    memory_key: Mapped[str | None] = mapped_column(String(160), nullable=True, index=True)
    source_priority: Mapped[int] = mapped_column(Integer, default=0)
    importance: Mapped[float] = mapped_column(Float, default=0.5)
    confidence: Mapped[float] = mapped_column(Float, default=0.5)
    source_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)
    valid_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    valid_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(24), default="active", index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    content_hash: Mapped[str] = mapped_column(String(64), default="")
    supersedes_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)
    index_status: Mapped[str] = mapped_column(String(24), default="unconfigured")
    embedding_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    invalidation_reason: Mapped[str] = mapped_column(String(120), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class MemoryVector(Base):
    __tablename__ = "memory_vectors"
    __table_args__ = (UniqueConstraint("memory_id", "memory_version", "model_fingerprint", name="uq_memory_vector_binding"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(BigInteger, index=True)
    owner_id: Mapped[int] = mapped_column(BigInteger, index=True)
    memory_id: Mapped[int] = mapped_column(BigInteger, index=True)
    memory_version: Mapped[int] = mapped_column(Integer)
    content_hash: Mapped[str] = mapped_column(String(64))
    model_fingerprint: Mapped[str] = mapped_column(String(64))
    dimensions: Mapped[int] = mapped_column(Integer)
    vector: Mapped[list] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(24), default="ready")


class MemorySearchDocument(Base):
    __tablename__ = "memory_search_documents"
    memory_id: Mapped[int] = mapped_column(ForeignKey("agent_memories.id", ondelete="CASCADE"), primary_key=True)
    memory_version: Mapped[int] = mapped_column(Integer)
    content_hash: Mapped[str] = mapped_column(String(64))
    tokenizer_version: Mapped[str] = mapped_column(String(40))


class MemorySearchTerm(Base):
    __tablename__ = "memory_search_terms"
    term_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    memory_id: Mapped[int] = mapped_column(ForeignKey("memory_search_documents.memory_id", ondelete="CASCADE"), primary_key=True)
    __table_args__ = (Index("ix_memory_search_term_memory", "memory_id"),)


class MemoryProjectionReceipt(Base):
    __tablename__ = "memory_projection_receipts"
    vector_id: Mapped[int] = mapped_column(ForeignKey("memory_vectors.id", ondelete="CASCADE"), primary_key=True)
    content_hash: Mapped[str] = mapped_column(String(64))
    projection_version: Mapped[str] = mapped_column(String(40))
    generation: Mapped[int] = mapped_column(BigInteger)


class MemoryTombstone(Base):
    __tablename__ = "memory_tombstones"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(BigInteger, index=True)
    owner_id: Mapped[int] = mapped_column(BigInteger, index=True)
    agent_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)
    memory_id: Mapped[int] = mapped_column(BigInteger)
    memory_key: Mapped[str | None] = mapped_column(String(160), nullable=True)
    content_hash: Mapped[str] = mapped_column(String(64))
    source_through_id: Mapped[int] = mapped_column(BigInteger, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ToolAudit(Base):
    __tablename__ = "tool_audits"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), index=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True, index=True)
    chat_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)
    tool_name: Mapped[str] = mapped_column(String(200))
    tool_type: Mapped[str] = mapped_column(String(40), default="local")
    outcome: Mapped[str] = mapped_column(String(32))
    input_digest: Mapped[str] = mapped_column(String(64))
    output_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    error_message: Mapped[str] = mapped_column(String(1000), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ToolExecutionIntent(Base):
    __tablename__ = "tool_execution_intents"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    tenant_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    tool_name: Mapped[str] = mapped_column(String(200), nullable=False)
    idempotency_key_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    input_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False)
    result_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    result_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "user_id", "idempotency_key_digest",
            name="uq_tool_intent_scope_key",
        ),
        Index("ix_tool_intents_tenant_created", "tenant_id", "created_at"),
    )
