"""会话归属服务；领域任务不需要导入 Web 装配入口。"""
from fastapi import HTTPException
from sqlalchemy import select

from eic.models import ChatMessage, ChatSession, Resource


async def require_owned_chat_session(session, principal, session_id, *, allow_deleted=False):
    chat = await session.get(ChatSession, session_id)
    if chat is None or chat.tenant_id != principal.tenant_id or chat.owner_id != principal.user_id:
        raise HTTPException(404, detail={"code": 404, "errMsg": "会话不存在"})
    resource = await session.get(Resource, session_id)
    if resource is not None and (
        resource.tenant_id != principal.tenant_id or resource.resource_type != "chats"
        or resource.owner_id != principal.user_id or resource.is_deleted and not allow_deleted
    ):
        raise HTTPException(404, detail={"code": 404, "errMsg": "会话不存在"})
    return chat


async def require_owned_chat_request(session, principal, request_id, *, allow_deleted=False):
    candidates = list(await session.scalars(select(ChatMessage).where(
        ChatMessage.tenant_id == principal.tenant_id,
        ChatMessage.request_id == request_id, ChatMessage.role == "user").limit(2)))
    if len(candidates) != 1:
        raise HTTPException(404, detail={"code": 404, "errMsg": "对话请求不存在"})
    stored = candidates[0]
    await require_owned_chat_session(session, principal, stored.session_id, allow_deleted=allow_deleted)
    resource = await session.get(Resource, stored.id)
    if resource is not None and (
        resource.tenant_id != principal.tenant_id or resource.resource_type != "chat_messages"
        or resource.owner_id != principal.user_id or resource.is_deleted and not allow_deleted
    ):
        raise HTTPException(404, detail={"code": 404, "errMsg": "对话请求不存在"})
    return resource, stored
