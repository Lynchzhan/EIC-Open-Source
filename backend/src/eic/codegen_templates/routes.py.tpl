"""手工绑定后使用；复用现有业务服务，保留租户、权限及层级约束。"""
from fastapi import Depends, HTTPException, Query
from eic.models import __ENTITY__
from .schemas import PageQuery, SaveInput


def bind(app, *, current_principal, get_session, ensure_permission, page_input, list_records, save_record, delete_record):
    @app.post("/api/Generated/__ENTITY__/GetPageData")
    async def page(payload: PageQuery, principal=Depends(current_principal), session=Depends(get_session)):
        await ensure_permission("system.__SLUG__.read", principal, session)
        return await list_records(page_input(**payload.model_dump()), principal, session)

    @app.post("/api/Generated/__ENTITY__/AddEdit")
    async def save(payload: SaveInput, principal=Depends(current_principal), session=Depends(get_session)):
        await ensure_permission("system.__SLUG__.write", principal, session)
        if payload.id is not None:
            item = await session.get(__ENTITY__, payload.id)
            if item is None or item.tenant_id != principal.tenant_id:
                raise HTTPException(404, detail={"errMsg": "记录不存在"})
        return await save_record(payload.model_dump(), principal, session)

    @app.delete("/api/Generated/__ENTITY__/Delete")
    async def remove(Id: int = Query(ge=1, le=9223372036854775807), principal=Depends(current_principal), session=Depends(get_session)):
        await ensure_permission("system.__SLUG__.write", principal, session)
        return await delete_record(Id, principal, session)
