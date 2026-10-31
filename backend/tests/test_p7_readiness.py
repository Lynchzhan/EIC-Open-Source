import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi.testclient import TestClient
import pytest


@pytest.mark.parametrize("failure", ["database", "vector-timeout", "redis-url", "redis-timeout"])
def test_required_failure_is_503_but_liveness_is_ok(monkeypatch, failure):
    from eic import main
    from eic.core.database import get_session
    from sqlalchemy.exc import OperationalError
    session = SimpleNamespace(execute=AsyncMock())
    if failure == "database":
        session.execute.side_effect = OperationalError("fixture", {}, Exception("synthetic"))
    async def override():
        yield session
    monkeypatch.setattr(main.settings, "require_runtime_dependencies", failure != "database")
    monkeypatch.setattr(main.settings, "task_execution_enabled", False)
    connection = SimpleNamespace(fetchval=AsyncMock(return_value="vector"), close=AsyncMock())
    connect = AsyncMock(return_value=connection)
    if failure == "vector-timeout":
        connect.side_effect = TimeoutError("synthetic")
    monkeypatch.setattr(main.asyncpg, "connect", connect)
    redis = SimpleNamespace(ping=AsyncMock(return_value=True), aclose=AsyncMock())
    if failure == "redis-timeout":
        redis.ping.side_effect = TimeoutError("synthetic")
    def factory(*a, **kw):
        if failure == "redis-url":
            raise ValueError("synthetic")
        return redis
    monkeypatch.setattr(main.redis_async, "from_url", factory)
    main.app.dependency_overrides[get_session] = override
    try:
        client = TestClient(main.app, raise_server_exceptions=False)
        assert client.get("/health").status_code == 200
        response = client.get("/api/Health/Ready")
        assert response.status_code == 503, response.text
        assert response.json()["status"] == "not_ready"
    finally:
        main.app.dependency_overrides.pop(get_session, None)


def test_runtime_sqlite_rejects_missing_parent():
    from eic.core.database import engine
    from sqlalchemy.exc import IntegrityError
    from sqlalchemy import text
    async def run():
        async with engine.begin() as connection:
            await connection.execute(text("CREATE TABLE p7_parent (id INTEGER PRIMARY KEY)"))
            await connection.execute(text("CREATE TABLE p7_child (id INTEGER PRIMARY KEY, parent_id INTEGER REFERENCES p7_parent(id))"))
            with pytest.raises(IntegrityError):
                await connection.execute(text("INSERT INTO p7_child VALUES (1,999)"))
            assert await connection.scalar(text("SELECT COUNT(*) FROM p7_child")) == 0
    asyncio.run(run())
