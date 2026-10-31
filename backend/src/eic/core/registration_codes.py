"""注册码固定 24 小时，一次性消费与账号创建处于同一事务。"""
from datetime import UTC, datetime, timedelta
import hashlib
import re
import secrets
import uuid

from fastapi import HTTPException
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from eic.models import RegistrationCode, User


def utcnow():
    return datetime.now(UTC)


def aware(value):
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


async def require_admin(principal, session: AsyncSession):
    user = await session.get(User, principal.user_id)
    if (user is None or user.username != "admin" or not user.is_super_admin
            or not user.is_active or user.approval_status != "approved"
            or user.tenant_id != principal.tenant_id):
        raise HTTPException(403, detail={"code": 403, "errMsg": "仅 admin 管理员可以管理注册码"})


def public_record(code, now=None):
    now = now or utcnow()
    state = ("used" if code.used_at else "revoked" if code.revoked_at else
             "expired" if aware(code.expires_at) <= now else "available")
    return {"id": code.id, "createdAt": aware(code.created_at).isoformat(),
            "expiresAt": aware(code.expires_at).isoformat(), "status": state,
            "usedAt": aware(code.used_at).isoformat() if code.used_at else None,
            "revokedAt": aware(code.revoked_at).isoformat() if code.revoked_at else None}


async def create(principal, session):
    await require_admin(principal, session)
    raw = secrets.token_urlsafe(32)
    now = utcnow()
    item = RegistrationCode(
        id=uuid.uuid4().hex, tenant_id=principal.tenant_id,
        code_hash=hashlib.sha256(raw.encode("ascii")).hexdigest(),
        created_by=principal.user_id, created_at=now, expires_at=now + timedelta(hours=24),
    )
    session.add(item)
    await session.commit()
    return {**public_record(item, now), "registrationCode": raw}


async def consume(raw, tenant_id, user_id, session):
    raw = raw.strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{43}", raw):
        raise HTTPException(400, detail={"code": 400, "errMsg": "请填写有效注册码"})
    now = utcnow()
    result = await session.execute(update(RegistrationCode).where(
        RegistrationCode.code_hash == hashlib.sha256(raw.encode("ascii")).hexdigest(),
        RegistrationCode.tenant_id == tenant_id,
        RegistrationCode.expires_at > now,
        RegistrationCode.used_at.is_(None),
        RegistrationCode.revoked_at.is_(None),
    ).values(used_at=now, used_by=user_id).execution_options(synchronize_session=False))
    if result.rowcount != 1:
        raise HTTPException(400, detail={"code": 400, "errMsg": "注册码无效、已过期或已使用"})


async def listing(principal, session, limit):
    await require_admin(principal, session)
    items = await session.scalars(select(RegistrationCode).where(
        RegistrationCode.tenant_id == principal.tenant_id
    ).order_by(RegistrationCode.created_at.desc()).limit(limit))
    now = utcnow()
    return [public_record(item, now) for item in items]


async def revoke(code_id, principal, session):
    await require_admin(principal, session)
    result = await session.execute(update(RegistrationCode).where(
        RegistrationCode.id == code_id, RegistrationCode.tenant_id == principal.tenant_id,
        RegistrationCode.used_at.is_(None), RegistrationCode.revoked_at.is_(None),
        RegistrationCode.expires_at > utcnow(),
    ).values(revoked_at=utcnow()).execution_options(synchronize_session=False))
    if result.rowcount != 1:
        raise HTTPException(409, detail={"code": 409, "errMsg": "注册码已失效或不存在"})
    await session.commit()
    return True
