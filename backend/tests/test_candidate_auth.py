"""认证迁移后的合成身份回归，不创建模型或发送外部请求。"""
import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from auth_fixture import signed_token
from test_id_generation_safety import fixture_db
from eic.core.authentication import resolve_identity, revoke_browser_session, session_active
from eic.core.security import create_access_token, decode_access_token
from eic.models import RevokedSession, User
from fastapi import HTTPException


def test_password_change_invalidates_existing_session(monkeypatch, tmp_path):
    async def run():
        async with fixture_db(monkeypatch, tmp_path) as sessions:
            claims = decode_access_token(signed_token(11, 1, "fixture", "unused"))
            async with sessions() as session:
                principal = await resolve_identity(claims, session)
                assert await session_active(session, principal)
                (await session.get(User, 11)).password_hash = "changed-synthetic-password-hash"
                await session.commit()
            async with sessions() as session:
                with pytest.raises(HTTPException) as error:
                    await resolve_identity(claims, session)
                assert error.value.status_code == 401
                updated = decode_access_token(signed_token(11, 1, "fixture", "changed-synthetic-password-hash"))
                assert (await resolve_identity(updated, session)).user_id == 11
    asyncio.run(run())


def test_logout_revokes_only_selected_login_and_preserves_absolute_expiry(monkeypatch, tmp_path):
    async def run():
        async with fixture_db(monkeypatch, tmp_path) as sessions:
            first = signed_token(11, 1, "fixture", "unused")
            other = signed_token(11, 1, "fixture", "unused")
            claims = decode_access_token(first)
            cookie = create_access_token(11, 1, "fixture", auth_stamp=claims["auth"],
                login_session_id=claims["sid"], session_expires_at=claims["session_exp"])
            async with sessions() as session:
                principal = await resolve_identity(claims, session)
                await revoke_browser_session(session, [first, cookie])
                assert not await session_active(session, principal)
                row = await session.get(RevokedSession, claims["sid"])
                assert row.expires_at.replace(tzinfo=UTC).timestamp() == claims["session_exp"]
            async with sessions() as session:
                for token in (first, cookie):
                    with pytest.raises(HTTPException):
                        await resolve_identity(decode_access_token(token), session)
                assert (await resolve_identity(decode_access_token(other), session)).user_id == 11
                await revoke_browser_session(session, [first, first])
    asyncio.run(run())


def test_missing_authentication_fields_and_absolute_expiry_fail_closed(monkeypatch, tmp_path):
    async def run():
        async with fixture_db(monkeypatch, tmp_path) as sessions, sessions() as session:
            old = decode_access_token(create_access_token(11, 1, "fixture"))
            with pytest.raises(HTTPException):
                await resolve_identity(old, session)
            claims = decode_access_token(signed_token(11, 1, "fixture", "unused"))
            claims["session_exp"] = int((datetime.now(UTC) - timedelta(seconds=1)).timestamp())
            with pytest.raises(HTTPException):
                await resolve_identity(claims, session)
    asyncio.run(run())
