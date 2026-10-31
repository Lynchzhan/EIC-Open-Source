from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

def _backend_root() -> Path:
    module = Path(__file__).resolve()
    # wheel安装层级不同；仅沿模块所属祖先寻找同一部署内的发布资源。
    for parent in module.parents:
        if (
            (parent / "pyproject.toml").is_file()
            and (parent / "alembic.ini").is_file()
            and (parent / "migrations" / "env.py").is_file()
        ):
            return parent
    return module.parents[3]


BACKEND_ROOT = _backend_root()


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="EIC_", extra="ignore")

    app_name: str = "Enterprise Intelligence Center"
    environment: str = "development"
    # 独立体验控制库与到期删除的正文库；正文库必须排除在备份之外。
    experience_root: str = "runtime/experience"
    experience_content_root: str = "runtime/experience-content"
    # 旧开关仅用于拒绝过期部署配置；应用启动不再写入 schema。
    auto_create_schema: bool = False
    runtime_schema_updates: bool = False
    generation_recovery_enabled: bool = True
    outbox_dispatch_enabled: bool = True
    outbox_inline_concurrency: int = Field(default=2, ge=1, le=10)
    # 空列表沿用全量扫描；轻量部署可仅启用已授权的后台任务类型。
    outbox_dispatch_kinds: list[Literal[
        "knowledge-base", "knowledge-document", "document-embeddings", "index-cleanup",
        "scheduled-task", "chat-compaction", "memory-write", "memory-extraction", "memory-index",
        "conversation-label", "memory-projection",
    ]] = Field(default_factory=list)
    observability_enabled: bool = True
    observability_retention_days: int = Field(default=30, ge=1, le=365)
    observability_queue_size: int = Field(default=512, ge=1, le=4096)
    observability_write_timeout: float = Field(default=0.5, ge=0.05, le=5)
    observability_lock_retry_timeout: float = Field(default=3, ge=0.05, le=10)
    # 独立观测旁路；关闭时不建立客户端、后台任务或外部连接。
    langfuse_enabled: bool | None = False
    langfuse_base_url: str | None = Field(default="", repr=False)
    langfuse_public_key: SecretStr | None = Field(default_factory=lambda: SecretStr(""))
    langfuse_secret_key: SecretStr | None = Field(default_factory=lambda: SecretStr(""))
    langfuse_namespace: str | None = "eic"
    langfuse_queue_size: int | None = Field(default=256, ge=1, le=4096)
    langfuse_batch_size: int | None = Field(default=32, ge=1, le=128)
    langfuse_timeout_seconds: float | None = Field(default=2.0, ge=0.1, le=10)
    langfuse_shutdown_seconds: float | None = Field(default=2.0, ge=0.1, le=10)
    langfuse_max_retries: int | None = Field(default=1, ge=0, le=2)
    langfuse_ui_base_url: str | None = Field(default="", repr=False)
    langfuse_project_id: str | None = Field(default="", repr=False)

    @field_validator(
        "langfuse_enabled", "langfuse_base_url", "langfuse_public_key", "langfuse_secret_key",
        "langfuse_namespace", "langfuse_queue_size", "langfuse_batch_size",
        "langfuse_timeout_seconds", "langfuse_shutdown_seconds", "langfuse_max_retries",
        "langfuse_ui_base_url", "langfuse_project_id", mode="wrap",
    )
    @classmethod
    def isolate_langfuse_config(cls, value, handler):
        # 无效值保留为 None，由出口拒绝；旁路配置不能阻断业务配置加载。
        try:
            return handler(value)
        except (ValueError, TypeError):
            return None

    dependency_controls_enabled: bool = False
    circuit_failure_threshold: int = Field(default=5, ge=1, le=100)
    circuit_window_seconds: int = Field(default=60, ge=1, le=3600)
    circuit_open_seconds: int = Field(default=30, ge=1, le=3600)
    circuit_probe_seconds: int = Field(default=210, ge=1, le=3600)
    provider_total_timeout_seconds: int = Field(default=180, ge=1, le=1800)
    provider_rate_limit_retries: int = Field(default=1, ge=0, le=1)
    embedding_provider_max_batch_size: int = Field(default=8, ge=1, le=256)
    admission_enabled: bool = False
    admission_global_concurrency: int | None = Field(default=None, gt=0)
    admission_tenant_concurrency: int | None = Field(default=None, gt=0)
    admission_global_rate: int | None = Field(default=None, gt=0)
    admission_tenant_rate: int | None = Field(default=None, gt=0)
    admission_global_units: int | None = Field(default=None, gt=0)
    admission_tenant_units: int | None = Field(default=None, gt=0)
    # D04/D10 待确认；默认不启动自动业务，也不推断补跑/DST策略。
    task_execution_enabled: bool = False
    chat_attachments_enabled: bool = True
    chat_attachment_max_files: int | None = Field(default=8, gt=0, le=20)
    chat_attachment_max_bytes: int | None = Field(default=16 * 1024 * 1024, gt=0, le=16 * 1024 * 1024)
    chat_attachment_max_chars: int | None = Field(default=80000, gt=0, le=1000000)
    chat_attachment_max_expanded_bytes: int = Field(default=64 * 1024 * 1024, ge=1024, le=256 * 1024 * 1024)
    chat_attachment_max_pages: int = Field(default=100, ge=1, le=500)
    memory_recall_max_candidates: int = Field(default=4096, ge=64, le=50000)
    memory_recall_max_seconds: float = Field(default=2, ge=0.05, le=10)
    memory_candidate_index_enabled: bool = True
    memory_vector_projection_enabled: bool = False
    chat_search_backend: Literal['none', 'searxng', 'tavily'] = 'none'
    chat_search_endpoint: str = Field(default='', repr=False)
    chat_search_api_key: SecretStr = Field(default_factory=lambda: SecretStr(''))
    chat_search_max_results: int = Field(default=5, ge=1, le=10)
    chat_search_max_chars: int = Field(default=12000, ge=1000, le=50000)
    chat_search_timeout_seconds: float = Field(default=20, ge=1, le=60)
    database_url: str = "sqlite+aiosqlite:///./eic.db"
    vector_database_url: str = "postgresql+asyncpg://eic:change-me@127.0.0.1:5432/eic_vector"
    redis_url: str = "redis://127.0.0.1:6379/0"
    qdrant_url: str = "http://127.0.0.1:6333"
    skill_root: str = str(BACKEND_ROOT / "skills")
    upload_root: str = str(BACKEND_ROOT / "uploads")
    # None 表示业务策略待批准；不在升级时静默设定生产限额。
    upload_max_file_bytes: int | None = Field(default=None, gt=0)
    upload_max_request_bytes: int | None = Field(default=None, gt=0)
    upload_max_body_bytes: int | None = Field(default=None, gt=0)
    upload_max_files: int | None = Field(default=None, gt=0)
    upload_tenant_max_bytes: int | None = Field(default=None, gt=0)
    upload_tenant_max_files: int | None = Field(default=None, gt=0)
    upload_chunk_bytes: int = Field(default=65536, ge=4096, le=1048576)
    # 仅显式启用私有 sidecar；默认保留现有 PDF 抽取策略。
    pdf_extractor: Literal["local", "mineru"] = "local"
    document_max_bytes: int = Field(default=64 * 1024 * 1024, ge=1024, le=256 * 1024 * 1024)
    document_max_expanded_bytes: int = Field(default=128 * 1024 * 1024, ge=1024, le=512 * 1024 * 1024)
    document_max_pages: int = Field(default=500, ge=1, le=2000)
    document_max_pixels: int = Field(default=20_000_000, ge=100000, le=40_000_000)
    document_max_cells: int = Field(default=1_000_000, ge=100, le=5_000_000)
    document_max_chars: int = Field(default=2_000_000, ge=1024, le=8_000_000)
    document_parse_timeout_seconds: int = Field(default=120, ge=1, le=600)
    document_parse_memory_mb: int = Field(default=512, ge=128, le=4096)
    mineru_endpoint: str = Field(default="", repr=False)
    mineru_allowed_ips: str = ""
    mineru_api_key: SecretStr = Field(default_factory=lambda: SecretStr(""))
    mineru_tier: Literal["basic", "standard", "advanced"] = "basic"
    mineru_ocr_mode: Literal["auto", "txt", "ocr"] = "auto"
    mineru_fallback_local: bool = True
    mineru_timeout_seconds: float = Field(default=120, ge=1, le=600)
    mineru_poll_interval_seconds: float = Field(default=1, ge=0.01, le=10)
    mineru_max_polls: int = Field(default=120, ge=1, le=600)
    mineru_max_pdf_bytes: int = Field(default=32 * 1024 * 1024, ge=1024, le=128 * 1024 * 1024)
    mineru_max_response_bytes: int = Field(default=16 * 1024 * 1024, ge=1024, le=64 * 1024 * 1024)
    mineru_max_text_chars: int = Field(default=2000000, ge=1024, le=8000000)
    mineru_max_pages: int = Field(default=2000, ge=1, le=10000)
    jwt_secret: str = "replace-before-production-with-a-random-secret"
    secret_encryption_key: str = "replace-before-production-with-a-random-secret"
    jwt_algorithm: str = "HS256"
    access_token_minutes: int = 60
    cors_origins: str = "http://127.0.0.1:5174"
    bootstrap_admin: str = "admin"
    # 保留开发环境初始化兼容性；部署必须显式关闭独立开关。
    bootstrap_enabled: bool = True
    bootstrap_admin_password: str = ""
    seed_demo_data: bool = False
    # 预留配置只能保持关闭；启用受控公共出口需另行实现和批准。
    remote_file_import_enabled: bool = False
    demo_user_password: str = ""
    require_runtime_dependencies: bool = False
    serve_frontend: bool = False
    frontend_dist: str = ""

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]

    def frontend_dist_path(self) -> Path:
        if self.frontend_dist:
            return self.runtime_path(self.frontend_dist)
        return Path(__file__).resolve().parents[4] / "frontend" / "dist"

    def runtime_path(self, value: str) -> Path:
        """Resolve relative runtime paths from backend/, independent of cwd."""
        path = Path(value).expanduser()
        return path if path.is_absolute() else BACKEND_ROOT / path

    def validate_production_security(self) -> None:
        if self.environment.casefold() in {"development", "test", "testing", "local"} and not self.require_runtime_dependencies:
            return
        values = (self.jwt_secret, self.secret_encryption_key)
        for value in values:
            lowered = value.casefold()
            if (len(value) < 32 or len(set(value)) < 12
                    or any(marker in lowered for marker in ("replace-", "change-me", "changeme", "example", "placeholder"))):
                raise ValueError("生产签名和加密密钥必须分别配置为足够强度的随机值")
        if self.jwt_secret == self.secret_encryption_key:
            raise ValueError("生产签名密钥与存储加密密钥必须分离")


@lru_cache
def get_settings() -> Settings:
    return Settings()
