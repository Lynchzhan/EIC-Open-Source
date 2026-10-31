"""独立 RBAC 与执行授权；只在显式只读作用域内复用同一事务的读取。"""
from contextlib import contextmanager

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from eic.core.authentication import Principal, session_active
from eic.core.runtime_refs import RuntimeReferenceUnavailable
from eic.core.security import credential_stamp
from eic.models import Permission, Role, RolePermission, Tenant, User, UserRole

_SCOPE = "eic.read_authorization_scope"


@contextmanager
def authorization_read_scope(session):
    """仅供无业务写入的单次完整 guard；离开作用域或切换事务即失效。"""
    previous = session.info.get(_SCOPE)
    session.info[_SCOPE] = {}
    try:
        yield
    finally:
        if previous is None:
            session.info.pop(_SCOPE, None)
        else:
            session.info[_SCOPE] = previous


def _cached(session, key):
    cache = session.info.get(_SCOPE)
    if cache is None or session.new or session.dirty or session.deleted:
        return None
    transaction, value = cache.get(key, (None, None))
    return value if transaction is session.sync_session.get_transaction() else None


def _remember(session, key, value):
    cache = session.info.get(_SCOPE)
    if cache is not None and not (session.new or session.dirty or session.deleted):
        cache[key] = (session.sync_session.get_transaction(), value)


async def load_subject(session, principal):
    key = ("subject", principal.tenant_id, principal.user_id)
    cached = _cached(session, key)
    if cached is not None:
        return cached
    user = await session.get(User, principal.user_id, populate_existing=True)
    tenant = await session.get(Tenant, principal.tenant_id, populate_existing=True)
    _remember(session, key, (user, tenant))
    return user, tenant


def expand_menu_codes(codes: set[str]) -> set[str]:
    extra: set[str] = set()
    for code in codes:
        if not code.startswith("Menu/"):
            continue
        parts = code.split("/")
        for index in range(2, len(parts) + 1):
            extra.add("/".join(parts[:index]))
    return codes | extra


async def permission_codes(principal: Principal, session: AsyncSession) -> set[str]:
    """Load effective permissions from the tenant-scoped RBAC relations."""
    cached = _cached(session, ("permissions", principal.tenant_id, principal.user_id))
    if cached is not None:
        return set(cached)
    user, tenant = await load_subject(session, principal)
    if (user is None or user.tenant_id != principal.tenant_id or not user.is_active
            or (user.approval_status or "approved") != "approved" or tenant is None or not tenant.active):
        return set()
    if user.is_super_admin:
        _remember(session, ("permissions", principal.tenant_id, principal.user_id), frozenset({"*"}))
        return {"*"}
    result = await session.scalars(
        select(Permission.code)
        .join(RolePermission, RolePermission.permission_id == Permission.id)
        .join(Role, Role.id == RolePermission.role_id)
        .join(UserRole, UserRole.role_id == Role.id)
        .where(UserRole.user_id == principal.user_id, Role.tenant_id == principal.tenant_id)
    )
    effective = expand_menu_codes(set(result))
    _remember(session, ("permissions", principal.tenant_id, principal.user_id), frozenset(effective))
    return effective


async def has_permission(code: str, principal: Principal, session: AsyncSession) -> bool:
    effective = await permission_codes(principal, session)
    return code in effective or "*" in effective


async def ensure_permission(code: str, principal: Principal, session: AsyncSession) -> None:
    if not await has_permission(code, principal, session):
        raise HTTPException(status_code=403, detail={"code": 403, "errMsg": "没有访问权限", "permission": code})


async def require_runtime_chat_permission(session: AsyncSession, principal: Principal) -> None:
    user, tenant = await load_subject(session, principal)
    if (user is None or user.tenant_id != principal.tenant_id or not user.is_active
        or (user.approval_status or "approved") != "approved" or tenant is None or not tenant.active):
        raise RuntimeReferenceUnavailable("runtime_authorization_unavailable")
    current = Principal(user.id, user.tenant_id, user.username, user.is_super_admin)
    if principal.auth_stamp and principal.auth_stamp != credential_stamp(user.password_hash):
        raise RuntimeReferenceUnavailable("runtime_authorization_unavailable")
    if not await session_active(session, principal):
        raise RuntimeReferenceUnavailable("runtime_authorization_unavailable")
    if not await has_permission("ai.chat.write", current, session):
        raise RuntimeReferenceUnavailable("runtime_authorization_unavailable")
