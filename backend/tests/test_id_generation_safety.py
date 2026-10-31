"""ID、租约、取消和终态提交窗口的独立合成数据库回归。"""
import asyncio
from contextlib import asynccontextmanager
from datetime import timedelta
import importlib.util
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine


@asynccontextmanager
async def fixture_db(monkeypatch, tmp_path, *, is_super_admin=False):
    from eic import main
    from eic.core import generation
    from eic.core.ids import initialize_allocator
    from eic.models import (Base, ChatMessage, ChatSession, Resource, Tenant, User,
                            Permission, Role, RolePermission, UserRole)

    engine = create_async_engine("sqlite+aiosqlite:///" + (tmp_path / "fixture.db").as_posix(),
                                 connect_args={"timeout": 30})
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(generation, "SessionLocal", sessions)
    monkeypatch.setattr(main, "SessionLocal", sessions)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with sessions() as session:
        await initialize_allocator(session)
        session.add(Tenant(id=1, code=1, name="fixture"))
        await session.flush()
        session.add(User(id=11, tenant_id=1, username="fixture", password_hash="unused",
                         is_super_admin=is_super_admin))
        session.add(ChatSession(id=101, tenant_id=1, owner_id=11, title="fixture"))
        session.add(Role(id=599, tenant_id=1, name="fixture-chat-writer"))
        session.add(Permission(id="ai.chat.write", code="ai.chat.write", name="fixture", category="ai"))
        await session.flush()
        session.add(UserRole(user_id=11, role_id=599))
        session.add(RolePermission(role_id=599, permission_id="ai.chat.write"))
        session.add(ChatMessage(id=301, tenant_id=1, session_id=101, role="user",
                                content="fixture", request_id="fixture", status="streaming"))
        session.add(Resource(id=301, tenant_id=1, owner_id=11,
                             resource_type="chat_messages", data={}))
        await session.commit()
    try:
        yield sessions
    finally:
        await engine.dispose()


async def claim(sessions):
    from eic.core import generation
    from eic.models import ChatMessage
    async with sessions() as session:
        token = await generation.claim(session, await session.get(ChatMessage, 301))
        await session.commit()
        return token


