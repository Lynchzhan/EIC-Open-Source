"""当前账号的外观偏好；独立校验并保留其他 profile 字段。"""
from typing import Literal

from fastapi import Depends, HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import update

from .models import User, Tenant, Department
from eic.core.user_profiles import owned_user


class FontSizes(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    nav: int = Field(default=15, ge=14, le=20)
    ui: int = Field(default=15, ge=14, le=20)
    chat: int = Field(default=16, ge=14, le=24)
    meta: int = Field(default=13, ge=12, le=18)
    code: int = Field(default=14, ge=12, le=22)


class AppearanceInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    schemaVersion: Literal[1]
    expectedRevision: int = Field(ge=0)
    theme: Literal["light", "dark"]
    fontSizes: FontSizes


def public_preferences(profile):
    try:
        raw = (profile or {}).get("appearancePreferences") or {}
        parsed = AppearanceInput.model_validate({
            "schemaVersion": raw.get("schemaVersion", 1),
            "expectedRevision": raw.get("revision", 0),
            "theme": raw.get("theme", "light"),
            "fontSizes": raw.get("fontSizes", {}),
        })
    except (ValueError, TypeError, AttributeError):
        raise HTTPException(409, detail={"errMsg": "已存外观设置格式不兼容，请联系管理员处理"})
    return {"schemaVersion": 1, "revision": parsed.expectedRevision,
            "theme": parsed.theme, "fontSizes": parsed.fontSizes.model_dump()}


def bind_routes(app, current_principal, get_session):
    @app.get("/api/User/AppearancePreferences")
    async def read_preferences(response: Response, principal=Depends(current_principal), session=Depends(get_session)):
        response.headers["Cache-Control"] = "no-store"
        user = await owned_user(principal, session)
        return public_preferences(user.profile)

    @app.put("/api/User/AppearancePreferences")
    async def save_preferences(payload: AppearanceInput, response: Response,
                               principal=Depends(current_principal), session=Depends(get_session)):
        response.headers["Cache-Control"] = "no-store"
        user = await owned_user(principal, session, lock=True)
        old_profile = user.profile
        current = public_preferences(old_profile)
        if current["revision"] != payload.expectedRevision:
            raise HTTPException(409, detail={"errMsg": "外观设置已被其他设备修改，请重新载入后再保存"})
        value = {"schemaVersion": 1, "revision": current["revision"] + 1,
                 "theme": payload.theme, "fontSizes": payload.fontSizes.model_dump()}
        merged = {**(old_profile or {}), "appearancePreferences": value}
        # MySQL/PostgreSQL 由行锁串行化；SQLite 不支持行锁，额外比较旧 JSON。
        # PostgreSQL 的 JSON 类型没有等号运算符，不能直接套用 SQLite 的条件。
        conditions = [User.id == user.id, User.tenant_id == principal.tenant_id]
        if session.get_bind().dialect.name == "sqlite":
            conditions.append(User.profile.is_(None) if old_profile is None else User.profile == old_profile)
        result = await session.execute(update(User).where(*conditions).values(
            profile=merged,
        ).execution_options(synchronize_session=False))
        if result.rowcount != 1:
            await session.rollback()
            raise HTTPException(409, detail={"errMsg": "账号资料已变化，请重新载入外观设置"})
        await session.commit()
        return value

    @app.get("/api/User/WorkspaceContext")
    async def workspace_context(response: Response, principal=Depends(current_principal), session=Depends(get_session)):
        response.headers["Cache-Control"] = "no-store"
        user = await owned_user(principal, session)
        tenant = await session.get(Tenant, principal.tenant_id)
        department = await session.get(Department, user.department_id) if user.department_id else None
        return {"tenantName": tenant.name if tenant else "",
                "organizationName": department.name if department and department.tenant_id == principal.tenant_id else ""}
