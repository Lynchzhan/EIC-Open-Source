"""索引仅产生候选；正文和赢家始终回关系库验证。"""
import math
from sqlalchemy import func, select

from eic.ai.memory_math import content_hash, keywords
from eic.core.memory_search import PROJECTION, TOKENIZER, missing_terms, term_hash
from eic.models import AgentMemory, MemoryProjectionReceipt, MemorySearchDocument, MemorySearchTerm, MemoryVector


async def indexed_candidates(session, ranked, *, query, query_vector, fingerprint,
                             tenant_id, owner_id, agent_id, session_id, metadata):
    winners = select(ranked.c.id).where(ranked.c.position == 1)
    if await missing_terms(session, winners) is not None:
        metadata["indexFallback"] = "memory_terms_backfill_required"
        return None
    terms = keywords(query)
    if len(terms) > 512:
        metadata["indexFallback"] = "memory_query_terms_budget"
        return None
    sparse, dense = [], []
    if terms:
        score = func.count(MemorySearchTerm.term_hash) * 1.0 / len(terms)
        rows = await session.execute(select(AgentMemory.id, score.label("score")).join(
            MemorySearchDocument, MemorySearchDocument.memory_id == AgentMemory.id).join(
            MemorySearchTerm, MemorySearchTerm.memory_id == AgentMemory.id).where(
                AgentMemory.id.in_(winners), MemorySearchDocument.memory_version == AgentMemory.version,
                MemorySearchDocument.content_hash == AgentMemory.content_hash,
                MemorySearchDocument.tokenizer_version == TOKENIZER,
                MemorySearchTerm.term_hash.in_([term_hash(term) for term in terms])
            ).group_by(AgentMemory.id).order_by(score.desc(), AgentMemory.id.desc()).limit(60))
        sparse = [(float(value), identity) for identity, value in rows]
    profiles = list(await session.scalars(select(AgentMemory.id).where(
        AgentMemory.id.in_(winners), AgentMemory.memory_type == "profile").order_by(AgentMemory.id.desc()).limit(60)))
    sparse_scores = dict((identity, score) for score, identity in sparse)
    for identity in profiles:
        sparse_scores[identity] = max(sparse_scores.get(identity, 0), 0.1)
    sparse = sorted(((score, identity) for identity, score in sparse_scores.items()), reverse=True)[:60]
    if query_vector is not None:
        # 未迁移的存量向量继续使用有界旧路径，避免无声丢失语义召回。
        valid_receipts = select(MemoryProjectionReceipt.vector_id).where(
            MemoryProjectionReceipt.projection_version == PROJECTION,
            MemoryProjectionReceipt.content_hash == MemoryVector.content_hash)
        missing = await session.scalar(select(MemoryVector.id).join(AgentMemory,
            AgentMemory.id == MemoryVector.memory_id).where(
                AgentMemory.id.in_(winners), MemoryVector.tenant_id == tenant_id,
                MemoryVector.owner_id == owner_id, MemoryVector.memory_version == AgentMemory.version,
                MemoryVector.content_hash == AgentMemory.content_hash, MemoryVector.status == "ready",
                MemoryVector.model_fingerprint == fingerprint,
                MemoryVector.id.not_in(valid_receipts)).limit(1))
        from eic.core.config import get_settings
        if missing is not None or not getattr(get_settings(), "memory_vector_projection_enabled", False):
            metadata["indexFallback"] = "memory_projection_backfill_required"
            return None
        try:
            from eic.vector.memory_store import candidates
            hits = await candidates(query_vector, tenant_id=tenant_id, owner_id=owner_id,
                agent_id=agent_id, session_id=session_id, fingerprint=fingerprint)
            # 命中 ID 也不可信：与权威向量的 ID/版本/hash/model 及当前赢家作交集。
            rows = await session.execute(select(MemoryVector.id, MemoryVector.memory_id,
                MemoryVector.memory_version, MemoryVector.content_hash).join(
                AgentMemory, AgentMemory.id == MemoryVector.memory_id).where(
                    AgentMemory.id.in_(winners), MemoryVector.id.in_([hit["vector_id"] for hit in hits]),
                    MemoryVector.tenant_id == tenant_id, MemoryVector.owner_id == owner_id,
                    MemoryVector.memory_version == AgentMemory.version,
                    MemoryVector.content_hash == AgentMemory.content_hash,
                    MemoryVector.model_fingerprint == fingerprint, MemoryVector.status == "ready"))
            current = {row.id: row for row in rows}
            for hit in hits:
                row = current.get(hit["vector_id"])
                score = hit["score"]
                if (row is not None and row.memory_id == hit["memory_id"]
                        and row.memory_version == hit["memory_version"] and row.content_hash == hit["content_hash"]
                        and isinstance(score, (int, float)) and math.isfinite(score) and score > 0):
                    dense.append((score, row.memory_id))
            dense = sorted(dense, reverse=True)[:60]
            metadata["vectorCandidateCount"] = len(hits)
        except Exception:
            metadata["indexFallback"] = "memory_projection_unavailable"
            return None
    identities = {identity for _, identity in sparse + dense}
    rows = (await session.execute(select(AgentMemory, ranked.c.conflicts).join(
        ranked, ranked.c.id == AgentMemory.id).where(ranked.c.position == 1, AgentMemory.id.in_(identities)))).all()
    facts = {}
    for memory, conflicts in rows:
        facts[memory.id] = {"id": memory.id, "content": memory.content, "version": memory.version,
            "memoryType": memory.memory_type, "hash": content_hash(memory.content),
            "sourceMessageId": memory.source_message_id, "conflictCount": conflicts,
            "lowConfidenceConflict": conflicts > 1 and memory.confidence < 0.5}
    metadata.update(candidateSource="persistent_index", candidateCount=len(facts))
    return sparse, dense, facts
