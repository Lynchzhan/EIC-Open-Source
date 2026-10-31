"""HTTP 和 SSE 共用身份校验；后台内部身份不签发浏览器凭据。"""
from dataclasses import dataclass
from datetime import UTC, datetime
import re

from fastapi import HTTPException
from sqlalchemy import delete

from eic.core.ids import new_id
from eic.core.security import credential_stamp, decode_access_token, valid_credential_stamp
from eic.models import RevokedSession, Tenant, User


@dataclass(frozen=True)
class Principal:
    user_id: int
    tenant_id: int
    username: str
    is_super_admin: bool
    auth_stamp: str = ""
    login_session_id: str = ""
    session_expires_at: int | None = None


async def session_active(session, principal):
    if not principal.login_session_id:
        return True
    if principal.session_expires_at is None or principal.session_expires_at <= datetime.now(UTC).timestamp():
        return False
    return await session.get(RevokedSession, principal.login_session_id) is None


async def resolve_identity(payload, session):
    user_id, tenant_id = int(payload["sub"]), int(payload["tenant_id"])
    user = await session.get(User, user_id)
    tenant = await session.get(Tenant, tenant_id)
    sid = payload.get("sid", "")
    deadline, expires = payload.get("session_exp"), payload.get("exp")
    if (user is None or not user.is_active or user.tenant_id != tenant_id
            or (user.approval_status or "approved") != "approved" or tenant is None or not tenant.active
            or not valid_credential_stamp(payload, user.password_hash)
            or not isinstance(sid, str) or re.fullmatch(r"[0-9a-f]{32}", sid) is None
            or type(deadline) is not int or type(expires) is not int or deadline < expires
            or deadline <= datetime.now(UTC).timestamp()
            or await session.get(RevokedSession, sid) is not None):
        raise HTTPException(401, detail={"code": 401, "errMsg": "登录已失效，请重新登录"})
    return Principal(user.id, user.tenant_id, user.username, user.is_super_admin,
                     credential_stamp(user.password_hash), sid, deadline)


async def revoke_browser_session(session, raw_tokens):
    from jwt import PyJWTError
    payloads = []
    for raw in raw_tokens:
        if not raw:
            continue
        try:
            payload = decode_access_token(raw)
            if (re.fullmatch(r"[0-9a-f]{32}", str(payload.get("sid", "")))
                    and type(payload.get("session_exp")) is int):
                payloads.append(payload)
        except (PyJWTError, ValueError, TypeError):
            continue
    if not payloads:
        return
    await new_id(session)
    now = datetime.now(UTC)
    await session.execute(delete(RevokedSession).where(RevokedSession.expires_at < now))
    for payload in payloads:
        if await session.get(RevokedSession, payload["sid"]) is None:
            session.add(RevokedSession(session_id=payload["sid"], tenant_id=int(payload["tenant_id"]),
                user_id=int(payload["sub"]), expires_at=datetime.fromtimestamp(payload["session_exp"], UTC)))
            await session.flush()
    await session.commit()
