"""模型测试设置属于当前账号，使用 profile 合并及 revision 并发保护。"""
from typing import Literal

from fastapi import Depends, HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import update

from .models import User
from eic.core.user_profiles import owned_user

DEFAULT_PROMPT = "请用一句话简要介绍自己。"
DEFAULT_TIMEOUT_SECONDS = 180


class ModelTestPreferencesInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    schemaVersion: Literal[1]
    expectedRevision: int = Field(ge=0)
    prompt: str = Field(min_length=1, max_length=16000)
    timeoutSeconds: int = Field(ge=10, le=300)

    @field_validator("prompt")
    @classmethod
    def nonempty_prompt(cls, value):
        if not value.strip():
            raise ValueError("测试文本不能为空")
        return value


def public_preferences(profile):
    try:
        raw = (profile or {}).get("modelTestPreferences") or {}
        parsed = ModelTestPreferencesInput.model_validate({
            "schemaVersion": raw.get("schemaVersion", 1),
            "expectedRevision": raw.get("revision", 0),
            "prompt": raw.get("prompt", DEFAULT_PROMPT),
            "timeoutSeconds": raw.get("timeoutSeconds", DEFAULT_TIMEOUT_SECONDS),
        })
    except (ValueError, TypeError, AttributeError):
        raise HTTPException(409, detail={"errMsg": "已存模型测试设置格式不兼容，请联系管理员处理"}) from None
    return {"schemaVersion": 1, "revision": parsed.expectedRevision,
            "prompt": parsed.prompt, "timeoutSeconds": parsed.timeoutSeconds}


async def read_current(principal, session):
    return public_preferences((await owned_user(principal, session)).profile)


def bind_routes(app, current_principal, get_session, ensure_permission):
    @app.get("/api/User/ModelTestPreferences")
    async def read_preferences(response: Response, principal=Depends(current_principal), session=Depends(get_session)):
        await ensure_permission("ai.model.write", principal, session)
        response.headers["Cache-Control"] = "no-store"
        return await read_current(principal, session)

    @app.put("/api/User/ModelTestPreferences")
    async def save_preferences(payload: ModelTestPreferencesInput, response: Response,
                               principal=Depends(current_principal), session=Depends(get_session)):
        await ensure_permission("ai.model.write", principal, session)
        response.headers["Cache-Control"] = "no-store"
        user = await owned_user(principal, session, lock=True)
        old_profile = user.profile
        current = public_preferences(old_profile)
        if current["revision"] != payload.expectedRevision:
            raise HTTPException(409, detail={"errMsg": "测试设置已被其他设备修改，请重新载入后保存"})
        value = {"schemaVersion": 1, "revision": current["revision"] + 1,
                 "prompt": payload.prompt, "timeoutSeconds": payload.timeoutSeconds}
        conditions = [User.id == user.id, User.tenant_id == principal.tenant_id]
        if session.get_bind().dialect.name == "sqlite":
            conditions.append(User.profile.is_(None) if old_profile is None else User.profile == old_profile)
        result = await session.execute(update(User).where(*conditions).values(
            profile={**(old_profile or {}), "modelTestPreferences": value},
        ).execution_options(synchronize_session=False))
        if result.rowcount != 1:
            await session.rollback()
            raise HTTPException(409, detail={"errMsg": "账号资料已变化，请重新载入测试设置"})
        await session.commit()
        response.headers["Cache-Control"] = "no-store"
        return value
