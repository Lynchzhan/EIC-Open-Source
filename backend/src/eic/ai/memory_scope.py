"""记忆召回与快照复核共享的授权和冲突优先级。"""
from sqlalchemy import or_
from eic.models import AgentMemory


def memory_scope(tenant_id, owner_id, session_id, agent_id, now):
    return [AgentMemory.tenant_id == tenant_id, AgentMemory.owner_id == owner_id,
        AgentMemory.agent_id == agent_id, AgentMemory.status == "active",
        or_(AgentMemory.valid_from.is_(None), AgentMemory.valid_from <= now),
        or_(AgentMemory.valid_until.is_(None), AgentMemory.valid_until > now),
        or_(AgentMemory.expires_at.is_(None), AgentMemory.expires_at > now),
        or_(AgentMemory.memory_type.notin_(["short_term", "session"]), AgentMemory.session_id == session_id)]


def memory_ordering():
    return (AgentMemory.source_priority.desc(), AgentMemory.valid_from.desc(),
        AgentMemory.confidence.desc(), AgentMemory.importance.desc(),
        AgentMemory.created_at.desc(), AgentMemory.id.desc())
