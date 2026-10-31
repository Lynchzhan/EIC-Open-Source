"""Synthetic migration replay only; never connect to deployed databases."""
import importlib.util
from pathlib import Path
import sqlite3
import subprocess
import sys
import os

from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
import pytest
import sqlalchemy as sa


def revision_module():
    path = Path(__file__).parents[1] / "migrations/versions/0014_transactional_outbox.py"
    spec = importlib.util.spec_from_file_location("outbox_migration_fixture", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_additive_migration_preserves_legacy_ids_and_reexecutes(tmp_path):
    path = tmp_path / "legacy.db"
    env = {**os.environ, "EIC_DATABASE_URL": "sqlite+aiosqlite:///" + path.as_posix()}
    def upgrade(revision):
        result = subprocess.run([sys.executable, "-m", "alembic", "upgrade", revision],
                                cwd=Path(__file__).parents[1], env=env, capture_output=True)
        assert result.returncode == 0, result.stderr.decode()
    upgrade("0013_id_generation_leases")
    with sqlite3.connect(path) as connection:
        connection.execute("INSERT INTO tenants(id,code,name,active,created_at) VALUES(9007199254740999,77,'synthetic',1,CURRENT_TIMESTAMP)")
        baseline = connection.execute("SELECT * FROM tenants").fetchall()
    upgrade("head")
    upgrade("head")
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT * FROM tenants").fetchall() == baseline
        head = ScriptDirectory(str(Path(__file__).parents[1] / "migrations")).get_current_head()
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (head,)
        assert connection.execute("SELECT COUNT(*) FROM outbox_events").fetchone() == (0,)
        columns = {row[1] for row in connection.execute("PRAGMA table_info(outbox_events)")}
        assert {"consume_token", "consume_status", "event_key"} <= columns


def test_partial_ddl_failure_reenters_without_dropping_receipts(monkeypatch, tmp_path):
    engine = sa.create_engine("sqlite:///" + (tmp_path / "partial.db").as_posix())
    module = revision_module()
    with engine.connect() as connection:
        connection.exec_driver_sql("CREATE TABLE tenants(id BIGINT PRIMARY KEY)")
        operations = Operations(MigrationContext.configure(connection))
        monkeypatch.setattr(module, "op", operations)
        original = operations.create_index
        def fail(*args, **kwargs):
            raise RuntimeError("synthetic-index-failure")
        monkeypatch.setattr(operations, "create_index", fail)
        with pytest.raises(RuntimeError, match="synthetic-index-failure"):
            module.upgrade()
        connection.commit()
        assert sa.inspect(connection).has_table("outbox_events")
        monkeypatch.setattr(operations, "create_index", original)
        module.upgrade()
        module.upgrade()
        connection.commit()
        assert len(sa.inspect(connection).get_indexes("outbox_events")) == 3
    engine.dispose()


def test_incompatible_existing_table_is_rejected(tmp_path):
    engine = sa.create_engine("sqlite:///" + (tmp_path / "incompatible.db").as_posix())
    module = revision_module()
    with engine.begin() as connection:
        connection.exec_driver_sql("CREATE TABLE outbox_events(id BIGINT PRIMARY KEY)")
        module.op = Operations(MigrationContext.configure(connection))
        with pytest.raises(RuntimeError, match="incompatible"):
            module.upgrade()
    engine.dispose()
