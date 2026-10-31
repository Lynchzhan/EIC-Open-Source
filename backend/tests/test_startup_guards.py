"""启动守卫正反对照；在新进程中执行，不使用部署数据和真实队列。"""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock
import os
from pathlib import Path
import subprocess
import sys

import pytest


def startup_settings(**changes):
    from eic.core.config import get_settings
    return get_settings().model_copy(update=changes)


@pytest.mark.parametrize("schema,patches,bootstrap,url,expected", [
    (False, False, False, "sqlite+aiosqlite:///fixture.db", ["check"]),
    (True, False, False, "sqlite+aiosqlite:///fixture.db", []),
    (False, True, False, "sqlite+aiosqlite:///fixture.db", []),
    (True, True, True, "sqlite+aiosqlite:///fixture.db", []),
    (False, False, True, "postgresql+asyncpg://fixture/fixture", ["check", "bootstrap"]),
    (False, False, False, "mysql+aiomysql://fixture/fixture", ["check"]),
])
def test_lifespan_independent_startup_guards(monkeypatch, tmp_path, schema, patches, bootstrap, url, expected):
    import eic.main as main

    calls = []

    monkeypatch.setattr(main, "settings", startup_settings(
        auto_create_schema=schema, runtime_schema_updates=patches,
        bootstrap_enabled=bootstrap, database_url=url, seed_demo_data=False,
    ))
    cleanup = AsyncMock()
    monkeypatch.setattr(main, "engine", SimpleNamespace(
        begin=lambda: pytest.fail("Startup opened a DDL transaction"), dispose=cleanup))
    monkeypatch.setattr(main, "require_schema", AsyncMock(side_effect=lambda _: calls.append("check")))
    monkeypatch.setattr(main, "UPLOAD_ROOT", tmp_path / "uploads")
    monkeypatch.setattr(main, "bootstrap_empty_environment", AsyncMock(side_effect=lambda: calls.append("bootstrap")))
    close = AsyncMock()
    monkeypatch.setattr(main, "close_queue", close)

    async def run():
        if schema or patches:
            with pytest.raises(RuntimeError, match="Startup DDL switches are retired"):
                async with main.lifespan(main.app):
                    pytest.fail("Retired DDL configuration was accepted")
        else:
            async with main.lifespan(main.app):
                assert calls == expected

    asyncio.run(run())
    assert calls == expected and cleanup.await_count == close.await_count == 1


def test_disabled_direct_bootstrap_does_not_acquire_a_session(monkeypatch):
    import eic.main as main

    monkeypatch.setattr(main.settings, "bootstrap_enabled", False)
    monkeypatch.setattr(main, "bootstrap_session", lambda: pytest.fail("Disabled bootstrap opened DB"))
    asyncio.run(main.bootstrap_empty_environment())


def test_bootstrap_username_false_is_not_a_boolean_switch(monkeypatch):
    from eic.core.config import Settings

    monkeypatch.setenv("EIC_BOOTSTRAP_ADMIN", "false")
    monkeypatch.setenv("EIC_BOOTSTRAP_ENABLED", "true")
    monkeypatch.setenv("EIC_RUNTIME_SCHEMA_UPDATES", "false")
    config = Settings(_env_file=None)
    assert config.bootstrap_admin == "false" and config.bootstrap_enabled is True
    assert config.runtime_schema_updates is False
    monkeypatch.setenv("EIC_BOOTSTRAP_ENABLED", "false")
    assert Settings(_env_file=None).bootstrap_enabled is False


def test_development_compatibility_defaults(monkeypatch):
    from eic.core.config import Settings

    monkeypatch.delenv("EIC_BOOTSTRAP_ENABLED", raising=False)
    monkeypatch.delenv("EIC_RUNTIME_SCHEMA_UPDATES", raising=False)
    monkeypatch.delenv("EIC_AUTO_CREATE_SCHEMA", raising=False)
    config = Settings(_env_file=None)
    assert config.bootstrap_enabled is True and config.runtime_schema_updates is False
    assert config.auto_create_schema is False


