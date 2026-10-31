"""messages 领域；通过显式装配提供跨域依赖。"""
from eic.api.resource_inputs import MessageWrite, write_payload
from dataclasses import dataclass
from collections.abc import Callable
from types import SimpleNamespace as _RouteExports
from typing import Any
from eic.api.runtime import RuntimeBindings
from sqlalchemy.ext.asyncio import AsyncSession
from fastapi import Depends
from fastapi import HTTPException
from eic.models import MessageReadReceipt
from eic.api.schemas import PageInput as PageInput
from eic.core.authentication import Principal
from fastapi import Request
from eic.models import Resource
from eic.core.database import get_session
from eic.core.message_reads import read_fields
from eic.core.message_reads import receipts
from sqlalchemy import select


@dataclass(frozen=True)
class Dependencies:
    delete_resource: Callable[..., Any]
    ensure_permission: Callable[..., Any]
    lock_tenant_writes: Callable[..., Any]
    resource_page: Callable[..., Any]
    save_resource: Callable[..., Any]


def bind_routes(app, *, dependencies: Dependencies, runtime: RuntimeBindings, current_principal):
    @app.post("/api/Message/GetPrivateUserSystemPageData")
    async def message_private_system_page(payload: PageInput, principal: Principal = Depends(current_principal), session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
        return await dependencies.resource_page("messages", payload, principal, session, type_filter="private-system")


    @app.post("/api/Message/GetPrivateUserPageData")
    async def message_private_user_page(payload: PageInput, principal: Principal = Depends(current_principal), session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
        return await dependencies.resource_page("messages", payload, principal, session, type_filter="private-user")


    @app.post("/api/Message/GetAIUserPageData")
    async def message_ai_user_page(payload: PageInput, principal: Principal = Depends(current_principal), session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
        return await dependencies.resource_page("messages", payload, principal, session, type_filter="ai-user")


    @app.post("/api/Message/GetPageData")
    async def message_page(payload: PageInput, principal: Principal = Depends(current_principal), session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
        return await dependencies.resource_page("messages", payload, principal, session)


    @app.post("/api/Message/AddEdit")
    async def message_add_edit(payload: MessageWrite, request: Request, principal: Principal = Depends(current_principal), session: AsyncSession = Depends(get_session)) -> bool:
        payload = write_payload(payload, MessageWrite)
        return await dependencies.save_resource("messages", payload, principal, session, request)


    @app.get("/api/Dictionary/GetTypeList")
    async def dictionary_type_list(principal: Principal = Depends(current_principal), session: AsyncSession = Depends(get_session)) -> list[dict[str, Any]]:
        await dependencies.ensure_permission("ai.resource.read", principal, session)
        rows = await session.scalars(select(Resource).where(Resource.tenant_id == principal.tenant_id, Resource.resource_type == "dictionaries", Resource.is_deleted.is_(False)))
        return [{"id": row.id, "name": row.name, "type": (row.data or {}).get("type", "default")} for row in rows]


    @app.get("/api/Dictionary/GetTypeWhereList")
    async def dictionary_type_where_list(type: str = "default", principal: Principal = Depends(current_principal), session: AsyncSession = Depends(get_session)) -> list[dict[str, Any]]:
        await dependencies.ensure_permission("ai.resource.read", principal, session)
        rows = await session.scalars(select(Resource).where(Resource.tenant_id == principal.tenant_id, Resource.resource_type == "dictionaries", Resource.is_deleted.is_(False)))
        return [{"id": row.id, "name": row.name, **(row.data or {})} for row in rows if (row.data or {}).get("type", "default") == type]


    @app.post("/api/Log/GetHttpLogPageData")
    @app.post("/api/HttpLog/GetPageData")
    @app.post("/api/HttpLogs/GetPageData")
    async def http_log_page(payload: PageInput, principal: Principal = Depends(current_principal), session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
        return await dependencies.resource_page("http_logs", payload, principal, session)


    @app.post("/api/Log/GetOSLogPageData")
    @app.post("/api/OSLog/GetPageData")
    @app.post("/api/OSLogs/GetPageData")
    async def os_log_page(payload: PageInput, principal: Principal = Depends(current_principal), session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
        return await dependencies.resource_page("os_logs", payload, principal, session)


    @app.post("/api/Message/GetSystemPageData")
    async def message_system_page(payload: PageInput, principal: Principal = Depends(current_principal), session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
        return await dependencies.resource_page("messages", payload, principal, session, type_filter="system")


    @app.post("/api/Message/GetAnnouncementPageData")
    async def message_announcement_page(payload: PageInput, principal: Principal = Depends(current_principal), session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
        return await dependencies.resource_page("messages", payload, principal, session, type_filter="announcement", skip_permission=True)


    @app.get("/api/Message/GetMyNoReadCount")
    async def message_unread_count(principal: Principal = Depends(current_principal), session: AsyncSession = Depends(get_session)) -> dict[str, int]:
        rows = list(await session.scalars(select(Resource).where(Resource.tenant_id == principal.tenant_id, Resource.resource_type == "messages", Resource.is_deleted.is_(False))))
        states = await receipts(session, principal.tenant_id, principal.user_id, [row.id for row in rows])
        fields = [read_fields(row.data or {}, states.get(row.id)) for row in rows]
        count = sum(not item["isRead"] for item in fields)
        unknown = sum(item["readState"] == "unknown" for item in fields)
        return {"count": count, "unreadCount": count, "unknownCount": unknown}


    @app.get("/api/Message/Read")
    async def message_read(messageId: int, principal: Principal = Depends(current_principal), session: AsyncSession = Depends(get_session)) -> bool:
        await dependencies.lock_tenant_writes(session, principal.tenant_id)
        item = await session.get(Resource, messageId)
        if item is None or item.tenant_id != principal.tenant_id or item.resource_type != "messages" or item.is_deleted:
            return False
        receipt = await session.get(MessageReadReceipt, (principal.tenant_id, messageId, principal.user_id))
        if receipt is None:
            session.add(MessageReadReceipt(tenant_id=principal.tenant_id, message_id=messageId, user_id=principal.user_id))
        await session.commit()
        return True


    @app.delete("/api/Message/Delete")
    async def message_delete_legacy(request: Request, Id: int | None = None, id: int | None = None, principal: Principal = Depends(current_principal), session: AsyncSession = Depends(get_session)) -> bool:
        item_id = Id if Id is not None else id
        if item_id is None:
            raise HTTPException(status_code=400, detail={"code": 400, "errMsg": "缺少消息 ID"})
        return await dependencies.delete_resource("messages", item_id, principal, session, request)

    return _RouteExports(
        message_private_system_page=message_private_system_page,
        message_private_user_page=message_private_user_page,
        message_ai_user_page=message_ai_user_page,
        message_page=message_page,
        message_add_edit=message_add_edit,
        dictionary_type_list=dictionary_type_list,
        dictionary_type_where_list=dictionary_type_where_list,
        http_log_page=http_log_page,
        os_log_page=os_log_page,
        message_system_page=message_system_page,
        message_announcement_page=message_announcement_page,
        message_unread_count=message_unread_count,
        message_read=message_read,
        message_delete_legacy=message_delete_legacy,
    )
