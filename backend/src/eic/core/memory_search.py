"""记忆的事务词项索引及无模型调用的投影意图。"""
import hashlib
from sqlalchemy import delete, insert, select

from eic.ai.memory_math import keywords, content_hash
from eic.models import MemorySearchDocument, MemorySearchTerm

TOKENIZER = "memory-keywords-v1"
PROJECTION = "memory-pg-v1"


def term_hash(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


async def remove_terms(session, memory_id):
    await session.execute(delete(MemorySearchTerm).where(MemorySearchTerm.memory_id == memory_id))
    await session.execute(delete(MemorySearchDocument).where(MemorySearchDocument.memory_id == memory_id))


async def index_terms(session, memory):
    """调用者持 allocator 锁；未来生效的 active 也可建索引，读取时再核有效期。"""
    memory.content_hash = content_hash(memory.content)
    await session.flush()
    previous = await session.get(MemorySearchDocument, memory.id)
    digest = content_hash(memory.content)
    if (previous and previous.memory_version == memory.version and previous.content_hash == digest
            and previous.tokenizer_version == TOKENIZER and memory.status == "active"):
        return
    await remove_terms(session, memory.id)
    if memory.status != "active":
        return
    await session.execute(insert(MemorySearchDocument).values(memory_id=memory.id,
        memory_version=memory.version, content_hash=digest, tokenizer_version=TOKENIZER))
    terms = [dict(memory_id=memory.id, term_hash=term_hash(term)) for term in sorted(keywords(memory.content))]
    for offset in range(0, len(terms), 500):
        await session.execute(insert(MemorySearchTerm), terms[offset:offset + 500])


async def stage_projection(session, memory):
    from eic.core.config import get_settings
    from eic.core.outbox import stage
    if not getattr(get_settings(), "memory_vector_projection_enabled", False):
        return None
    return await stage(session, "memory-projection", memory.id, memory.tenant_id)


async def stage_cleanup(session, memory):
    await remove_terms(session, memory.id)
    from eic.core.config import get_settings
    from eic.core.outbox import stage
    from eic.models import MemoryProjectionReceipt, MemoryVector, OutboxEvent
    if not getattr(get_settings(), "memory_vector_projection_enabled", False):
        previous = await session.scalar(select(OutboxEvent.id).where(
            OutboxEvent.tenant_id == memory.tenant_id, OutboxEvent.kind == "memory-projection",
            OutboxEvent.entity_id == memory.id).limit(1))
        receipt = await session.scalar(select(MemoryProjectionReceipt.vector_id).where(
            MemoryProjectionReceipt.vector_id.in_(select(MemoryVector.id).where(
                MemoryVector.memory_id == memory.id))).limit(1)) if previous is None else None
        if previous is None and receipt is None:
            return None
    # 曾投影的数据即使关闭投影也保留清理意图，关闭期间不调度 PG I/O。
    return await stage(session, "memory-projection", memory.id, memory.tenant_id)


async def missing_terms(session, scoped_ids):
    from eic.models import AgentMemory
    valid = select(MemorySearchDocument.memory_id).join(AgentMemory,
        AgentMemory.id == MemorySearchDocument.memory_id).where(
            MemorySearchDocument.memory_version == AgentMemory.version,
            MemorySearchDocument.content_hash == AgentMemory.content_hash,
            MemorySearchDocument.tokenizer_version == TOKENIZER)
    return await session.scalar(select(AgentMemory.id).where(
        AgentMemory.id.in_(scoped_ids), AgentMemory.id.not_in(valid)).limit(1))
