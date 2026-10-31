import os
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path

from alembic.script import ScriptDirectory


def test_initial_schema_migrates_into_an_empty_database(tmp_path: Path) -> None:
    database_path = tmp_path / "enterprise_intelligence_center.db"
    environment = os.environ.copy()
    environment["EIC_DATABASE_URL"] = f"sqlite+aiosqlite:///{database_path.as_posix()}"

    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=Path(__file__).parents[1],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    with closing(sqlite3.connect(database_path)) as connection:
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        revision = connection.execute("SELECT version_num FROM alembic_version").fetchone()
        observation_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(observability_events)")
        }
        observation_indexes = {
            row[1] for row in connection.execute("PRAGMA index_list(observability_events)")
        }

    head = ScriptDirectory(str(Path(__file__).parents[1] / "migrations")).get_current_head()
    assert revision == (head,)
    assert {
        "tenants", "users", "ai_models", "resources", "chat_events", "scheduled_tasks",
        "ai_apps", "ai_prompts", "knowledge_bases", "knowledge_documents", "skills",
        "chat_sessions", "chat_messages", "knowledge_chunks", "knowledge_ingestion_runs",
        "agent_memories", "tool_audits", "task_runs", "role_permissions", "departments", "positions",
        "legacy_records", "knowledge_eval_sets", "knowledge_eval_cases", "knowledge_eval_runs",
        "id_allocator", "generation_leases", "outbox_events", "agent_runs", "agent_events",
        "agent_checkpoints", "agent_tool_calls", "agent_run_leases",
        "agent_graph_checkpoints", "agent_graph_writes", "observability_events",
        "revoked_sessions", "langfuse_deliveries", "knowledge_sparse_chunks", "knowledge_sparse_terms",
    } <= tables
    assert {
        "event_key", "schema_version", "project", "tenant_id", "owner_key",
        "trace_id", "run_id", "category", "stage", "status", "source",
        "occurred_at", "collected_at", "duration_ms", "usage_source",
        "input_tokens", "output_tokens", "total_tokens", "cached_tokens",
        "saved_tokens", "summary",
    } <= observation_columns
    assert {
        "ix_observation_tenant_time", "ix_observation_owner_time",
        "ix_observation_trace_time", "ix_observation_run_time",
    } <= observation_indexes


def test_0039_backup_copy_upgrades_and_preserves_business_rows(tmp_path):
    original = tmp_path / "synthetic-0039.db"
    restored = tmp_path / "restored-candidate.db"

    def migrate(path, revision):
        environment = {name: value for name, value in os.environ.items()
                       if not name.startswith(("EIC_", "LANGFUSE_", "OTEL_"))}
        # 保留父隔离器的 Python 路径和网络保护，仅使用本测试的临时数据库。
        environment.update(EIC_DATABASE_URL="sqlite+aiosqlite:///" + path.as_posix(),
                           EIC_ENVIRONMENT="testing", EIC_LANGFUSE_ENABLED="false")
        if os.environ.get("EIC_CANDIDATE_NETWORK_LOG"):
            environment["EIC_CANDIDATE_NETWORK_LOG"] = os.environ["EIC_CANDIDATE_NETWORK_LOG"]
        result = subprocess.run([sys.executable, "-B", "-m", "alembic", "upgrade", revision],
            cwd=Path(__file__).parents[1], env=environment, capture_output=True,
            text=True, encoding="utf-8", timeout=60)
        assert result.returncode == 0, result.stderr

    migrate(original, "0039_model_search_mode")
    with closing(sqlite3.connect(original)) as connection:
        connection.execute("INSERT INTO tenants(id,code,name,active,created_at) VALUES(9001,9001,'synthetic-migration',1,CURRENT_TIMESTAMP)")
        connection.commit()
        with closing(sqlite3.connect(restored)) as target:
            connection.backup(target)
    migrate(restored, "head")
    migrate(restored, "head")
    with closing(sqlite3.connect(restored)) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert connection.execute("SELECT name FROM tenants WHERE id=9001").fetchone() == ("synthetic-migration",)
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"revoked_sessions", "langfuse_deliveries", "knowledge_sparse_chunks", "knowledge_sparse_terms"} <= tables
        assert "ix_sparse_terms_chunk" in {row[1] for row in connection.execute("PRAGMA index_list(knowledge_sparse_terms)")}
        indexes = {row[1] for row in connection.execute("PRAGMA index_list(langfuse_deliveries)")}
        assert "ix_langfuse_delivery_pending" in indexes
        sql = "INSERT INTO langfuse_deliveries(event_key,destination,export_key,status,attempts,next_attempt_at,updated_at) VALUES(?,?,?,'pending',0,'2026-10-21','2026-10-21')"
        connection.execute(sql, ("e1", "d1", "x1"))
        import pytest
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(sql, ("e2", "d1", "x1"))
    with closing(sqlite3.connect(original)) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == ("0039_model_search_mode",)
        assert connection.execute("SELECT name FROM tenants WHERE id=9001").fetchone() == ("synthetic-migration",)
        assert connection.execute("SELECT name FROM sqlite_master WHERE name='revoked_sessions'").fetchone() is None
