"""Versioned expansion and fail-closed startup, synthetic databases only."""
import asyncio
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
import pytest
import sqlalchemy as sa

BACKEND = Path(__file__).parents[1]
HEAD = ScriptDirectory(str(BACKEND / "migrations")).get_current_head()


def upgrade(path, revision="head", check=True):
    env = {**os.environ, "EIC_DATABASE_URL": "sqlite+aiosqlite:///" + path.as_posix()}
    command = [sys.executable, "-B", "-m", "alembic", "upgrade", revision]
    result = subprocess.run(command, cwd=BACKEND, env=env, capture_output=True, text=True)
    if check:
        assert result.returncode == 0, result.stderr
    return result


def revision_module():
    path = BACKEND / "migrations/versions/0015_runtime_schema_expand.py"
    spec = importlib.util.spec_from_file_location("schema_expand_fixture", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def seed_old(path):
    upgrade(path, "0014_transactional_outbox")
    with sqlite3.connect(path) as connection:
        connection.execute("INSERT INTO tenants VALUES(9007199254740999,77,'synthetic',1,CURRENT_TIMESTAMP)")
        connection.execute("""INSERT INTO users(id,tenant_id,username,nickname,password_hash,is_active,is_super_admin,
            created_at,phone,email) VALUES(9007199254741000,9007199254740999,'synthetic','fixture','not-a-login',1,0,
            CURRENT_TIMESTAMP,'','')""")
        connection.execute("""INSERT INTO scheduled_tasks(id,tenant_id,name,cron,payload,enabled,task_type,
            max_retries,status) VALUES(9007199254741001,9007199254740999,'synthetic','0 * * * *','{}',0,'cron',3,'ready')""")
    return snapshot(path)


def snapshot(path):
    with sqlite3.connect(path) as connection:
        return {
            "tenant": connection.execute("SELECT * FROM tenants").fetchall(),
            "user": connection.execute("SELECT id,tenant_id,username,password_hash,is_active,is_super_admin FROM users").fetchall(),
            "task": connection.execute("SELECT id,tenant_id,name,cron,payload,enabled,status FROM scheduled_tasks").fetchall(),
        }


def test_empty_head_supports_every_orm_mapper_and_versioned_reentry(tmp_path):
    from eic.models import Base
    from eic.core.schema import assert_schema_ready
    path = tmp_path / "empty.db"
    upgrade(path)
    upgrade(path)
    engine = sa.create_engine("sqlite:///" + path.as_posix())
    with engine.connect() as connection:
        assert_schema_ready(connection)
        assert sa.inspect(connection).get_columns("alembic_version")[0]["type"].length == 128
        assert connection.scalar(sa.text("SELECT version_num FROM alembic_version")) == HEAD
        for mapper in Base.registry.mappers:
            connection.execute(sa.select(mapper.class_).limit(1)).all()
        assert connection.scalar(sa.text("SELECT COUNT(*) FROM id_allocator")) == 1
    engine.dispose()


def test_legacy_ids_rows_and_unowned_tasks_are_preserved(tmp_path):
    path = tmp_path / "legacy.db"
    before = seed_old(path)
    upgrade(path)
    upgrade(path)
    assert snapshot(path) == before
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT approval_status FROM users").fetchall() == [("approved",)]
        assert connection.execute("SELECT owner_id FROM scheduled_tasks").fetchall() == [(None,)]


@pytest.mark.parametrize("indexed", [False, True])
def test_runtime_patched_columns_preserve_pending_null_and_owner(indexed, tmp_path):
    path = tmp_path / "patched.db"
    before = seed_old(path)
    with sqlite3.connect(path) as connection:
        connection.execute("ALTER TABLE users ADD COLUMN approval_status VARCHAR(20) DEFAULT 'approved'")
        connection.execute("ALTER TABLE scheduled_tasks ADD COLUMN owner_id BIGINT NULL")
        connection.execute("UPDATE users SET approval_status='pending'")
        connection.execute("UPDATE scheduled_tasks SET owner_id=9007199254741000")
        if indexed:
            connection.execute("CREATE INDEX ix_scheduled_tasks_owner_id ON scheduled_tasks(owner_id)")
    upgrade(path)
    assert snapshot(path) == before
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT approval_status FROM users").fetchone() == ("pending",)
        assert connection.execute("SELECT owner_id FROM scheduled_tasks").fetchone() == (9007199254741000,)
        connection.execute("UPDATE users SET approval_status=NULL")
    upgrade(path)
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT approval_status FROM users").fetchone() == (None,)


@pytest.mark.parametrize("failure", ["second-column", "index"])
def test_partial_ddl_failure_resumes_without_stamp_or_row_rewrite(monkeypatch, tmp_path, failure):
    path = tmp_path / "partial.db"
    before = seed_old(path)
    engine = sa.create_engine("sqlite:///" + path.as_posix())
    module = revision_module()
    with engine.connect() as connection:
        operations = Operations(MigrationContext.configure(connection))
        monkeypatch.setattr(module, "op", operations)
        if failure == "index":
            monkeypatch.setattr(operations, "create_index", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("synthetic-ddl-failure")))
        else:
            original = operations.add_column
            def add(table, column, **kwargs):
                if table == "scheduled_tasks":
                    raise RuntimeError("synthetic-ddl-failure")
                return original(table, column, **kwargs)
            monkeypatch.setattr(operations, "add_column", add)
        with pytest.raises(RuntimeError, match="synthetic-ddl-failure"):
            module.upgrade()
        connection.commit()
        assert connection.scalar(sa.text("SELECT version_num FROM alembic_version")) == "0014_transactional_outbox"
        assert "approval_status" in {column["name"] for column in sa.inspect(connection).get_columns("users")}
    engine.dispose()
    upgrade(path)
    upgrade(path)
    assert snapshot(path) == before


