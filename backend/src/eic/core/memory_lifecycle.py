"""SQL 为记忆的唯一权威；修订和遗忘同时撤销派生数据。"""
from eic.ai.app_config import runtime_app_config
from eic.core.authentication import Principal
from eic.core.chat_ownership import require_owned_chat_session
from datetime import UTC, datetime
import hashlib
import uuid
from sqlalchemy import delete, func, select, update

from eic.ai.context_policy import memory_settings, policy_hash
from eic.core.ids import new_id
from eic.models import AgentMemory, ChatMessage, ChatSession, ChatSummary, ContextArtifact, ContextJob, MemoryTombstone, MemoryVector, OutboxEvent


def record(session, row, stage, status, **summary):
    from eic.core import observability
    session_id = getattr(row, "session_id", None)
    prefix = "memory-index" if getattr(row, "kind", None) == "memory-index" else "context"
    request_id = f"{prefix}:{row.id}"
    trace_id = observability.turn_trace(row.tenant_id, request_id)
    if isinstance(row, ContextJob) and row.kind != "memory-index" and row.run_id:
        bound_trace = ((row.usage or {}).get("_observation") or {}).get("trace_id")
        if bound_trace in {trace_id, uuid.uuid5(uuid.NAMESPACE_URL, row.run_id).hex}:
            trace_id = bound_trace
    # 任务收据常在 run_json_job 恢复调用方上下文后提交，显式绑定任务根。
    parent = ({"parent_span_id": observability.turn_span(row.tenant_id, request_id)}
              if isinstance(row, ContextJob) else {})
    if isinstance(row, ContextJob) and row.run_id:
        parent["run_id"] = row.run_id
    observability.stage(session, observability.observation("agent_run", stage, status,
        tenant_id=row.tenant_id, owner_id=row.owner_id, summary=summary, source="memory_ops",
        trace_id=trace_id, request_id=request_id,
        chat_id=str(session_id) if session_id else None,
        span_id=observability.turn_span(row.tenant_id, f"{request_id}:{stage}"), **parent))


async def capacity(session, tenant_id, owner_id, agent_id, maximum):
    from fastapi import HTTPException
    await new_id(session)
    count = await session.scalar(select(func.count()).select_from(AgentMemory).where(
        AgentMemory.tenant_id == tenant_id, AgentMemory.owner_id == owner_id,
        AgentMemory.agent_id == agent_id, AgentMemory.status.in_(("active", "pending")))) or 0
    if count >= maximum:
        raise HTTPException(409, detail={"errMsg": "已达到记忆条目上限，请先归档或遗忘旧记忆"})


async def stage_index(session, memory, model_id=None, *, budget_session_id=None):
    from eic.core.memory_index import fingerprint
    from eic.core.outbox import stage
    from eic.core.runtime_refs import require_runtime_model

    if memory.status != "active":
        return None
    from eic.core.memory_search import index_terms
    memory.content_hash = hashlib.sha256(memory.content.encode("utf-8")).hexdigest()
    await index_terms(session, memory)
    timestamp = datetime.now(UTC)
    if any(value is not None and utc(value) <= timestamp for value in (memory.valid_until, memory.expires_at)):
        memory.index_status = "expired"
        return None
    if memory.valid_from is not None and utc(memory.valid_from) > timestamp:
        memory.index_status = "scheduled"
    memory.content_hash = hashlib.sha256(memory.content.encode("utf-8")).hexdigest()
    config = await runtime_app_config(session, memory.tenant_id, memory.agent_id) if memory.agent_id else {}
    budget_session_id = budget_session_id or memory.session_id
    if budget_session_id:
        from fastapi import HTTPException
        principal = Principal(memory.owner_id, memory.tenant_id, "", False)
        try:
            chat = await require_owned_chat_session(session, principal, budget_session_id)
        except HTTPException:
            memory.index_status = "requires_session_budget"
            return None
        if chat.app_id != memory.agent_id:
            raise ValueError("memory_index_budget_agent_mismatch")
        config = await runtime_app_config(session, memory.tenant_id, chat.app_id)
    model_id = model_id or memory_settings(config)["embeddingModelId"]
    if not model_id:
        memory.index_status = "unconfigured"
        return None
    if budget_session_id is None:
        memory.index_status = "requires_session_budget"
        return None
    model = await require_runtime_model(session, memory.tenant_id, int(model_id), "embedding")
    bound = fingerprint(model)
    existing = await session.scalar(select(MemoryVector).where(MemoryVector.memory_id == memory.id,
        MemoryVector.memory_version == memory.version, MemoryVector.model_fingerprint == bound,
        MemoryVector.content_hash == memory.content_hash, MemoryVector.status == "ready"))
    if existing:
        memory.index_status, memory.embedding_fingerprint = "ready", bound
        from eic.core.memory_search import stage_projection
        await stage_projection(session, memory)
        return None
    key = "memory-index:" + policy_hash([memory.id, memory.version, memory.content_hash, bound])
    job = await session.scalar(select(ContextJob).where(ContextJob.tenant_id == memory.tenant_id,
        ContextJob.job_key == key))
    if job is not None:
        if job.result_encrypted and (job.status == "completed" or job.status == "cancelled"
                and job.error_code == "memory_index_source_deleted_result_preserved"):
            # 向量缺失只重做持久结果的本地提交，不再请求 embedding。
            job.status = "provider_done"
            job.session_id = budget_session_id
            job.payload = {**job.payload, "runtimePolicy": config.get("runtimePolicy")}
            await stage(session, "memory-index", job.id, memory.tenant_id,
                event_key=f"memory-index-repair:{job.id}:{await new_id(session)}")
        elif (job.status == "cancelled" and job.error_code == "memory_index_source_deleted_before_provider"
                and job.lease_token is None and not job.result_encrypted):
            # 只有持久确认尚未开始外呼的删除取消任务，才能在新预算会话显式重建。
            job.status, job.error_code, job.session_id = "pending", "", budget_session_id
            job.payload = {**job.payload, "runtimePolicy": config.get("runtimePolicy")}
            await stage(session, "memory-index", job.id, memory.tenant_id,
                event_key=f"memory-index-rebind:{job.id}:{await new_id(session)}",
                available_at=utc(memory.valid_from) if memory.valid_from and utc(memory.valid_from) > timestamp else None)
        memory.index_status = job.status
        return job.id if job.status in {"pending", "running", "provider_started", "provider_done"} else None
    identity = await new_id(session)
    job = ContextJob(id=identity, tenant_id=memory.tenant_id, owner_id=memory.owner_id,
        session_id=budget_session_id, kind="memory-index", job_key=key,
        source_hash=memory.content_hash, payload={"memoryId": memory.id, "version": memory.version,
            "modelId": int(model.id), "fingerprint": bound, "runtimePolicy": config.get("runtimePolicy")}, status="pending")
    session.add(job)
    future = memory.valid_from is not None and utc(memory.valid_from) > timestamp
    memory.index_status = "scheduled" if future else "pending"
    await stage(session, "memory-index", identity, memory.tenant_id, event_key=key,
                available_at=utc(memory.valid_from) if future else None)
    return identity


