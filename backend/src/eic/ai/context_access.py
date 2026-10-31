"""按会话所有权读取历史及加密工具结果，不接受文件路径或跨会话 ID。"""
from __future__ import annotations

import json
from sqlalchemy import select

from eic.ai.context_policy import context_settings
from eic.ai.conversation_context import visible_message
from eic.ai.context_manifest import bounded_tool_result
from eic.ai.memory_math import content_hash
from eic.ai.checkpoint_security import sanitize_tool_result
from eic.ai.standard_tools import StandardToolDefinition
from eic.core.ids import new_id
from eic.core.security import _fernet
from eic.models import ChatMessage, ChatSession, ContextArtifact, Resource


def tool_names(config):
    names = {"ReadContextArtifact"}
    if context_settings(config)["shortTermEnabled"] and context_settings(config)["mode"] == "retrieval":
        names.add("SearchChatHistory")
    return names


def definitions(config):
    specs = {
        "SearchChatHistory": ("检索当前会话此前原始消息及相邻消息；用于核实摘要中的细节。", {
            "query": {"type": "string", "minLength": 1, "maxLength": 200}}, ["query"]),
        "ReadContextArtifact": ("按返回的 artifactId 分页回读当前会话中外置的工具结果。", {
            "artifactId": {"type": "string", "pattern": "^[0-9]{1,19}$"},
            "offset": {"type": "integer", "minimum": 0, "maximum": 2000000}}, ["artifactId"]),
    }
    return [StandardToolDefinition(name=name, description=specs[name][0], tool_type="context",
        input_schema={"type": "object", "properties": specs[name][1], "required": specs[name][2],
                      "additionalProperties": False}) for name in sorted(tool_names(config))]


async def bounded_result(session, value, *, tenant_id, owner_id, session_id, request_id, call_id):
    value = sanitize_tool_result(value)
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    bounded = bounded_tool_result(value)
    if not json.loads(bounded).get("truncated"):
        return bounded
    # 超大结果受服务端存储上限约束，绝不承诺不存在的全文。
    if len(encoded.encode("utf-8")) > 2000000:
        return json.dumps({**json.loads(bounded), "readable": False,
                           "reason": "artifact_storage_limit"}, ensure_ascii=False)
    row = await session.scalar(select(ContextArtifact).where(
        ContextArtifact.tenant_id == tenant_id, ContextArtifact.owner_id == owner_id,
        ContextArtifact.session_id == session_id, ContextArtifact.request_id == request_id,
        ContextArtifact.call_id == call_id))
    digest = content_hash(encoded)
    if row is None:
        row = ContextArtifact(id=await new_id(session), tenant_id=tenant_id, owner_id=owner_id,
            session_id=session_id, request_id=request_id, call_id=call_id, content_hash=digest,
            content_encrypted=_fernet().encrypt(encoded.encode("utf-8")).decode("ascii"))
        session.add(row)
    elif row.content_hash != digest:
        raise ValueError("context_artifact_binding_changed")
    return json.dumps({**json.loads(bounded), "artifactId": str(row.id), "readable": True}, ensure_ascii=False)


async def invoke(session, name, arguments, *, tenant_id, owner_id, session_id, before_id):
    chat = await session.scalar(select(ChatSession).where(ChatSession.id == session_id,
        ChatSession.tenant_id == tenant_id, ChatSession.owner_id == owner_id))
    if chat is None:
        raise ValueError("context_session_unavailable")
    resource = await session.get(Resource, session_id, populate_existing=True)
    if resource and (resource.is_deleted or resource.tenant_id != tenant_id or resource.owner_id != owner_id
                     or resource.resource_type != "chats"):
        raise ValueError("context_session_unavailable")
    if name == "ReadContextArtifact":
        row = await session.scalar(select(ContextArtifact).where(
            ContextArtifact.id == int(arguments["artifactId"]), ContextArtifact.tenant_id == tenant_id,
            ContextArtifact.owner_id == owner_id, ContextArtifact.session_id == session_id))
        if row is None:
            return {"status": "missing"}
        source = await session.scalar(select(ChatMessage.id).where(ChatMessage.tenant_id == tenant_id,
            ChatMessage.session_id == session_id, ChatMessage.request_id == row.request_id,
            ChatMessage.role == "user", ChatMessage.id <= before_id, visible_message(tenant_id)).limit(1))
        if source is None:
            return {"status": "missing", "reason": "source_unavailable"}
        value = _fernet().decrypt(row.content_encrypted.encode("ascii")).decode("utf-8")
        if content_hash(value) != row.content_hash:
            raise ValueError("context_artifact_hash_invalid")
        offset = arguments.get("offset", 0)
        preview = value[offset:offset + 900]
        return {"artifactId": str(row.id), "hash": row.content_hash, "content": preview,
                "lowTrust": True, "offset": offset,
                "nextOffset": offset + len(preview) if offset + len(preview) < len(value) else None}
    if name != "SearchChatHistory":
        raise ValueError("context_tool_unavailable")
    from eic.core.memory_lifecycle import forget_epoch
    _, watermark = await forget_epoch(session, tenant_id, owner_id)
    base = select(ChatMessage).where(ChatMessage.tenant_id == tenant_id,
        ChatMessage.session_id == session_id, ChatMessage.id < before_id,
        ChatMessage.id > watermark,
        ChatMessage.role.in_(("user", "assistant")), ChatMessage.status == "completed", visible_message(tenant_id))
    hits = list(await session.scalars(base.where(ChatMessage.content.contains(arguments["query"], autoescape=True))
        .order_by(ChatMessage.id.desc()).limit(3)))
    selected = {row.id: row for row in hits}
    for hit in hits:
        for row in await session.scalars(base.where(ChatMessage.id < hit.id).order_by(ChatMessage.id.desc()).limit(1)):
            selected[row.id] = row
        for row in await session.scalars(base.where(ChatMessage.id > hit.id).order_by(ChatMessage.id).limit(1)):
            selected[row.id] = row
    return {"messages": [{"id": str(row.id), "role": row.role, "content": row.content[:1500],
        "hash": content_hash(row.content), "truncated": len(row.content) > 1500}
        for row in sorted(selected.values(), key=lambda row: row.id)], "lowTrust": True}
