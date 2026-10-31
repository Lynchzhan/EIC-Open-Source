"""P8 未版本化旧库复制、拒绝条件与历史业务语义回归。"""
import hashlib
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

from alembic.script import ScriptDirectory
import pytest

from eic.core.config import BACKEND_ROOT
from eic.core.legacy_sqlite import (
    expected_signature, orphan_ingestion_rows, prepare_copy, quote, row_digest, rows_digest, signature,
)

HIGH = 2_900_000_000_000_000


def legacy(path):
    statements = (BACKEND_ROOT / "migrations/legacy_cloud_lite.sql").read_text().split(";")
    with sqlite3.connect(path) as connection:
        for sql in sorted((s.strip() for s in statements if s.strip()),
                          key=lambda s: not s.startswith("CREATE TABLE")):
            connection.execute(sql)
        def insert(table, **values):
            fields = connection.execute("PRAGMA table_info(" + quote(table) + ")").fetchall()
            data = {}
            for _, name, kind, required, default, pk in fields:
                if name in values:
                    data[name] = values[name]
                elif required:
                    data[name] = ("2026-10-03 00:00:00" if "DATE" in kind else "{}" if kind == "JSON"
                                  else 0 if kind in {"INTEGER", "BIGINT", "BOOLEAN"} else "")
            columns = ",".join(map(quote, data))
            connection.execute("INSERT INTO " + quote(table) + "(" + columns + ") VALUES("
                               + ",".join("?" for _ in data) + ")", list(data.values()))
        insert("tenants", id=HIGH, code=77, name="P8 synthetic tenant", active=1)
        insert("users", id=HIGH+1, tenant_id=HIGH, username="p8-user", approval_status="pending",
               password_hash="P8-synthetic-hash", is_active=1)
        insert("chat_sessions", id=HIGH+2, tenant_id=HIGH, owner_id=HIGH+1)
        insert("chat_messages", id=HIGH+3, tenant_id=HIGH, session_id=HIGH+2,
               request_id="p8-request", status="generating", role="assistant", content="P8 partial")
        insert("scheduled_tasks", id=HIGH+4, tenant_id=HIGH, enabled=1, task_type="cron",
               status="ready", owner_id=None)
        insert("knowledge_bases", id=HIGH+5, tenant_id=HIGH, status="ready")
        insert("knowledge_documents", id=HIGH+6, tenant_id=HIGH, knowledge_base_id=HIGH+5,
               status="ready", content="P8 synthetic content")
    return path


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_copy_runs_real_migrations_preserves_rows_and_does_not_stamp(tmp_path):
    source = legacy(tmp_path / "old.db")
    before = digest(source)
    record = prepare_copy(source, tmp_path / "new.db")
    assert digest(source) == before
    head = ScriptDirectory(str(BACKEND_ROOT / "migrations")).get_current_head()
    assert record["old_column_values_preserved"] and record["schema_head"] == head
    assert record["provider_requests_replayed"] == 0 and record["expired_generation_leases"] == 1
    assert record["allocator_high"] == HIGH+6
    assert record["snapshot_only"] and record["cutover_requires_writer_freeze_and_new_write_protection"]
    assert "Running upgrade" in record["migration"]["stderr"]
    with sqlite3.connect(source) as old, sqlite3.connect(tmp_path / "new.db") as new:
        for table, spec in signature(old).items():
            columns = [r[1] for r in spec["columns"]]
            assert row_digest(old, table, columns) == row_digest(new, table, columns)
        assert new.execute("PRAGMA foreign_key_check").fetchall() == []
        assert new.execute("SELECT value FROM id_allocator WHERE name='global'").fetchone() == (HIGH+6,)
        assert new.execute("SELECT schedule_policy,owner_id FROM scheduled_tasks").fetchone() == ("hold", None)
        assert new.execute("SELECT approval_status FROM users").fetchone() == ("pending",)
        assert new.execute("SELECT parse_status,embedding_status FROM knowledge_documents").fetchone() == ("legacy","unverified")