def test_revision_fits_declared_alembic_column():
    path = Path(__file__).parents[1] / "migrations/versions/0013_id_allocator_generation_leases.py"
    spec = importlib.util.spec_from_file_location("lease_revision_fixture", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert len(module.revision) <= 32


@pytest.mark.parametrize("reason", ["expired", "wrong-token", "revoked"])
def test_stale_owner_writes_are_fenced(monkeypatch, tmp_path, reason):
    from eic import main
    from eic.core import generation
    from eic.models import ChatEvent, GenerationLease

    async def run():
        async with fixture_db(monkeypatch, tmp_path) as sessions:
            token = await claim(sessions)
            async with sessions() as session:
                values = {"state": "finished"} if reason == "revoked" else {
                    "expires_at": await generation.database_now(session) - timedelta(seconds=1)
                } if reason == "expired" else {}
                if values:
                    await session.execute(update(GenerationLease).values(**values))
                    await session.commit()
            owner = generation.OWNER.set((301, "wrong" if reason == "wrong-token" else token))
            try:
                with pytest.raises(generation.LeaseLost):
                    await main.persist_chat_event("fixture", main.Principal(11, 1, "fixture", True),
                                                  1, "aimsg", "stale")
            finally:
                generation.OWNER.reset(owner)
            async with sessions() as session:
                assert await session.scalar(select(func.count()).select_from(ChatEvent)) == 0
    asyncio.run(run())


def test_recovery_is_idempotent_and_never_retries(monkeypatch, tmp_path):
    from eic.core import generation
    from eic.models import ChatEvent, ChatMessage, GenerationLease

    async def run():
        async with fixture_db(monkeypatch, tmp_path) as sessions:
            await claim(sessions)
            async with sessions() as session:
                await session.execute(update(GenerationLease).values(
                    expires_at=await generation.database_now(session) - timedelta(seconds=1)))
                await session.commit()
            assert sum(await asyncio.gather(*(generation.recover_expired() for _ in range(3)))) == 1
            assert await generation.recover_expired() == 0
            async with sessions() as session:
                message = await session.get(ChatMessage, 301)
                assert message.status == "failed"
                assert message.token_usage["automaticRetry"] is False
                assert message.token_usage["providerOutcome"] == "uncertain"
                assert list(await session.scalars(select(ChatEvent.event_type))) == ["done"]
    asyncio.run(run())


@pytest.mark.parametrize("status", ["completed", "failed", "cancelled"])
def test_crash_between_terminal_commit_and_done_is_repaired(monkeypatch, tmp_path, status):
    from eic.core import generation
    from eic.models import ChatEvent, ChatMessage, GenerationLease

    async def run():
        async with fixture_db(monkeypatch, tmp_path) as sessions:
            await claim(sessions)
            async with sessions() as session:
                await session.execute(update(ChatMessage).values(status=status))
                await session.execute(update(GenerationLease).values(
                    expires_at=await generation.database_now(session) - timedelta(seconds=1)))
                await session.commit()
            await generation.recover_expired()
            await generation.recover_expired()
            async with sessions() as session:
                assert (await session.get(ChatMessage, 301)).status == status
                events = list(await session.scalars(select(ChatEvent)))
                assert len(events) == 1 and events[0].event_type == "done"
    asyncio.run(run())


def test_explicit_cancel_revokes_lease_and_persists_one_done(monkeypatch, tmp_path):
    from eic import main
    from eic.core import generation
    from eic.models import ChatEvent, ChatMessage

    async def run():
        async with fixture_db(monkeypatch, tmp_path) as sessions:
            token = await claim(sessions)
            principal = main.Principal(11, 1, "fixture", True)
            for _ in range(2):
                async with sessions() as session:
                    assert await main.chat_history_cancel("fixture", principal, session)
            owner = generation.OWNER.set((301, token))
            try:
                with pytest.raises(generation.LeaseLost):
                    await main.persist_chat_event("fixture", principal, 2, "aimsg", "late")
            finally:
                generation.OWNER.reset(owner)
            async with sessions() as session:
                assert (await session.get(ChatMessage, 301)).status == "cancelled"
                assert list(await session.scalars(select(ChatEvent.event_type))) == ["done"]
    asyncio.run(run())


def test_cancel_does_not_overwrite_completed_result(monkeypatch, tmp_path):
    from eic import main
    from eic.models import ChatMessage
    async def run():
        async with fixture_db(monkeypatch, tmp_path) as sessions:
            async with sessions() as session:
                await session.execute(update(ChatMessage).values(status="completed"))
                await session.commit()
            async with sessions() as session:
                await main.chat_history_cancel("fixture", main.Principal(11, 1, "fixture", True), session)
            async with sessions() as session:
                assert (await session.get(ChatMessage, 301)).status == "completed"
    asyncio.run(run())


def test_startup_failure_closes_resources(monkeypatch, tmp_path):
    from eic import main
    close, dispose = AsyncMock(), AsyncMock()
    monkeypatch.setattr(main, "engine", SimpleNamespace(dispose=dispose))
    monkeypatch.setattr(main, "close_queue", close)
    monkeypatch.setattr(main, "require_schema", AsyncMock())
    monkeypatch.setattr(main, "UPLOAD_ROOT", tmp_path / "uploads")
    monkeypatch.setattr(main, "settings", main.settings.model_copy(update={
        "auto_create_schema": False, "runtime_schema_updates": False, "bootstrap_enabled": True,
        "seed_demo_data": False, "database_url": "sqlite+aiosqlite:///fixture.db",
        "generation_recovery_enabled": False,
    }))
    monkeypatch.setattr(main, "bootstrap_empty_environment", AsyncMock(side_effect=RuntimeError("startup-fixture")))
    async def run():
        with pytest.raises(RuntimeError, match="startup-fixture"):
            async with main.lifespan(main.app):
                pass
    asyncio.run(run())
    assert close.await_count == dispose.await_count == 1


def test_recovery_defaults_to_enabled(monkeypatch):
    from eic.core.config import Settings
    monkeypatch.delenv("EIC_GENERATION_RECOVERY_ENABLED", raising=False)
    assert Settings(_env_file=None).generation_recovery_enabled is True
    monkeypatch.setenv("EIC_GENERATION_RECOVERY_ENABLED", "false")
    assert Settings(_env_file=None).generation_recovery_enabled is False


def test_unallocated_future_client_id_is_rejected(monkeypatch, tmp_path):
    from eic import main
    from eic.core.ids import INITIAL_ID, new_id
    from eic.models import Resource
    async def run():
        async with fixture_db(monkeypatch, tmp_path) as sessions:
            async with sessions() as session:
                with pytest.raises(main.HTTPException) as error:
                    await main.save_resource("chats", {"id": INITIAL_ID + 100, "name": "unallocated"},
                                             main.Principal(11, 1, "fixture", True), session)
                assert error.value.status_code == 400
                await session.rollback()
                assert await session.get(Resource, INITIAL_ID + 100) is None
                assert await new_id(session) < INITIAL_ID + 100
    asyncio.run(run())


def test_legacy_and_preallocated_client_ids_remain_compatible(monkeypatch, tmp_path):
    from eic import main
    from eic.core.ids import new_id
    from eic.models import Resource
    async def run():
        async with fixture_db(monkeypatch, tmp_path) as sessions:
            async with sessions() as session:
                preallocated = await new_id(session)
                await session.commit()
            for item_id in (12345, preallocated):
                async with sessions() as session:
                    assert await main.save_resource("chats", {"id": item_id, "name": "valid"},
                                                    main.Principal(11, 1, "fixture", True), session)
                async with sessions() as session:
                    assert await session.get(Resource, item_id) is not None
    asyncio.run(run())


def test_allocator_missing_never_falls_back_to_time(monkeypatch, tmp_path):
    from sqlalchemy import delete
    from eic.core.ids import new_id
    from eic.models import IdAllocator
    async def run():
        async with fixture_db(monkeypatch, tmp_path) as sessions:
            async with sessions() as session:
                await session.execute(delete(IdAllocator))
                await session.commit()
                with pytest.raises(RuntimeError, match="explicit migration required"):
                    await new_id(session)
    asyncio.run(run())
