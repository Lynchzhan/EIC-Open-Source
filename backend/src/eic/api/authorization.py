"""authorization 领域路由；不导入应用入口或复制全局命名空间。"""
from types import SimpleNamespace
from typing import Any
from fastapi import Depends, HTTPException, Request, Response
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from eic.core.authentication import revoke_browser_session
from eic.core.database import get_session
from eic.core.ids import new_id
from eic.core import registration_codes
from eic.core.security import create_access_token, credential_stamp, hash_password, verify_and_upgrade_password
from eic.models import Department, Position, Tenant, User
from eic.api.schemas import LoginInput, RegisterInput


def bind_routes(app, *, attach_operation_logs, serialize_department, serialize_position):
    @app.post("/api/Authorize/GetToken")
    async def get_token(payload: LoginInput, request: Request, session: AsyncSession = Depends(get_session)) -> str:
        tenant = await session.scalar(select(Tenant).where(Tenant.code == payload.tenant_id, Tenant.active.is_(True)))
        user = await session.scalar(select(User).where(User.tenant_id == tenant.id, User.username == payload.name)) if tenant else None
        valid_password, upgraded_hash = (False, "") if user is None else verify_and_upgrade_password(payload.password, user.password_hash)
        if user is None or not valid_password:
            raise HTTPException(status_code=400, detail={"code": 400, "errMsg": "用户名、密码或租户不正确"})
        approval_status = str(getattr(user, "approval_status", "approved") or "approved")
        if approval_status == "pending":
            raise HTTPException(status_code=400, detail={"code": 400, "errMsg": "账号待管理员审核"})
        if approval_status == "rejected":
            raise HTTPException(status_code=400, detail={"code": 400, "errMsg": "账号未通过审核"})
        if not user.is_active:
            raise HTTPException(status_code=400, detail={"code": 400, "errMsg": "账号已停用"})
        if upgraded_hash != user.password_hash:
            user.password_hash = upgraded_hash
        await attach_operation_logs(
            session, tenant_id=user.tenant_id, user_id=user.id, username=user.username,
            operate_type="登录", operate_remark="用户登录", table="users", table_id=user.id,
            payload={"name": payload.name, "tenantId": payload.tenant_id}, request=request,
        )
        await session.commit()
        return create_access_token(user.id, user.tenant_id, user.username, auth_stamp=credential_stamp(user.password_hash))


    @app.post("/api/Authorize/Logout")
    async def logout(request: Request, response: Response, session: AsyncSession = Depends(get_session)) -> bool:
        # 无须有效 JWT：过期会话同样需要清除浏览器的流式认证 Cookie。
        response.delete_cookie("eic_stream_token", path="/", httponly=True,
                               secure=request.url.scheme == "https", samesite="lax")
        response.headers["Cache-Control"] = "no-store"
        authorization = request.headers.get("authorization", "")
        bearer = authorization.split(" ", 1)[1] if authorization.lower().startswith("bearer ") else None
        await revoke_browser_session(session, [bearer, request.cookies.get("eic_stream_token")])
        return True


    @app.get("/api/Authorize/RegisterOptions")
    async def register_options(tenantId: int = 1000, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
        tenant = await session.scalar(select(Tenant).where(Tenant.code == tenantId, Tenant.active.is_(True)))
        if tenant is None:
            return {"departments": [], "positions": []}
        departments = await session.scalars(select(Department).where(Department.tenant_id == tenant.id, Department.active.is_(True)).order_by(Department.name))
        positions = await session.scalars(select(Position).where(Position.tenant_id == tenant.id, Position.active.is_(True)).order_by(Position.name))
        return {
            "departments": [serialize_department(item) for item in departments],
            "positions": [serialize_position(item) for item in positions],
        }


    @app.post("/api/Authorize/Register")
    async def register_user(payload: RegisterInput, session: AsyncSession = Depends(get_session)) -> bool:
        if not payload.registration_code.strip():
            raise HTTPException(status_code=400, detail={"code": 400, "errMsg": "注册必须填写注册码，请联系 admin 管理员"})
        tenant = await session.scalar(select(Tenant).where(Tenant.code == payload.tenant_id, Tenant.active.is_(True)))
        if tenant is None:
            raise HTTPException(status_code=400, detail={"code": 400, "errMsg": "租户不存在或已停用"})
        username = payload.name.strip()
        if len(username) < 2:
            raise HTTPException(status_code=400, detail={"code": 400, "errMsg": "用户名至少 2 个字符"})
        if len(payload.password) < 8:
            raise HTTPException(status_code=400, detail={"code": 400, "errMsg": "密码至少 8 位"})
        existing = await session.scalar(select(User).where(User.tenant_id == tenant.id, User.username == username))
        if existing is not None:
            raise HTTPException(status_code=400, detail={"code": 400, "errMsg": "用户名已存在"})
        department_id = payload.department_id
        position_id = payload.position_id
        if department_id:
            department = await session.get(Department, department_id)
            if department is None or department.tenant_id != tenant.id or not department.active:
                raise HTTPException(status_code=400, detail={"code": 400, "errMsg": "部门不存在或已停用"})
        if position_id:
            position = await session.get(Position, position_id)
            if position is None or position.tenant_id != tenant.id or not position.active:
                raise HTTPException(status_code=400, detail={"code": 400, "errMsg": "岗位不存在或已停用"})
        user_id = await new_id(session)
        await registration_codes.consume(payload.registration_code, tenant.id, user_id, session)
        session.add(User(
            id=user_id, tenant_id=tenant.id, username=username,
            nickname=payload.nickname or username, password_hash=hash_password(payload.password),
            is_active=False, is_super_admin=False, approval_status="pending",
            phone=(payload.phone or "")[:32], email=(payload.email or "")[:160],
            department_id=payload.department_id, position_id=payload.position_id,
        ))
        try:
            await session.commit()
        except IntegrityError as exc:
            await session.rollback()
            raise HTTPException(409, detail={"code": 409, "errMsg": "注册发生冲突，请检查用户名后重试"}) from exc
        return True
    return SimpleNamespace(get_token=get_token, logout=logout, register_options=register_options, register_user=register_user)
