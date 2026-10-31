"""可变维度独立向量索引；先持久标记外呼，未知结果禁止自动重放。"""
from eic.core.authentication import Principal
from eic.core.authorization import ensure_permission
from eic.ai.app_config import runtime_app_config
from eic.core.chat_ownership import require_owned_chat_session
import hashlib
from datetime import UTC, datetime
import uuid
from sqlalchemy import select

from eic.ai.context_policy import policy_hash
from eic.ai.embeddings import embed_texts
from eic.ai.memory_math import cosine
from eic.ai.runtime_policy import ACTIVE, DEFAULTS, normalize_policy
from eic.core import observability
from eic.core.context_jobs import _BackgroundAccount
from eic.core.ids import new_id
from eic.core.langfuse_context import turn_context
from eic.core.security import _fernet
from eic.models import AgentMemory, AIModel, ContextJob, MemoryVector, ObservabilityEvent, OutboxEvent, User


def fingerprint(model):
    return policy_hash(["memory-embedding-v1", *[getattr(model, key, None) for key in (
        "id", "tenant_id", "provider", "model_name", "endpoint", "embedding_dimensions",
        "secret_encrypted")]])


async def _checked(session, ctx, job_id, tenant_id):
    from eic.core.outbox import now, utc
    await new_id(session)
    event = await session.scalar(select(OutboxEvent).where(OutboxEvent.id == ctx["event_id"]).with_for_update())
    job = await session.scalar(select(ContextJob).where(ContextJob.id == job_id).with_for_update())
    timestamp = await now(session)
    if (job is None or event is None or job.tenant_id != tenant_id or job.kind != "memory-index"
            or event.tenant_id != tenant_id or event.kind != job.kind or event.entity_id != job.id
            or event.consume_token != ctx["consume_token"] or event.consume_status != "running"
            or event.consume_until is None or utc(event.consume_until) <= timestamp):
        raise ValueError("memory_index_consumer_lost")
    memory = await session.get(AgentMemory, job.payload["memoryId"], populate_existing=True)
    user = await session.get(User, job.owner_id, populate_existing=True)
    model = await session.get(AIModel, job.payload["modelId"], populate_existing=True)
    if (memory is None or memory.tenant_id != tenant_id or memory.owner_id != job.owner_id
            or job.session_id is None
            or memory.status != "active" or memory.version != job.payload["version"]
            or hashlib.sha256(memory.content.encode("utf-8")).hexdigest() != job.source_hash
            or user is None or user.tenant_id != tenant_id or not user.is_active
            or model is None or model.tenant_id != tenant_id or not model.enabled
            or model.model_type != "embedding" or fingerprint(model) != job.payload["fingerprint"]):
        raise ValueError("memory_index_source_changed")
    if (any(value is not None and utc(value) <= timestamp for value in (memory.valid_until, memory.expires_at))
            or memory.valid_from is not None and utc(memory.valid_from) > timestamp):
        raise ValueError("memory_index_outside_validity")
    principal = Principal(user.id, tenant_id, user.username, user.is_super_admin)
    await ensure_permission("memory.write", principal, session)
    config = await runtime_app_config(session, tenant_id, memory.agent_id) if memory.agent_id else {}
    chat = await require_owned_chat_session(session, principal, job.session_id)
    if chat.app_id != memory.agent_id:
        raise ValueError("memory_index_session_changed")
    config = await runtime_app_config(session, tenant_id, chat.app_id)
    from eic.ai.context_policy import memory_settings
    if memory.agent_id and memory_settings(config)["mode"] == "off":
        raise ValueError("memory_index_disabled")
    if config.get("runtimePolicy") != job.payload.get("runtimePolicy"):
        raise ValueError("memory_index_policy_changed")
    # active 已由用户明确保存或确认；其生命周期独立于原聊天的保留期限。
    return job, memory, model


def _observation_start(job):
    data = (job.usage or {}).get("_observation", {})
    try:
        value = datetime.fromisoformat(data["started_at"])
        return value.astimezone(UTC) if value.tzinfo is not None else None
    except (KeyError, TypeError, ValueError):
        return None


async def _stage_lifecycle(session, job, *, recovered=False):
    """终态与业务同事务提交；持久收据允许重投补发相同事件。"""
    event_key = f"memory-lifecycle:{job.tenant_id}:{job.id}:{job.status}"
    existing = await session.get(ObservabilityEvent, event_key)
    if existing is not None:
        observability.stage(session, {column.name: getattr(existing, column.name)
            for column in ObservabilityEvent.__table__.columns if column.name != "collected_at"})
        return existing.stage == "memory_index"
    usage = dict(job.usage or {})
    data = dict(usage.get("_observation") or {})
    start = _observation_start(job)
    if data.get("terminal_status") != job.status or not data.get("finished_at"):
        data.update(finished_at=datetime.now(UTC).isoformat(), terminal_status=job.status,
                    recovered=recovered)
        usage["_observation"] = data
        job.usage = usage
    end = datetime.fromisoformat(data["finished_at"]).astimezone(UTC)
    measured = start is not None and end >= start
    request = f"memory-index:{job.id}"
    scope = turn_context(tenant_id=job.tenant_id, request_id=request, run_id=request,
        chat_id=str(job.session_id), owner_key=observability.actor_key(job.tenant_id, job.owner_id))
    token = observability.CONTEXT.set(scope)
    try:
        row = observability.observation(
            "agent_run", "memory_index" if measured else "memory_index_recovery", job.status,
            source="memory_pipeline", event_key=event_key,
            span_id=scope["span_id"] if measured else observability.turn_span(job.tenant_id, request + ":recovery"),
            parent_span_id="", occurred_at=start if measured else end,
            duration_ms=(end - start).total_seconds() * 1000 if measured else None,
            error_code=("memory_index_lifecycle_recovered" if data.get("recovered") else None)
                       if measured else "memory_index_start_missing")
        if row is not None:
            # SQL 行与业务终态原子落盘；stage 仅用于提交后的尽力导出。
            session.add(ObservabilityEvent(**row))
            observability.stage(session, row)
    finally:
        observability.CONTEXT.reset(token)
    return measured


