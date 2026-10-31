"""独立合成库覆盖注册码边界与事务，不改变历史 Locked 测试。"""
import asyncio
from datetime import timedelta
import hashlib
from types import SimpleNamespace

from fastapi import HTTPException
import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from eic.core import registration_codes as codes
from eic.models import Base, RegistrationCode, Tenant, User


@pytest.fixture
async def fixture(tmp_path):
    engine = create_async_engine("sqlite+aiosqlite:///" + str(tmp_path / "invite.db"))
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as db:
        db.add(Tenant(id=1, code=1000, name="fixture"))
        db.add(Tenant(id=2, code=2000, name="other"))
        db.add_all([
            User(id=11, tenant_id=1, username="admin", password_hash="fixture",
                 is_active=True, is_super_admin=True, approval_status="approved"),
            User(id=12, tenant_id=1, username="other-admin", password_hash="fixture",
                 is_active=True, is_super_admin=True, approval_status="approved"),
        ])
        await db.commit()
    yield sessions, SimpleNamespace(user_id=11, tenant_id=1, username="admin", is_super_admin=True)
    await engine.dispose()


async def make(fixture):
    sessions, principal = fixture
    async with sessions() as db:
        return await codes.create(principal, db)


async def test_only_admin_generates_and_plaintext_is_not_persisted(fixture):
    sessions, principal = fixture
    created = await make(fixture)
    async with sessions() as db:
        item = await db.get(RegistrationCode, created["id"])
        assert len(created["registrationCode"]) == 43
        assert item.code_hash == hashlib.sha256(created["registrationCode"].encode()).hexdigest()
        assert codes.aware(item.expires_at) - codes.aware(item.created_at) == timedelta(hours=24)
        listed = await codes.listing(principal, db, 50)
        assert "registrationCode" not in listed[0] and "code_hash" not in listed[0]
        for user_id, tenant_id in [(12, 1), (999, 1), (11, 2)]:
            fake = SimpleNamespace(user_id=user_id, tenant_id=tenant_id, username="admin", is_super_admin=True)
            with pytest.raises(HTTPException) as exc:
                await codes.create(fake, db)
            assert exc.value.status_code == 403


@pytest.mark.parametrize("kind", ["invalid", "expired", "boundary", "revoked", "tenant", "used"])
async def test_invalid_states_fail_closed(fixture, kind, monkeypatch):
    sessions, principal = fixture
    created = await make(fixture)
    raw = created["registrationCode"]
    async with sessions() as db:
        item = await db.get(RegistrationCode, created["id"])
        if kind == "expired":
            item.expires_at = codes.utcnow() - timedelta(seconds=1)
        elif kind == "boundary":
            fixed = codes.aware(item.expires_at)
            monkeypatch.setattr(codes, "utcnow", lambda: fixed)
        elif kind == "revoked":
            await codes.revoke(item.id, principal, db)
        elif kind == "used":
            await codes.consume(raw, 1, 100, db)
        await db.commit()
        with pytest.raises(HTTPException) as exc:
            await codes.consume("bad" if kind == "invalid" else raw,
                                2 if kind == "tenant" else 1, 101, db)
        assert exc.value.status_code == 400
        await db.rollback()


async def test_rollback_keeps_code_available(fixture):
    sessions, _ = fixture
    created = await make(fixture)
    async with sessions() as db:
        await codes.consume(created["registrationCode"], 1, 101, db)
        await db.rollback()
    async with sessions() as db:
        await codes.consume(created["registrationCode"], 1, 102, db)
        await db.commit()
        item = await db.get(RegistrationCode, created["id"])
        assert item.used_by == 102


async def test_concurrent_consumption_has_one_winner(fixture):
    sessions, _ = fixture
    created = await make(fixture)
    async def attempt(user):
        async with sessions() as db:
            try:
                await codes.consume(created["registrationCode"], 1, user, db)
                await db.commit()
                return True
            except HTTPException:
                await db.rollback()
                return False
    outcomes = await asyncio.gather(attempt(101), attempt(102))
    assert sorted(outcomes) == [False, True]
    async with sessions() as db:
        assert len(list(await db.scalars(select(RegistrationCode)))) == 1


async def test_revocation_cannot_reactivate_used_code(fixture):
    sessions, principal = fixture
    created = await make(fixture)
    async with sessions() as db:
        await codes.consume(created["registrationCode"], 1, 101, db)
        await db.commit()
        with pytest.raises(HTTPException):
            await codes.revoke(created["id"], principal, db)
        await db.rollback()
        await db.execute(update(User).where(User.id == 11).values(is_active=False))
        await db.commit()
        with pytest.raises(HTTPException):
            await codes.create(principal, db)
