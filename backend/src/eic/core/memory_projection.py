"""仅投影已持久向量；从不补发 embedding，失败可安全重试本地索引同步。"""
from datetime import UTC, datetime
from sqlalchemy import delete, select

from eic.ai.memory_math import content_hash, cosine
from eic.core.config import get_settings
from eic.core.ids import new_id
from eic.core.memory_search import PROJECTION
from eic.core.memory_lifecycle import utc
from eic.models import AgentMemory, MemoryProjectionReceipt, MemoryVector, OutboxEvent, User


async def process_projection(ctx, memory_id, tenant_id, *, sessions):
    if not getattr(get_settings(), "memory_vector_projection_enabled", False):
        # 已排队任务也保留未执行意图，由 Outbox 换新身份等待重新启用。
        raise ValueError("memory_projection_disabled")
    async with sessions() as session:
        generation = await new_id(session)
        event = await session.get(OutboxEvent, ctx["event_id"], populate_existing=True)
        timestamp = datetime.now(UTC)
        if (event is None or event.tenant_id != tenant_id or event.entity_id != memory_id
                or event.kind != "memory-projection" or event.consume_token != ctx["consume_token"]
                or event.consume_status != "running" or event.consume_until is None
                or utc(event.consume_until) <= timestamp):
            raise ValueError("memory_projection_consumer_lost")
        memory = await session.get(AgentMemory, memory_id, populate_existing=True)
        if memory is not None and memory.tenant_id != tenant_id:
            raise ValueError("memory_projection_tenant_mismatch")
        user = await session.get(User, memory.owner_id) if memory is not None else None
        active = (memory is not None and memory.status == "active" and user is not None
                  and user.tenant_id == tenant_id and user.is_active)
        if active and any(value is not None and utc(value) <= timestamp
                          for value in (memory.valid_until, memory.expires_at)):
            active = False
        vector = await session.scalar(select(MemoryVector).where(
            MemoryVector.memory_id == memory_id, MemoryVector.tenant_id == tenant_id,
            MemoryVector.owner_id == memory.owner_id, MemoryVector.memory_version == memory.version,
            MemoryVector.content_hash == content_hash(memory.content),
            MemoryVector.model_fingerprint == memory.embedding_fingerprint,
            MemoryVector.status == "ready").order_by(MemoryVector.id.desc()).limit(1)) if active else None
        row = None
        if vector is not None:
            cosine(vector.vector, vector.vector)
            if vector.dimensions != len(vector.vector):
                raise ValueError("memory_projection_vector_invalid")
            row = dict(vector_id=vector.id, memory_version=memory.version, content_hash=vector.content_hash,
                tenant_id=tenant_id, owner_id=memory.owner_id, agent_id=memory.agent_id,
                session_id=memory.session_id, session_scoped=memory.memory_type in {"short_term", "session"},
                dimensions=vector.dimensions, vector=list(vector.vector), model_fingerprint=vector.model_fingerprint)
        await session.commit()
    from eic.vector.memory_store import replace
    accepted = await replace(memory_id, generation, row)
    if accepted:
        async with sessions() as session:
            await new_id(session)
            event = await session.get(OutboxEvent, ctx["event_id"], populate_existing=True)
            if (event is None or event.consume_token != ctx["consume_token"]
                    or event.consume_status != "running" or event.consume_until is None
                    or utc(event.consume_until) <= datetime.now(UTC)):
                raise ValueError("memory_projection_consumer_lost")
            current = await session.get(AgentMemory, memory_id, populate_existing=True)
            # 清理也撤销回执；迟到提交不能删除更高代次的已确认回执。
            await session.execute(delete(MemoryProjectionReceipt).where(
                MemoryProjectionReceipt.vector_id.in_(select(MemoryVector.id).where(
                    MemoryVector.memory_id == memory_id)), MemoryProjectionReceipt.generation <= generation))
            vector = await session.get(MemoryVector, row["vector_id"]) if row else None
            previous = await session.get(MemoryProjectionReceipt, vector.id) if vector else None
            if (row is not None and previous is None and current is not None
                    and current.tenant_id == tenant_id and current.status == "active"
                    and current.version == row["memory_version"] and current.content_hash == row["content_hash"]
                    and current.embedding_fingerprint == row["model_fingerprint"] and vector is not None):
                session.add(MemoryProjectionReceipt(vector_id=vector.id, content_hash=row["content_hash"],
                    projection_version=PROJECTION, generation=generation))
            await session.commit()
    return {"status": ("projected" if row is not None else "cleaned") if accepted else "fenced"}