async def process_index(ctx, job_id, tenant_id, *, sessions):
    token = uuid.uuid4().hex
    async with sessions() as session:
        job, memory, model = await _checked(session, ctx, job_id, tenant_id)
        if job.status == "completed":
            await _stage_lifecycle(session, job)
            await session.commit()
            return {"status": "completed"}
        if job.status in {"uncertain", "failed", "cancelled"}:
            await _stage_lifecycle(session, job)
            await session.commit()
            return {"status": job.status, "automaticRetry": False}
        if job.status == "provider_started":
            job.status, memory.index_status = "uncertain", "uncertain"
            await _stage_lifecycle(session, job, recovered=True)
            await session.commit()
            return {"status": "uncertain", "automaticRetry": False}
        if job.status == "provider_done":
            import json
            vector = json.loads(_fernet().decrypt(job.result_encrypted.encode("ascii")))
        else:
            vector = None
            # 与外呼栅栏同事务保存，绝不从 created_at 推算历史开始时间。
            usage = dict(job.usage or {})
            usage["_observation"] = {"started_at": datetime.now(UTC).isoformat()}
            job.usage = usage
            job.status, job.lease_token = "provider_started", token
            memory.index_status = "running"
        text_value, payload, owner_id, session_id = memory.content, dict(job.payload), job.owner_id, job.session_id
        await session.commit()
    observation = observability.CONTEXT.set(turn_context(tenant_id=tenant_id,
        request_id=f"memory-index:{job_id}", run_id=f"memory-index:{job_id}",
        chat_id=str(session_id) if session_id else None, owner_key=observability.actor_key(tenant_id, owner_id)))
    account = _BackgroundAccount(sessions, tenant_id, owner_id, session_id,
        normalize_policy(payload.get("runtimePolicy") or DEFAULTS)) if session_id else None
    budget_token = ACTIVE.set(account)
    try:
        if vector is None:
            vectors = await embed_texts(model, [text_value], tenant_id=tenant_id)
            if len(vectors) != 1:
                raise ValueError("memory_index_count_invalid")
            vector = vectors[0]
            cosine(vector, vector)
            async with sessions() as session:
                job, memory, _ = await _checked(session, ctx, job_id, tenant_id)
                if job.status != "provider_started" or job.lease_token != token:
                    raise ValueError("memory_index_owner_lost")
                import json
                job.result_encrypted = _fernet().encrypt(json.dumps(vector, allow_nan=False).encode()).decode("ascii")
                job.status = "provider_done"
                await session.commit()
        async with sessions() as session:
            job, memory, _ = await _checked(session, ctx, job_id, tenant_id)
            if job.status != "provider_done":
                raise ValueError("memory_index_result_not_ready")
            cosine(vector, vector)
            session.add(MemoryVector(id=await new_id(session), tenant_id=tenant_id, owner_id=owner_id,
                memory_id=memory.id, memory_version=memory.version, content_hash=job.source_hash,
                model_fingerprint=payload["fingerprint"], dimensions=len(vector), vector=vector, status="ready"))
            job.status, memory.index_status = "completed", "ready"
            memory.embedding_fingerprint = payload["fingerprint"]
            from eic.core.memory_search import stage_projection
            await stage_projection(session, memory)
            measured = await _stage_lifecycle(session, job, recovered=vector is not None and token != job.lease_token)
            if measured:
                from eic.core.memory_lifecycle import record
                record(session, job, "memory_index", "completed", resultCount=1, indexedDimensions=len(vector))
            await session.commit()
        return {"status": "completed", "memoryId": str(payload["memoryId"])}
    except BaseException:
        async with sessions() as session:
            try:
                job, memory, _ = await _checked(session, ctx, job_id, tenant_id)
                if job.status not in {"provider_done", "completed"}:
                    job.status, memory.index_status = "uncertain", "uncertain"
                    job.error_code = "memory_index_outcome_unknown"
                    await _stage_lifecycle(session, job)
                await session.commit()
            except Exception:
                await session.rollback()
        raise
    finally:
        ACTIVE.reset(budget_token)
        observability.CONTEXT.reset(observation)