@pytest.mark.parametrize("change,reason", [
    ("ALTER TABLE users ADD COLUMN surprise TEXT", "schema"),
    ("DROP INDEX ix_users_username", "schema"),
    ("UPDATE users SET tenant_id=42", "外键"),
    ("UPDATE chat_messages SET request_id=NULL", "request_id"),
    ("UPDATE tenants SET id=9223372036854775807; UPDATE users SET tenant_id=9223372036854775807; "
     "UPDATE chat_sessions SET tenant_id=9223372036854775807; UPDATE chat_messages SET tenant_id=9223372036854775807; "
     "UPDATE scheduled_tasks SET tenant_id=9223372036854775807; UPDATE knowledge_bases SET tenant_id=9223372036854775807; "
     "UPDATE knowledge_documents SET tenant_id=9223372036854775807", "64"),
])
def test_failed_copy_never_publishes_destination_or_modifies_source(tmp_path, change, reason):
    source = legacy(tmp_path / "old.db")
    with sqlite3.connect(source) as connection:
        connection.executescript(change)
    before = digest(source)
    with pytest.raises(ValueError, match=reason):
        prepare_copy(source, tmp_path / "new.db")
    assert not (tmp_path / "new.db").exists() and digest(source) == before


def test_existing_target_and_source_self_target_are_rejected(tmp_path):
    source = legacy(tmp_path / "old.db")
    target = tmp_path / "existing.db"
    target.write_bytes(b"P8-new-writes")
    for destination in (source, target):
        with pytest.raises(ValueError, match="独立文件"):
            prepare_copy(source, destination)
    assert target.read_bytes() == b"P8-new-writes"


def add_orphan(path):
    with sqlite3.connect(path) as con:
        con.execute("INSERT INTO knowledge_ingestion_runs VALUES(?,?,?,?,?,?,?,?,?)",
                    (HIGH + 7, HIGH, HIGH + 99, "ready", 3, 2, "original-error", "2026-10-03", None))


def test_explicit_orphan_archive_is_lossless_and_source_is_unchanged(tmp_path):
    source = legacy(tmp_path / "old.db")
    add_orphan(source)
    before = digest(source)
    target = tmp_path / "new.db"
    with sqlite3.connect(source) as old:
        approved = rows_digest(orphan_ingestion_rows(old))["sha256"]
    record = prepare_copy(source, target, orphan_ingestion_sha256=approved)
    assert digest(source) == before
    assert record["orphan_ingestion_archive"]["count"] == 1
    assert record["allocator_high"] == HIGH + 7
    with sqlite3.connect(source) as old, sqlite3.connect(target) as new:
        assert new.execute("PRAGMA foreign_key_check").fetchall() == []
        columns = [r[1] for r in signature(old)["knowledge_ingestion_runs"]["columns"]]
        assert row_digest(new, "legacy_orphan_ingestion_archive", columns) == row_digest(old, "knowledge_ingestion_runs", columns)
        assert new.execute("SELECT count(*) FROM knowledge_ingestion_runs").fetchone() == (0,)
        # 从活动区和归档区重建独立旧版格式，验证可追溯恢复而非只留一个摘要。
        restored = tmp_path / "restored.db"
        with sqlite3.connect(restored) as replay:
            sql = (BACKEND_ROOT / "migrations/legacy_cloud_lite.sql").read_text().split(";")
            for statement in sorted((s.strip() for s in sql if s.strip()),
                                    key=lambda s: not s.startswith("CREATE TABLE")):
                replay.execute(statement)
            for table, spec in signature(old).items():
                names = ",".join(quote(r[1]) for r in spec["columns"])
                rows = new.execute("SELECT " + names + " FROM " + quote(table)
                                   + (" UNION ALL SELECT " + names + " FROM legacy_orphan_ingestion_archive"
                                      if table == "knowledge_ingestion_runs" else "")).fetchall()
                replay.executemany("INSERT INTO " + quote(table) + "(" + names + ") VALUES("
                                   + ",".join("?" for _ in spec["columns"]) + ")", rows)
                assert row_digest(replay, table, [r[1] for r in spec["columns"]]) == row_digest(old, table, [r[1] for r in spec["columns"]])
            assert signature(replay) == signature(old)


