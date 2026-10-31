"""每个场景独立迁移/释放 SQLite、两租户三用户和可记录 Mock provider。"""
import asyncio
import os
from pathlib import Path
import subprocess
import sys

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import event
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine


@pytest.fixture
def p7_world(tmp_path, monkeypatch):
    from eic import main
    from eic.core.database import get_session
    from eic.core.security import hash_password
    from auth_fixture import signed_headers
    from eic.models import Tenant, User, Role, UserRole, Permission, RolePermission

    url = "sqlite+aiosqlite:///" + (tmp_path / "p7.db").as_posix()
    result = subprocess.run(
        [sys.executable, "-B", "-m", "eic.core.schema", "upgrade"],
        cwd=Path(__file__).parents[1], env={**os.environ, "EIC_DATABASE_URL": url},
        capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stderr
    engine = create_async_engine(url)
    @event.listens_for(engine.sync_engine, "connect")
    def foreign_keys(connection, _):
        connection.execute("PRAGMA foreign_keys=ON")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    password_hash = hash_password("Fixture-Only-P7-Password")
    async def seed():
        async with sessions() as session:
            session.add_all([Tenant(id=71, code=7100, name="T1"),
                             Tenant(id=72, code=7200, name="T2")])
            await session.flush()
            session.add_all([
                User(id=711, tenant_id=71, username="A", password_hash=password_hash, is_super_admin=True),
                User(id=712, tenant_id=71, username="B", password_hash=password_hash),
                User(id=721, tenant_id=72, username="C", password_hash=password_hash, is_super_admin=True),
                Role(id=719, tenant_id=71, name="普通用户"),
                Permission(id="ai.chat.read", code="ai.chat.read", name="查看聊天"),
                Permission(id="ai.chat.write", code="ai.chat.write", name="使用聊天"),
            ])
            await session.flush()
            session.add(UserRole(user_id=712, role_id=719))
            session.add_all([RolePermission(role_id=719, permission_id=code)
                            for code in ("ai.chat.read", "ai.chat.write")])
            await session.commit()
    asyncio.run(seed())
    async def session_override():
        async with sessions() as session:
            yield session
    monkeypatch.setattr(main, "engine", engine)
    monkeypatch.setattr(main, "SessionLocal", sessions)
    monkeypatch.setattr(main, "UPLOAD_ROOT", tmp_path / "uploads")
    monkeypatch.setattr(main.settings, "bootstrap_enabled", False)
    monkeypatch.setattr(main.settings, "generation_recovery_enabled", False)
    monkeypatch.setattr(main.settings, "outbox_dispatch_enabled", False)
    calls = []
    async def mock_provider(*args, **kwargs):
        calls.append({"args": args, "kwargs": kwargs})
        yield "Mock-P7"
    monkeypatch.setattr(main, "stream_chat_completion", mock_provider)
    main.app.dependency_overrides[get_session] = session_override
    headers = {
        name: signed_headers(user, tenant, name, password_hash)
        for name, user, tenant in [("A", 711, 71), ("B", 712, 71), ("C", 721, 72)]
    }
    try:
        with TestClient(main.app) as client:
            yield {"client": client, "headers": headers, "sessions": sessions, "calls": calls}
    finally:
        main.app.dependency_overrides.pop(get_session, None)
        asyncio.run(engine.dispose())