@pytest.mark.parametrize("bad", ["approval-type", "owner-type", "owner-nullability", "wrong-index"])
def test_incompatible_prepatched_schema_blocks_upgrade_without_claiming_head(tmp_path, bad):
    path = tmp_path / "bad.db"
    before = seed_old(path)
    with sqlite3.connect(path) as connection:
        if bad == "approval-type":
            connection.execute("ALTER TABLE users ADD COLUMN approval_status INTEGER")
        elif bad == "owner-type":
            connection.execute("ALTER TABLE scheduled_tasks ADD COLUMN owner_id VARCHAR(100)")
        elif bad == "owner-nullability":
            connection.execute("ALTER TABLE scheduled_tasks ADD COLUMN owner_id BIGINT NOT NULL DEFAULT 0")
        else:
            connection.execute("CREATE INDEX ix_scheduled_tasks_owner_id ON scheduled_tasks(tenant_id)")
    result = upgrade(path, check=False)
    assert result.returncode != 0 and "Incompatible" in result.stderr
    assert snapshot(path) == before
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == ("0014_transactional_outbox",)


@pytest.mark.parametrize("kind", ["empty", "old", "forged-head"])
def test_read_only_check_blocks_unmigrated_or_falsely_stamped_schema(tmp_path, kind):
    from eic.core.schema import assert_schema_ready
    path = tmp_path / "blocked.db"
    if kind != "empty":
        seed_old(path)
    if kind == "forged-head":
        with sqlite3.connect(path) as connection:
            connection.execute("UPDATE alembic_version SET version_num=?", (HEAD,))
    engine = sa.create_engine("sqlite:///" + path.as_posix())
    statements = []
    sa.event.listen(engine, "before_cursor_execute", lambda c, u, sql, p, ctx, many: statements.append(sql))
    with engine.connect() as connection:
        with pytest.raises(RuntimeError, match="revision|missing columns|missing table"):
            assert_schema_ready(connection)
    engine.dispose()
    assert not any(sql.lstrip().upper().startswith(("CREATE", "ALTER", "DROP", "INSERT", "UPDATE", "DELETE")) for sql in statements)


def test_cli_upgrade_from_an_unrelated_cwd_and_check(tmp_path):
    path = tmp_path / "cli.db"
    env = {**os.environ, "EIC_DATABASE_URL": "sqlite+aiosqlite:///" + path.as_posix(),
           "PYTHONPATH": str(BACKEND / "src")}
    for action in ["upgrade", "check", "upgrade"]:
        result = subprocess.run([sys.executable, "-B", "-m", "eic.core.schema", action],
                                cwd=tmp_path, env=env, capture_output=True, text=True)
        assert result.returncode == 0 and "SCHEMA_READY action=" + action in result.stdout, result.stderr


def test_worker_schema_failure_blocks_before_jobs_and_closes_engine(monkeypatch):
    import eic.workers as workers
    monkeypatch.setattr(workers, "get_settings", lambda: SimpleNamespace(auto_create_schema=False, runtime_schema_updates=False))
    cleanup = AsyncMock()
    monkeypatch.setattr(workers, "engine", SimpleNamespace(dispose=cleanup))
    monkeypatch.setattr(workers, "require_schema", AsyncMock(side_effect=RuntimeError("schema-not-ready")))
    with pytest.raises(RuntimeError, match="schema-not-ready"):
        asyncio.run(workers.WorkerSettings.on_startup({}))
    assert cleanup.await_count == 1


