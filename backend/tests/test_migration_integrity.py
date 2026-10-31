import asyncio
import hashlib
import importlib.util
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
from types import SimpleNamespace

import pytest
from sqlalchemy import event, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from eic.models import Base, Department, LegacyRecord, Resource, Tenant


def load(name="migrate_mysql"):
    path = Path(__file__).parents[2] / "migration/scripts" / (name + ".py")
    spec = importlib.util.spec_from_file_location("integrity_test_" + name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def rows():
    return {
        "TTenant": [{"Id": 1, "Code": 10, "Name": "tenant"}],
        "TUser": [{"Id": 2, "TenantId": 10, "Name": "user", "PasswordHash": hashlib.sha256(b"test").hexdigest()}],
    }


async def fixture(tmp_path, data=None, *, omit=(), prepare=True):
    migrator = load()
    source = f"sqlite+aiosqlite:///{(tmp_path / 'source.db').as_posix()}"
    target = f"sqlite+aiosqlite:///{(tmp_path / 'target.db').as_posix()}"
    source_engine = create_async_engine(source)
    fields = (
        "Id TEXT, TenantId INTEGER, Code INTEGER, Name TEXT, PasswordHash TEXT, UserId INTEGER, "
        "RoleId INTEGER, PermissionId TEXT, ParentId INTEGER, ChatModelID INTEGER, RerankModelID INTEGER, "
        "AIPromptID INTEGER, KmsId INTEGER, AIChatsId INTEGER, AIAppsId INTEGER, AppId INTEGER, "
        "FileId INTEGER, SecretKey TEXT, ModelKey TEXT, Content TEXT, aIModelsId INTEGER, PositionId INTEGER"
    )
    try:
        async with source_engine.begin() as connection:
            for table in migrator.SOURCE_TABLES:
                if table not in omit:
                    await connection.execute(text("CREATE TABLE `" + table + "` (" + fields + ")"))
            for table, records in (data or rows()).items():
                if table in omit:
                    continue
                for row in records:
                    names = list(row)
                    sql = "INSERT INTO `" + table + "` (" + ",".join(names) + ") VALUES (" + ",".join(":" + n for n in names) + ")"
                    await connection.execute(text(sql), row)
    finally:
        await source_engine.dispose()
    if prepare:
        engine = create_async_engine(target)
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        await engine.dispose()
    return migrator, SimpleNamespace(source_url=source, target_url=target, dry_run=False,
                                     prepare_schema=False, allow_missing_tables=True)


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_complete_inventory_and_schema_aware_audit(tmp_path):
    async def run():
        migrator, args = await fixture(tmp_path)
        report = await migrator.run(args)
        assert report["status"] == "success"
        assert report["integrity"]["complete"]
        assert len(report["tables"]) == 33
        assert report["tables"]["TUser"]["target_verified"] == 1
        audit = await load("audit_mysql_migration").audit(args.source_url, args.target_url)
        assert audit["status"] == "success" and all(audit["match"].values())
    asyncio.run(run())


@pytest.mark.parametrize("case", ["missing", "skipped", "relation", "duplicate"])
def test_incomplete_migration_is_partial_not_success(tmp_path, case):
    async def run():
        data = rows()
        if case == "skipped":
            data["TTenant"].append({"Id": 9, "Name": "invalid"})
        if case == "relation":
            data["TAIApps"] = [{"Id": 3, "TenantId": 10, "Name": "app", "ChatModelID": 999}]
        if case == "duplicate":
            data["TTenant"].append(dict(data["TTenant"][0]))
        migrator, args = await fixture(tmp_path, data, omit=["TPosition"] if case == "missing" else [])
        report = await migrator.run(args)
        if case in {'skipped', 'duplicate'}:
            assert report['status']=='failed' and report['migration_blocked'] is True, report
            assert report['error_code']==('invalid_source_identity' if case=='skipped' else 'ambiguous_source_tenant')
            with sqlite3.connect(tmp_path/'target.db') as connection:
                assert connection.execute('SELECT COUNT(*) FROM tenants').fetchone()[0]==0
        else:
            assert report["status"] == "partial", report
            assert report["source_errors"] or report["integrity"]["issues"]
    asyncio.run(run())


def test_dry_run_uses_read_only_target_and_does_not_write_original_file(tmp_path):
    async def run():
        migrator, args = await fixture(tmp_path)
        before = digest(tmp_path / "target.db")
        source_before = digest(tmp_path / "source.db")
        from sqlalchemy.engine import Engine
        statements = []

        def observe(connection, cursor, sql, params, context, many):
            if connection.engine.url.database and "target.db" in connection.engine.url.database:
                statements.append(sql.split()[0].upper())
        event.listen(Engine, "before_cursor_execute", observe)
        try:
            args.dry_run = True
            report = await migrator.run(args)
        finally:
            event.remove(Engine, "before_cursor_execute", observe)
        assert report["status"] == "success", report
        assert report["dry_run"] and report["transaction"] == "preview_memory_rolled_back"
        assert not {"INSERT", "UPDATE", "DELETE", "CREATE", "ALTER", "DROP"} & set(statements)
        assert digest(tmp_path / "target.db") == before
        assert digest(tmp_path / "source.db") == source_before
        with sqlite3.connect(tmp_path / "target.db") as connection:
            assert connection.execute("SELECT COUNT(*) FROM tenants").fetchone()[0] == 0
    asyncio.run(run())


@pytest.mark.parametrize("prepare", [False, True])
def test_preview_does_not_create_missing_target_or_ddl(tmp_path, prepare):
    async def run():
        migrator, args = await fixture(tmp_path, prepare=False)
        args.dry_run = True
        args.prepare_schema = prepare
        report = await migrator.run(args)
        assert report["status"] == "failed"
        assert not (tmp_path / "target.db").exists()
    asyncio.run(run())


def test_both_unavailable_never_match_and_cli_exit_is_nonzero(tmp_path):
    for name in ["source.db", "target.db"]:
        sqlite3.connect(tmp_path / name).close()
    script = Path(__file__).parents[2] / "migration/scripts/audit_mysql_migration.py"
    output = tmp_path / "audit.json"
    result = subprocess.run([sys.executable, "-B", str(script),
                             "--source-url", f"sqlite+aiosqlite:///{(tmp_path / 'source.db').as_posix()}",
                             "--target-url", f"sqlite+aiosqlite:///{(tmp_path / 'target.db').as_posix()}",
                             "--output", str(output)], capture_output=True, text=True)
    report = json.loads(output.read_text())
    assert result.returncode == 2 and report["status"] == "failed"
    assert not any(report["match"].values())


def test_partial_cli_returns_three_and_existing_target_rerun_is_blocked(tmp_path):
    async def run():
        migrator, args = await fixture(tmp_path, omit=["TPosition"])
        script = Path(migrator.__file__)
        output = tmp_path / "migration.json"
        command = [sys.executable, "-B", str(script), "--source-url", args.source_url,
                   "--target-url", args.target_url, "--allow-missing-tables", "--report", str(output)]
        first = subprocess.run(command, capture_output=True, text=True)
        assert first.returncode == 3 and json.loads(output.read_text())["status"] == "partial"
        engine = create_async_engine(args.source_url)
        async with engine.begin() as connection:
            await connection.execute(text("CREATE TABLE TPosition (Id TEXT)"))
        await engine.dispose()
        before=digest(tmp_path/'target.db')
        second = subprocess.run(command, capture_output=True, text=True)
        assert second.returncode == 2
        assert json.loads(output.read_text())['error_code']=='target_entity_id_collision'
        assert digest(tmp_path/'target.db')==before
        assert (await migrator.run(args))['error_code']=='target_entity_id_collision'
        engine = create_async_engine(args.target_url)
        maker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
        async with maker() as session:
            assert len(list(await session.scalars(select(Tenant)))) == 1
            assert len(list(await session.scalars(select(LegacyRecord)))) == 2
        await engine.dispose()
    asyncio.run(run())


def test_secret_decryption_failure_rolls_back_and_report_is_redacted(tmp_path, monkeypatch):
    async def run():
        data = rows()
        data["TAIApps"] = [{"Id": 3, "TenantId": 10, "Name": "app", "SecretKey": "fixture-sensitive-marker"}]
        migrator, args = await fixture(tmp_path, data)
        monkeypatch.setattr(migrator, "encrypt_secret", lambda _: "invalid-ciphertext")
        report = await migrator.run(args)
        assert report["status"] == "failed" and report["transaction"] == "rolled_back"
        assert "fixture-sensitive-marker" not in json.dumps(report)
        engine = create_async_engine(args.target_url)
        maker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
        async with maker() as session:
            assert list(await session.scalars(select(Tenant))) == []
        await engine.dispose()
    asyncio.run(run())


def test_audit_detects_missing_entity_and_relationship_with_equal_global_counts(tmp_path):
    async def run():
        data = rows()
        data["TRole"] = [{"Id": 3, "TenantId": 10, "Name": "role"}]
        data["TUserBindRole"] = [{"UserId": 2, "RoleId": 3}]
        migrator, args = await fixture(tmp_path, data)
        assert (await migrator.run(args))["status"] == "success"
        engine = create_async_engine(args.target_url)
        async with engine.begin() as connection:
            await connection.execute(text("DELETE FROM user_roles"))
            await connection.execute(text("UPDATE roles SET id=99 WHERE id=3"))
            await connection.execute(text("INSERT INTO user_roles VALUES (2,99)"))
        await engine.dispose()
        report = await load("audit_mysql_migration").audit(args.source_url, args.target_url)
        assert report["status"] == "partial"
        assert not report["match"]["TRole"] and not report["match"]["TUserBindRole"]
    asyncio.run(run())


def test_cross_tenant_role_binding_is_not_migrated(tmp_path):
    async def run():
        data = rows()
        data["TTenant"].append({"Id": 4, "Code": 40, "Name": "other"})
        data["TRole"] = [{"Id": 3, "TenantId": 40, "Name": "foreign-role"}]
        data["TUserBindRole"] = [{"UserId": 2, "RoleId": 3}]
        migrator, args = await fixture(tmp_path, data)
        report = await migrator.run(args)
        assert report["status"] == "partial"
        assert report["tables"]["TUserBindRole"]["target_verified"] == 0
    asyncio.run(run())


def test_department_cycle_is_failure_without_persistent_rows(tmp_path):
    async def run():
        data = rows()
        data["TDepartment"] = [{"Id": 3, "TenantId": 10, "Name": "a", "ParentId": 4},
                              {"Id": 4, "TenantId": 10, "Name": "b", "ParentId": 3}]
        migrator, args = await fixture(tmp_path, data)
        report = await migrator.run(args)
        assert report["status"] == "failed"
        engine = create_async_engine(args.target_url)
        maker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
        async with maker() as session:
            assert list(await session.scalars(select(Department))) == []
        await engine.dispose()
    asyncio.run(run())


def test_archive_composite_position_identity_does_not_drop_second_relation(tmp_path):
    async def run():
        data = rows()
        data["TUserBindPosition"] = [{"UserId": 2, "PositionId": 3}, {"UserId": 2, "PositionId": 4}]
        migrator, args = await fixture(tmp_path, data)
        report = await migrator.run(args)
        assert report["status"] == "success"
        assert report["tables"]["TUserBindPosition"]["mapping"] == "archive_only"
        assert report["tables"]["TUserBindPosition"]["archived"] == 2
    asyncio.run(run())


def test_file_resource_collision_is_blocked_before_document_relation(tmp_path):
    async def run():
        data = rows()
        data.update({
            "TAIPrompts": [{"Id": 3, "TenantId": 10, "Name": "prompt"}],
            "TFile": [{"Id": 3, "TenantId": 10, "Name": "file"}],
            "TAIKmss": [{"Id": 4, "TenantId": 10, "Name": "kb"}],
            "TAIKmsDetails": [{"Id": 5, "TenantId": 10, "KmsId": 4, "FileId": 3}],
        })
        migrator, args = await fixture(tmp_path, data)
        report = await migrator.run(args)
        assert report['status']=='failed' and report['migration_blocked'] is True
        assert report['error_code']=='source_resource_id_collision'
        with sqlite3.connect(tmp_path/'target.db') as connection:
            assert connection.execute('SELECT COUNT(*) FROM resources').fetchone()[0]==0
            assert connection.execute('SELECT COUNT(*) FROM knowledge_documents').fetchone()[0]==0
    asyncio.run(run())


def test_connection_credentials_and_query_values_are_never_reported():
    migrator = load()
    redacted = migrator.safe_url("mysql+asyncmy://secret-user:secret-password@localhost/fixture?token=secret-token")
    assert not any(secret in redacted for secret in ["secret-user", "secret-password", "secret-token"])


def test_same_sqlite_file_with_alias_url_is_rejected_before_ddl(tmp_path):
    async def run():
        migrator, args = await fixture(tmp_path)
        before = digest(tmp_path / "source.db")
        args.target_url = args.source_url + "?timeout=20"
        args.prepare_schema = True
        report = await migrator.run(args)
        assert report["status"] == "failed" and report["error"] == "ValueError"
        assert digest(tmp_path / "source.db") == before
    asyncio.run(run())