async def invalidate(session, memory, reason, *, forget=False):
    identity = await new_id(session)
    from eic.core.memory_search import stage_cleanup
    await stage_cleanup(session, memory)
    from eic.models import MemoryProjectionReceipt
    await session.execute(delete(MemoryProjectionReceipt).where(MemoryProjectionReceipt.vector_id.in_(
        select(MemoryVector.id).where(MemoryVector.memory_id == memory.id))))
    if reason == "archived":
        memory.version += 1
    await session.execute(delete(MemoryVector).where(MemoryVector.tenant_id == memory.tenant_id,
        MemoryVector.owner_id == memory.owner_id, MemoryVector.memory_id == memory.id))
    memory.index_status, memory.embedding_fingerprint = "invalidated", None
    memory.invalidation_reason = reason
    # 保守撤销该用户的全部摘要，不依赖模型自报引用是否完整。
    await session.execute(update(ChatSummary).where(ChatSummary.tenant_id == memory.tenant_id,
        ChatSummary.owner_id == memory.owner_id, ChatSummary.status == "active").values(status="invalidated"))
    if forget:
        memory.version += 1
        through = await session.scalar(select(func.max(ChatMessage.id)).join(ChatSession).where(
            ChatSession.tenant_id == memory.tenant_id, ChatSession.owner_id == memory.owner_id)) or 0
        session.add(MemoryTombstone(id=identity, tenant_id=memory.tenant_id, owner_id=memory.owner_id,
            agent_id=memory.agent_id, memory_id=memory.id, memory_key=memory.memory_key,
            content_hash=hashlib.sha256(memory.content.encode("utf-8")).hexdigest(),
            source_through_id=through))
        memory.content, memory.attributes = "", {}
        memory.status = "forgotten"
        await session.execute(update(ChatSummary).where(ChatSummary.tenant_id == memory.tenant_id,
            ChatSummary.owner_id == memory.owner_id).values(content={}, status="invalidated"))
        await session.execute(delete(ContextArtifact).where(ContextArtifact.tenant_id == memory.tenant_id,
            ContextArtifact.owner_id == memory.owner_id))
        await session.execute(update(ContextJob).where(ContextJob.tenant_id == memory.tenant_id,
            ContextJob.owner_id == memory.owner_id,
            ContextJob.kind.in_(("chat-compaction", "memory-extraction")))
            .values(result_encrypted=None, status="cancelled", error_code="user_forgotten"))
        # 旧候选也不能通过确认重新带回遗忘水位以前的事实。
        await session.execute(update(AgentMemory).where(AgentMemory.tenant_id == memory.tenant_id,
            AgentMemory.owner_id == memory.owner_id, AgentMemory.status == "candidate",
            AgentMemory.source_message_id <= through).values(content="", attributes={}, status="rejected"))
        await session.execute(update(ContextJob).where(ContextJob.tenant_id == memory.tenant_id,
            ContextJob.owner_id == memory.owner_id, ContextJob.kind == "memory-index",
            ContextJob.source_hash == memory.content_hash).values(result_encrypted=None, status="cancelled",
                                                                    error_code="user_forgotten"))
    record(session, memory, "memory_invalidation", "forgotten" if forget else "invalidated")


