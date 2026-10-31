"""记忆激活和持久收据在同一事务中完成；无外部副作用，可安全重试。"""
from eic.core.authorization import ensure_permission
from eic.core.authentication import Principal
from eic.core.chat_ownership import require_owned_chat_session
from eic.ai.app_config import runtime_app_config
from datetime import UTC

from fastapi import HTTPException

from eic.core.ids import new_id
from eic.models import AgentMemory, ChatMessage, ChatSession, OutboxEvent, Resource, User


async def consume_memory_write(sessions, event_id: int, tenant_id: int):
    from eic.core.outbox import now

    async with sessions() as session:
        # 共享数据库行锁，不用进程内字典判定幂等。
        await new_id(session)
        event = await session.get(OutboxEvent, event_id)
        if event is None or event.tenant_id != tenant_id or event.kind != "memory-write":
            return {"status": "missing"}
        if event.consume_status == "completed":
            return event.result
        timestamp = await now(session)
        if timestamp < (event.available_at.replace(tzinfo=UTC)
                        if event.available_at.tzinfo is None else event.available_at):
            return {"status": "pending"}
        memory = await session.get(AgentMemory, event.entity_id)
        valid = memory is not None and memory.tenant_id == tenant_id and memory.status == "pending"
        user = await session.get(User, memory.owner_id) if valid else None
        valid = valid and user is not None and user.tenant_id == tenant_id and user.is_active
        if valid:
            try:
                await ensure_permission(
                    "memory.write",
                    Principal(user.id, tenant_id, user.username, user.is_super_admin), session,
                )
                if memory.session_id is not None:
                    principal = Principal(user.id, tenant_id, user.username, user.is_super_admin)
                    chat = await require_owned_chat_session(session, principal, memory.session_id)
                    valid = (chat is not None and chat.tenant_id == tenant_id and chat.owner_id == user.id
                             and chat.app_id == memory.agent_id)
                from eic.ai.context_policy import memory_settings
                config = await runtime_app_config(session, tenant_id, memory.agent_id) if memory.agent_id else {}
                valid = valid and (not memory.agent_id or memory_settings(config)["mode"] != "off")
            except HTTPException:
                valid = False
        if valid:
            if memory.source_message_id is not None:
                source = await session.get(ChatMessage, memory.source_message_id)
                source_resource = await session.get(Resource, memory.source_message_id)
                source_chat = await session.get(ChatSession, source.session_id) if source else None
                valid = (
                    source is not None and source.tenant_id == tenant_id
                    and source.role == "user" and source.status == "completed"
                    and source.session_id == memory.session_id
                    and source_chat is not None and source_chat.tenant_id == tenant_id
                    and source_chat.owner_id == user.id
                    and not (source_resource and source_resource.is_deleted)
                )
            for expiry in (memory.valid_until, memory.expires_at):
                if expiry is not None:
                    expiry = expiry.replace(tzinfo=UTC) if expiry.tzinfo is None else expiry
                    valid = valid and expiry > timestamp
        if valid:
            memory.status = "active"
            from eic.core.memory_lifecycle import stage_index
            await stage_index(session, memory)
        elif memory is not None and memory.tenant_id == tenant_id and memory.status == "pending":
            memory.status = "cancelled"
        if memory is not None and memory.tenant_id == tenant_id:
            from eic.core.memory_lifecycle import record
            record(session, memory, "memory_save", memory.status)
        result = {"status": "completed" if valid else "cancelled",
                  "memoryId": memory.id if valid else None, "automaticRetry": False}
        event.consume_status, event.consume_token, event.consume_until = "completed", None, None
        event.result = result
        await session.commit()
        return result
