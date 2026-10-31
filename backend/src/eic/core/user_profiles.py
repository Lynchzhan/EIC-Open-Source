"""当前账号资料的租户归属及可选行锁；不合并或覆盖 profile 内容。"""
from fastapi import HTTPException
from sqlalchemy import select
from eic.models import User


async def owned_user(principal, session, *, lock=False):
    statement = select(User).where(User.id == principal.user_id,
        User.tenant_id == principal.tenant_id).execution_options(populate_existing=True)
    if lock:
        statement = statement.with_for_update()
    user = await session.scalar(statement)
    if user is None:
        raise HTTPException(404, detail={"errMsg": "当前账号不存在"})
    return user