def test_three_actual_lifespans_only_read_schema(tmp_path):
    path = tmp_path / "multiprocess.db"
    upgrade(path)
    program = r'''
import asyncio,json
from sqlalchemy import event
from eic import main
statements=[]
event.listen(main.engine.sync_engine,"before_cursor_execute",lambda c,u,s,p,ctx,many:statements.append(s))
async def run():
 async with main.lifespan(main.app): pass
asyncio.run(run())
assert statements and not any(s.lstrip().upper().startswith(("CREATE","ALTER","DROP","INSERT","UPDATE","DELETE")) for s in statements)
print(json.dumps({"sql_count":len(statements),"ddl":0,"writes":0}))
'''
    env = {**os.environ, "EIC_DATABASE_URL": "sqlite+aiosqlite:///" + path.as_posix(),
           "EIC_AUTO_CREATE_SCHEMA": "false", "EIC_RUNTIME_SCHEMA_UPDATES": "false",
           "EIC_BOOTSTRAP_ENABLED": "false", "EIC_SEED_DEMO_DATA": "false",
           "EIC_GENERATION_RECOVERY_ENABLED": "false", "EIC_OUTBOX_DISPATCH_ENABLED": "false",
           "EIC_UPLOAD_ROOT": str(tmp_path / "uploads"), "EIC_SKILL_ROOT": str(tmp_path / "skills"),
           "PYTHONPATH": str(BACKEND / "src")}
    processes = [subprocess.Popen([sys.executable, "-B", "-c", program], cwd=BACKEND, env=env,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for _ in range(3)]
    for process in processes:
        out, err = process.communicate(timeout=60)
        assert process.returncode == 0, err
        assert json.loads(out)["ddl"] == 0


def test_deploy_commands_use_one_migration_job_not_per_worker_ddl():
    import yaml
    for name in ["docker-compose.yml", "docker-compose.prod.yml"]:
        services = yaml.safe_load((BACKEND.parent / "deploy" / name).read_text())["services"]
        assert services["migrate"]["command"] == ["python", "-m", "eic.core.schema", "upgrade"]
        for service in ["backend", "worker"]:
            assert services[service]["depends_on"]["migrate"]["condition"] == "service_completed_successfully"
            assert services[service]["environment"]["EIC_AUTO_CREATE_SCHEMA"] == "false"
            assert "alembic" not in " ".join(services[service]["command"])


def test_expand_revision_is_forward_only():
    with pytest.raises(RuntimeError, match="Forward-only"):
        revision_module().downgrade()


def test_all_historical_revisions_fit_expanded_version_capacity():
    import ast
    revisions = []
    for path in (BACKEND / "migrations/versions").glob("*.py"):
        for node in ast.parse(path.read_text()).body:
            if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id == "revision" for target in node.targets):
                revisions.append(ast.literal_eval(node.value))
    assert len(revisions) >= 27 and len(set(revisions)) == len(revisions)
    assert all(len(revision) <= 128 for revision in revisions)
    assert len("0010_knowledge_retrieval_and_eval") > 32


def test_incompatible_version_table_is_not_stamped_or_rebuilt(tmp_path):
    path = tmp_path / "bad-version.db"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE alembic_version(version_num INTEGER PRIMARY KEY)")
        connection.execute("INSERT INTO alembic_version VALUES(123)")
    result = upgrade(path, check=False)
    assert result.returncode != 0 and "Incompatible Alembic version table" in result.stderr
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT * FROM alembic_version").fetchall() == [(123,)]


def test_historical_retrieval_partial_ddl_resumes_without_drop_or_stamp(monkeypatch, tmp_path):
    path = tmp_path / "historical-partial.db"
    upgrade(path, "0009_department_description")
    with sqlite3.connect(path) as connection:
        connection.execute("INSERT INTO tenants VALUES(77,77,'synthetic',1,CURRENT_TIMESTAMP)")
    before = snapshot(path)
    spec = importlib.util.spec_from_file_location("historical_partial_fixture", BACKEND / "migrations/versions/0010_knowledge_retrieval_and_eval.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    engine = sa.create_engine("sqlite:///" + path.as_posix())
    with engine.connect() as connection:
        operations = Operations(MigrationContext.configure(connection))
        monkeypatch.setattr(module, "op", operations)
        original = operations.create_table
        def injected(name, *args, **kwargs):
            if name == "knowledge_eval_cases":
                raise RuntimeError("synthetic-historical-create-failure")
            return original(name, *args, **kwargs)
        monkeypatch.setattr(operations, "create_table", injected)
        with pytest.raises(RuntimeError, match="synthetic-historical-create-failure"):
            module.upgrade()
        connection.commit()
        assert sa.inspect(connection).has_table("knowledge_eval_sets")
        assert connection.scalar(sa.text("SELECT version_num FROM alembic_version")) == "0009_department_description"
    engine.dispose()
    upgrade(path)
    upgrade(path)
    assert snapshot(path) == before