def test_lifespan_body_exception_still_closes_resources(monkeypatch, tmp_path):
    import eic.main as main

    monkeypatch.setattr(main, "settings", startup_settings(
        auto_create_schema=False, runtime_schema_updates=False, bootstrap_enabled=False,
        database_url="sqlite+aiosqlite:///fixture.db", seed_demo_data=False,
    ))
    cleanup, close = AsyncMock(), AsyncMock()
    monkeypatch.setattr(main, "engine", SimpleNamespace(
        begin=lambda: pytest.fail("Closed startup opened a DB transaction"), dispose=cleanup,
    ))
    monkeypatch.setattr(main, "close_queue", close)
    monkeypatch.setattr(main, "require_schema", AsyncMock())
    monkeypatch.setattr(main, "UPLOAD_ROOT", tmp_path / "uploads")

    async def run():
        with pytest.raises(RuntimeError, match="fixture-body"):
            async with main.lifespan(main.app):
                raise RuntimeError("fixture-body")

    asyncio.run(run())
    assert cleanup.await_count == close.await_count == 1


@pytest.mark.parametrize("enabled", [False, True])
def test_real_empty_sqlite_bootstrap_toggle(monkeypatch, tmp_path, enabled):
    import eic.main as main
    from sqlalchemy import event, func, select
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    database = tmp_path / "startup.db"
    url = "sqlite+aiosqlite:///" + database.as_posix()
    result = subprocess.run(
        [sys.executable, "-B", "-m", "eic.core.schema", "upgrade"],
        cwd=Path(__file__).parents[1], env={**os.environ, "EIC_DATABASE_URL": url},
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    engine = create_async_engine(url)
    statements = []
    event.listen(engine.sync_engine, "before_cursor_execute",
                 lambda conn, cursor, statement, parameters, context, many: statements.append(statement))
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(main, "engine", engine)
    monkeypatch.setattr(main, "SessionLocal", sessions)
    monkeypatch.setattr(main, "UPLOAD_ROOT", tmp_path / "uploads")
    monkeypatch.setattr(main, "close_queue", AsyncMock())
    monkeypatch.setattr(main, "settings", startup_settings(
        database_url=url, auto_create_schema=False, runtime_schema_updates=False,
        bootstrap_enabled=enabled, bootstrap_admin="fixture-admin",
        bootstrap_admin_password="Synthetic-Startup-Only-Password-2026", seed_demo_data=False,
    ))

    async def run():
        statements.clear()
        async with main.lifespan(main.app):
            pass
        startup = list(statements)
        async with sessions() as session:
            users = await session.scalar(select(func.count()).select_from(main.User))
            roles = await session.scalar(select(func.count()).select_from(main.Role))
        await engine.dispose()
        assert users == int(enabled)
        assert roles == (2 if enabled else 0)
        if enabled:
            assert any(statement.lstrip().upper().startswith("INSERT") for statement in startup)
            assert not any(statement.lstrip().upper().startswith(("ALTER", "CREATE", "DROP")) for statement in startup)
        else:
            assert startup and not any(
                statement.lstrip().upper().startswith(("ALTER", "CREATE", "DROP", "INSERT", "UPDATE", "DELETE"))
                for statement in startup
            ), "Disabled bootstrap must only execute schema checks"

    asyncio.run(run())


@pytest.mark.parametrize("weak", ["short", "a" * 40, "replace-before-production-with-a-random-secret"])
def test_production_security_rejects_weak_values(weak):
    settings = startup_settings(environment="production", jwt_secret=weak,
        secret_encryption_key="Synthetic-Encryption-For-Unit-Only-93271")
    with pytest.raises(ValueError, match="足够强度"):
        settings.validate_production_security()


def test_production_security_requires_distinct_keys_and_accepts_separate_values():
    signing = "Synthetic-Signing-For-Unit-Only-63541"
    settings = startup_settings(environment="production", jwt_secret=signing, secret_encryption_key=signing)
    with pytest.raises(ValueError, match="分离"):
        settings.validate_production_security()
    settings.secret_encryption_key = "Synthetic-Encryption-For-Unit-Only-93271"
    settings.validate_production_security()