@pytest.mark.parametrize("case", ["no_approval", "wrong_digest", "other_fk", "no_orphan"])
def test_orphan_archive_fails_closed(tmp_path, case):
    source = legacy(tmp_path / "old.db")
    if case != "no_orphan":
        add_orphan(source)
    with sqlite3.connect(source) as con:
        approved = rows_digest(orphan_ingestion_rows(con))["sha256"]
        if case == "other_fk":
            con.execute("UPDATE users SET tenant_id=42")
    before = digest(source)
    with pytest.raises(ValueError):
        prepare_copy(source, tmp_path / "new.db",
                     orphan_ingestion_sha256=None if case == "no_approval" else "0" * 64 if case == "wrong_digest" else approved)
    assert digest(source) == before and not (tmp_path / "new.db").exists()


def test_publication_race_does_not_overwrite_new_file(tmp_path, monkeypatch):
    source = legacy(tmp_path / "old.db")
    target = tmp_path / "new.db"
    original = os.link
    def race(old, new):
        Path(new).write_bytes(b"P8-concurrent-writes")
        return original(old, new)
    monkeypatch.setattr(os, "link", race)
    with pytest.raises(FileExistsError):
        prepare_copy(source, target)
    assert target.read_bytes() == b"P8-concurrent-writes"


def test_upgrade_rejects_unversioned_source_before_any_ddl(tmp_path):
    source = legacy(tmp_path / "old.db")
    before = digest(source)
    env = {**os.environ, "EIC_DATABASE_URL": "sqlite+aiosqlite:///" + source.as_posix()}
    result = subprocess.run([sys.executable, "-B", "-m", "eic.core.schema", "upgrade"],
                            env=env, capture_output=True, text=True)
    assert result.returncode != 0 and "未版本化旧库禁止原地升级" in result.stderr
    assert digest(source) == before


def test_copied_legacy_generating_is_recovered_without_provider_replay(tmp_path, monkeypatch):
    import asyncio
    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
    from eic.core import generation
    source = legacy(tmp_path / "old.db")
    target = tmp_path / "new.db"
    prepare_copy(source, target)
    async def run():
        engine = create_async_engine("sqlite+aiosqlite:///" + target.as_posix())
        monkeypatch.setattr(generation, "SessionLocal", async_sessionmaker(engine, expire_on_commit=False))
        try:
            await generation.recover_expired()
        finally:
            await engine.dispose()
    asyncio.run(run())
    with sqlite3.connect(target) as connection:
        assert connection.execute("SELECT status FROM chat_messages").fetchone()[0] != "generating"


def test_schema_fixture_exactly_matches_reviewed_28_tables(tmp_path):
    path = legacy(tmp_path / "old.db")
    with sqlite3.connect(path) as connection:
        assert signature(connection) == expected_signature()
    assert len(expected_signature()) == 28


def test_release_run_uses_supported_compose_options_and_stops_on_migration_failure():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "p8_release", BACKEND_ROOT.parent / "scripts/release_deploy.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    image = {"digest": "sha256:"+"a"*64, "reference": "p8-fixture:verified"}
    script = module.deployment_script(
        "/opt/p8-fixture", "eic-p8-fixture", "docker-compose.cloud-lite.yml",
        "test-release", "/tmp/p8-source", "/tmp/p8-images", "/tmp/p8-env",
        "b"*64, "c"*64, {"images": {"backend": image, "frontend": image}})
    lines = [line for line in script.splitlines() if " run " in line]
    assert len(lines) == 2
    assert all("--no-build" not in line and "--pull never" in line for line in lines)
    assert all("-T --interactive=false" in line for line in lines)
    assert "set -euo pipefail" in script
    assert script.index(" migrate\n") < script.index("schema check\n") < script.index(" up -d")