async def forget_epoch(session, tenant_id, owner_id):
    rows = list(await session.scalars(select(MemoryTombstone).where(
        MemoryTombstone.tenant_id == tenant_id, MemoryTombstone.owner_id == owner_id)
        .order_by(MemoryTombstone.id)))
    return policy_hash([[r.id, r.memory_id, r.source_through_id] for r in rows]), max(
        (r.source_through_id for r in rows), default=0)


async def clear_context(session, tenant_id, owner_id, session_id, *, detach_memories=False):
    """删除会话或消息时撤销派生内容；已确认长期记忆保留，由用户另行遗忘。"""
    await new_id(session)
    scope = [ContextJob.tenant_id == tenant_id, ContextJob.owner_id == owner_id,
             ContextJob.session_id == session_id]
    await session.execute(update(ContextJob).where(*scope, ContextJob.kind != "memory-index").values(
        status="cancelled", result_encrypted=None, error_code="context_source_deleted"))
    indexes = list(await session.scalars(select(ContextJob).where(*scope, ContextJob.kind == "memory-index")))
    for job in indexes:
        if job.status == "completed":
            continue
        if job.status == "pending" and job.lease_token is None:
            job.status, job.error_code = "cancelled", "memory_index_source_deleted_before_provider"
        elif job.status == "provider_done" and job.result_encrypted:
            job.status, job.error_code = "cancelled", "memory_index_source_deleted_result_preserved"
        elif job.status == "provider_started":
            job.status, job.error_code = "uncertain", "memory_index_source_deleted_outcome_unknown"
        # 撤销全部旧消费凭据，防止重绑后旧 event 抢用新预算会话发起调用。
        await session.execute(update(OutboxEvent).where(OutboxEvent.tenant_id == tenant_id,
            OutboxEvent.kind == "memory-index", OutboxEvent.entity_id == job.id).values(
                consume_status="completed", consume_token=None, consume_until=None,
                result={"status": job.status, "reason": "context_source_deleted"}))
        await session.execute(update(AgentMemory).where(AgentMemory.tenant_id == tenant_id,
            AgentMemory.owner_id == owner_id, AgentMemory.id == job.payload["memoryId"],
            AgentMemory.version == job.payload["version"]).values(index_status=job.status))
    await session.execute(delete(ContextArtifact).where(ContextArtifact.tenant_id == tenant_id,
        ContextArtifact.owner_id == owner_id, ContextArtifact.session_id == session_id))
    await session.execute(update(ChatSummary).where(ChatSummary.tenant_id == tenant_id,
        ChatSummary.owner_id == owner_id, ChatSummary.session_id == session_id)
        .values(status="invalidated", content={}))
    memories = [AgentMemory.tenant_id == tenant_id, AgentMemory.owner_id == owner_id,
                AgentMemory.session_id == session_id]
    await session.execute(update(AgentMemory).where(*memories, AgentMemory.status == "candidate")
        .values(status="rejected", content="", attributes={}))
    await session.execute(update(AgentMemory).where(*memories, AgentMemory.status == "pending")
        .values(status="cancelled", index_status="cancelled"))
    if detach_memories:
        from types import SimpleNamespace
        from eic.core.memory_search import stage_cleanup
        from eic.models import MemoryProjectionReceipt, MemorySearchDocument, MemorySearchTerm
        after_id = 0
        while True:
            short_ids = list(await session.scalars(select(AgentMemory.id).where(*memories,
                AgentMemory.memory_type.in_(("short_term", "session")), AgentMemory.id > after_id)
                .order_by(AgentMemory.id).limit(100)))
            if not short_ids:
                break
            for memory_id in short_ids:
                await stage_cleanup(session, SimpleNamespace(id=memory_id, tenant_id=tenant_id))
            await session.execute(delete(MemoryProjectionReceipt).where(MemoryProjectionReceipt.vector_id.in_(
                select(MemoryVector.id).where(MemoryVector.memory_id.in_(short_ids)))))
            await session.execute(delete(MemorySearchTerm).where(MemorySearchTerm.memory_id.in_(short_ids)))
            await session.execute(delete(MemorySearchDocument).where(MemorySearchDocument.memory_id.in_(short_ids)))
            await session.execute(delete(MemoryVector).where(MemoryVector.memory_id.in_(short_ids)))
            await session.execute(delete(AgentMemory).where(AgentMemory.id.in_(short_ids)))
            after_id = short_ids[-1]
        # 长期记忆不按来源会话过滤投影，解除来源无需重新写入全部向量。
        await session.execute(update(AgentMemory).where(*memories)
            .values(session_id=None, source_message_id=None))



def utc(value):
    return value.replace(tzinfo=UTC) if value is not None and value.tzinfo is None else value
