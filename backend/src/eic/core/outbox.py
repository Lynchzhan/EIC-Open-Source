"""Business writes and intent share a transaction; broker I/O happens after commit."""
import asyncio
from datetime import UTC, timedelta
import logging
import uuid

from arq.worker import Retry
from sqlalchemy import and_, func, or_, select, update

from eic.core import queue, tasks
from eic.core.background_dispatch import DispatchPool
from eic.core.async_cleanup import complete_cleanup
from eic.core.config import get_settings
from eic.core.database import SessionLocal
from eic.core.ids import new_id
from eic.models import KnowledgeDocument, KnowledgeIngestionRun, OutboxEvent, Resource, ScheduledTask, TaskRun, TaskGraphLease

log = logging.getLogger(__name__)
KINDS = {"knowledge-base", "knowledge-document", "document-embeddings", "index-cleanup",
         "scheduled-task", "chat-compaction", "memory-write", "memory-extraction", "memory-index", "conversation-label", "memory-projection"}
CONSUMER_LEASE_SECONDS = 900


def _inline_kinds(settings):
    kinds = {"memory-write"}
    if ("lite" in settings.environment.lower()
            or settings.environment.lower() in {"development", "test", "testing"}
            or settings.database_url.startswith("sqlite")):
        kinds.update({"knowledge-base", "knowledge-document", "document-embeddings", "chat-compaction",
                      "memory-extraction", "memory-index", "conversation-label", "index-cleanup", "memory-projection"})
    return kinds


def _dispatch_available(timestamp, settings):
    # Inline无broker延迟队列；ARQ路径仍可提前入队并使用_defer_until。
    return and_(
        or_(OutboxEvent.kind.not_in(_inline_kinds(settings)), OutboxEvent.available_at <= timestamp),
        or_(OutboxEvent.kind != "memory-projection", getattr(settings, "memory_vector_projection_enabled", False)),
    )


def utc(value):
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


async def now(session):
    return utc(await session.scalar(select(func.current_timestamp())))


async def stage(session, kind, entity_id, tenant_id, *, event_key=None, available_at=None):
    """No commit and no provider/Redis calls. Caller owns the transaction."""
    if kind not in KINDS:
        raise ValueError("Unknown outbox event kind")
    event_id = await new_id(session)
    if event_key:
        existing = await session.scalar(select(OutboxEvent).where(
            OutboxEvent.tenant_id == tenant_id, OutboxEvent.event_key == event_key,
        ))
        if existing is not None:
            if existing.kind != kind or existing.entity_id != entity_id:
                raise ValueError("Outbox key belongs to another operation")
            return existing.id
    timestamp = await now(session)
    event = OutboxEvent(
        id=event_id, tenant_id=tenant_id, event_key=event_key or f"{kind}:{event_id}",
        kind=kind, entity_id=entity_id,
        available_at=utc(available_at) if available_at else timestamp,
        next_attempt_at=timestamp, status="pending", consume_status="pending",
        attempts=0, result={}, last_error="",
    )
    session.add(event)
    await session.flush()
    return event.id


async def stage_document(session, document_id, tenant_id):
    document = await session.get(KnowledgeDocument, document_id)
    identity = await new_id(session)
    return await stage(session, "knowledge-document", document_id, tenant_id,
                       event_key=f"document:{document_id}:{document.source_revision}:{identity}")


async def latest_document_event(document_id, tenant_id):
    async with SessionLocal() as session:
        return await session.scalar(select(OutboxEvent.id).where(
            OutboxEvent.kind == "knowledge-document", OutboxEvent.entity_id == document_id,
            OutboxEvent.tenant_id == tenant_id,
        ).order_by(OutboxEvent.id.desc()).limit(1))


async def _update_business(session, event, status, job_id):
    if event.kind == "scheduled-task":
        changed = await session.execute(update(TaskRun).where(
            TaskRun.id == event.entity_id, TaskRun.tenant_id == event.tenant_id,
            TaskRun.status.in_(["pending", "queue_unavailable", "queued"]),
        ).values(status=status, job_id=job_id))
        run = await session.get(TaskRun, event.entity_id)
        if run and changed.rowcount == 1:
            await session.execute(update(ScheduledTask).where(
                ScheduledTask.id == run.task_id, ScheduledTask.tenant_id == event.tenant_id,
                ScheduledTask.status.in_(["ready", "pending", "queue_unavailable", "queued"]),
            ).values(status=status))
    elif event.kind == "knowledge-base":
        item = await session.get(Resource, event.entity_id)
        if item and item.tenant_id == event.tenant_id and not item.is_deleted:
            data = item.data or {}
            if (data.get("indexStatus") in {"pending", "queued", "queue_unavailable"}
                    and data.get("indexEventId", str(event.id)) == str(event.id)):
                item.data = {**data, "indexStatus": status, "indexJobId": job_id}


async def _scheduled_runtime(session, event):
    """仅在调用方 allocator 事务内使用；不自行提交或换锁。"""
    from eic.core.task_graph_runtime import TaskRuntime
    run = await session.get(TaskRun, event.entity_id, populate_existing=True)
    task = await session.get(ScheduledTask, run.task_id, populate_existing=True) if run else None
    if (run is None or task is None or run.tenant_id != event.tenant_id
            or task.tenant_id != event.tenant_id):
        return None, None, None
    lease = await session.get(TaskGraphLease, run.id, populate_existing=True)
    return TaskRuntime(SessionLocal, task.id, event.tenant_id, run.id), lease, run


async def _uncertain_business(session, event):
    if event.kind in {"chat-compaction", "memory-extraction", "memory-index", "conversation-label"}:
        from eic.models import ContextJob, AgentMemory
        job = await session.get(ContextJob, event.entity_id, populate_existing=True)
        if job and job.tenant_id == event.tenant_id and job.status == "provider_done":
            # 仅重做本地提交，保留原模型结果与调用绑定；最多三次，禁止补发模型。
            retries = int((job.usage or {}).get("localCommitRetries", 0))
            if retries < 3:
                job.usage = {**(job.usage or {}), "localCommitRetries": retries + 1}
                job.lease_token, job.lease_until = None, None
                await stage(session, event.kind, event.entity_id, event.tenant_id,
                    event_key=f"context-local-commit:{job.id}:{retries + 1}",
                    available_at=await now(session) + timedelta(seconds=30))
            else:
                job.status, job.error_code = "failed", "context_local_commit_exhausted"
                if job.kind == "memory-index":
                    memory = await session.get(AgentMemory, job.payload["memoryId"])
                    if memory and memory.tenant_id == event.tenant_id and memory.version == job.payload["version"]:
                        memory.index_status = "failed"
                from eic.core.memory_lifecycle import record
                record(session, job, "memory_index" if job.kind == "memory-index" else
                       "conversation_label" if job.kind == "conversation-label" else
                       "context_compaction" if job.kind == "chat-compaction" else "memory_extraction", "failed")
            return
        if job and job.tenant_id == event.tenant_id and job.status not in {"completed", "provider_done", "failed", "cancelled", "uncertain"}:
            job.status = "uncertain" if job.status == "provider_started" else "cancelled"
            job.error_code = "context_consumer_interrupted"
            if job.kind == "memory-index":
                memory = await session.get(AgentMemory, job.payload["memoryId"])
                if memory and memory.tenant_id == event.tenant_id and memory.version == job.payload["version"]:
                    memory.index_status = job.status
            from eic.core.memory_lifecycle import record
            record(session, job, "memory_index" if job.kind == "memory-index" else
                   "conversation_label" if job.kind == "conversation-label" else
                   "context_compaction" if job.kind == "chat-compaction" else "memory_extraction", job.status)
        return
    if event.kind == "scheduled-task":
        runtime, lease, run = await _scheduled_runtime(session, event)
        timestamp = await now(session)
        # 旧消费错误不是有效 graph owner 的终止凭据，不能抢写其业务状态。
        if lease is not None:
            if lease.state in {'active', 'inflight'} and utc(lease.expires_at) > timestamp:
                return
            if runtime is not None:
                await runtime.quarantine_locked(session, lease, timestamp)
        changed = await session.execute(update(TaskRun).where(
            TaskRun.id == event.entity_id, TaskRun.tenant_id == event.tenant_id,
            TaskRun.status.in_(["pending", "queued", "queue_unavailable", "running", "retrying"]),
        ).values(status="uncertain", error_message="consumer_outcome_uncertain",
                 finished_at=func.current_timestamp()))
        run = await session.get(TaskRun, event.entity_id)
        if run and changed.rowcount == 1:
            await session.execute(update(ScheduledTask).where(
                ScheduledTask.id == run.task_id, ScheduledTask.tenant_id == event.tenant_id,
                ScheduledTask.status.in_(["pending", "queued", "queue_unavailable", "running", "retrying"]),
            ).values(status="uncertain"))
    elif event.kind in {"knowledge-document", "document-embeddings"}:
        document = await session.get(KnowledgeDocument, event.entity_id)
        if document and document.tenant_id == event.tenant_id and not document.is_deleted:
            version = document.pending_index_version
            run = await session.get(KnowledgeIngestionRun, version) if version else None
            matches = True
            if event.kind == "document-embeddings" and event.event_key.startswith("embedding:"):
                matches = str(version) == event.event_key.rsplit(":", 1)[1]
            if event.kind == "knowledge-document" and event.event_key.startswith("document:"):
                matches = str(document.source_revision) == event.event_key.split(":")[2]
            if matches and run and run.status in {"parsing", "embedding"}:
                run.status = "uncertain"
                run.lease_until = None
                document.status = document.index_status = "uncertain"
                document.index_error = {"code": "consumer_outcome_uncertain", "automaticRetry": False}
    elif event.kind == "knowledge-base":
        item = await session.get(Resource, event.entity_id)
        if item and item.tenant_id == event.tenant_id and not item.is_deleted:
            data = item.data or {}
            if data.get("indexEventId", str(event.id)) == str(event.id):
                item.data = {**data, "indexStatus": "uncertain", "automaticRetry": False}


async def dispatch_one(event_id):
    """CAS lease serializes senders. A crash before ack retries the same job identity."""
    token = uuid.uuid4().hex
    settings = get_settings()
    async with SessionLocal() as session:
        await new_id(session)
        event = await session.get(OutboxEvent, event_id)
        if event and event.kind == "scheduled-task" and not get_settings().task_execution_enabled:
            await session.rollback()
            return None
        if event and event.kind == "memory-projection" and not getattr(settings, "memory_vector_projection_enabled", False):
            await session.rollback()
            return None
        timestamp = await now(session)
        result = await session.execute(update(OutboxEvent).where(
            OutboxEvent.id == event_id,
            _dispatch_available(timestamp, settings),
            or_(OutboxEvent.status == "pending",
                (OutboxEvent.status == "sending") & (OutboxEvent.lease_until <= timestamp),
                (OutboxEvent.status == "sent") & (OutboxEvent.consume_status == "pending")
                & (OutboxEvent.next_attempt_at <= timestamp)),
        ).values(status="sending", owner_token=token,
                 lease_until=timestamp + timedelta(seconds=30),
                 attempts=OutboxEvent.attempts + 1).execution_options(synchronize_session="fetch"))
        if result.rowcount != 1:
            event = await session.get(OutboxEvent, event_id)
            job_id = event.job_id if event and event.status == "sent" else None
            await session.rollback()
            return job_id
        event = await session.get(OutboxEvent, event_id)
        await session.commit()
    job_id = None
    inline = event.kind in _inline_kinds(settings)
    try:
        if inline:
            await consume_event({}, event.id, event.tenant_id)
            job_id = f"eic-outbox:{event.tenant_id}:{event.id}"
        else:
            job_id = await asyncio.wait_for(queue.enqueue_outbox(
                event.id, event.tenant_id, available_at=utc(event.available_at)), timeout=4)
    except asyncio.CancelledError:
        raise
    except Exception:
        # 只记录稳定错误分类，避免将 provider/key 异常内容写入持久报告。
        log.error("Outbox publish failed event_id=%s", event_id)
    async with SessionLocal() as session:
        await new_id(session)
        timestamp = await now(session)
        changed = await session.execute(update(OutboxEvent).where(
            OutboxEvent.id == event_id, OutboxEvent.status == "sending",
            OutboxEvent.owner_token == token,
        ).values(
            status="sent" if job_id else "pending", job_id=job_id,
            owner_token=None, lease_until=None,
            next_attempt_at=timestamp + timedelta(seconds=min(60, 5 * event.attempts)),
            last_error="" if job_id else "publish_unavailable",
        ))
        if changed.rowcount == 1:
            await _update_business(session, event, "queued" if job_id else "queue_unavailable", job_id)
        await session.commit()
    return job_id


async def dispatch_many(event_ids):
    """并发投递持久化事件；单事件失败不阻挡同批其他文档。"""
    import logging
    semaphore = asyncio.Semaphore(10)
    async def send(event_id):
        async with semaphore:
            try:
                return await dispatch_one(event_id)
            except Exception:
                logging.getLogger(__name__).exception("outbox dispatch failed event_id=%s", event_id)
                return None
    return await asyncio.gather(*(send(value) for value in dict.fromkeys(event_ids)))


async def dispatch_pending(limit=50, *, kind=None, scheduler=None):
    if scheduler is not None:
        limit = min(limit, scheduler.available)
    if limit <= 0:
        return 0
    async with SessionLocal() as session:
        timestamp = await now(session)
        ids = list(await session.scalars(select(OutboxEvent.id).where(
            *([OutboxEvent.kind == kind] if kind else []),
            *([OutboxEvent.id.not_in(tuple(scheduler.active))] if scheduler is not None and scheduler.active else []),
            _dispatch_available(timestamp, get_settings()),
            or_((OutboxEvent.status == "pending") & (OutboxEvent.next_attempt_at <= timestamp),
                (OutboxEvent.status == "sending") & (OutboxEvent.lease_until <= timestamp),
                (OutboxEvent.status == "sent") & (OutboxEvent.consume_status == "pending")
                & (OutboxEvent.next_attempt_at <= timestamp)),
        ).order_by(OutboxEvent.id).limit(limit)))
    sent = 0
    for event_id in ids:
        if scheduler is None:
            sent += bool(await dispatch_one(event_id))
        else:
            sent += scheduler.submit(event_id, dispatch_one)
    return sent


async def _invoke(ctx, event):
    from eic import workers
    ctx = {**ctx, "event_id": event.id, "consume_token": event.consume_token}
    if event.kind == "document-embeddings" and event.event_key.startswith("embedding:"):
        ctx["index_version"] = int(event.event_key.rsplit(":", 1)[1])
    if event.kind == "knowledge-document" and event.event_key.startswith("document:"):
        ctx["source_revision"] = int(event.event_key.split(":")[2])
    if event.kind == "scheduled-task":
        async with SessionLocal() as session:
            run = await session.get(TaskRun, event.entity_id)
            task = await session.get(ScheduledTask, run.task_id) if run else None
            if (run is None or task is None or run.tenant_id != event.tenant_id
                    or task.tenant_id != event.tenant_id):
                return {"status": "cancelled"}
            task_id = task.id
        return await workers.run_scheduled_task({**ctx, 'consume_token': event.consume_token}, task_id, event.tenant_id, event.entity_id)
    if event.kind in {"knowledge-document", "document-embeddings"}:
        async with SessionLocal() as session:
            document = await session.get(KnowledgeDocument, event.entity_id)
            base = await session.get(Resource, document.knowledge_base_id) if document else None
            if (document is None or document.tenant_id != event.tenant_id or base is None
                    or base.tenant_id != event.tenant_id or base.is_deleted
                    or base.resource_type != "knowledge_bases" or document.is_deleted):
                return {"status": "missing"}
    if event.kind == "conversation-label":
        from eic.ai.conversation_labels import process
        return await process(ctx, event.entity_id, event.tenant_id, sessions=SessionLocal)
    if event.kind == "memory-projection":
        from eic.core.memory_projection import process_projection
        return await process_projection(ctx, event.entity_id, event.tenant_id, sessions=SessionLocal)
    if event.kind == "memory-index":
        from eic.core.memory_index import process_index
        return await process_index(ctx, event.entity_id, event.tenant_id, sessions=SessionLocal)
    if event.kind == "memory-extraction":
        from eic.ai.memory_candidates import process_extraction
        return await process_extraction(ctx, event.entity_id, event.tenant_id, sessions=SessionLocal)
    functions = {
        "knowledge-base": workers.process_knowledge_base,
        "knowledge-document": workers.process_knowledge_document,
        "document-embeddings": workers.process_document_embeddings,
        "chat-compaction": workers.compact_chat_history,
        "index-cleanup": workers.cleanup_document_index,
    }
    return await functions[event.kind](ctx, event.entity_id, event.tenant_id)


async def _redeliver_projection(session, event, timestamp):
    retries = (event.result or {}).get("projectionRetries", 0)
    retries = retries if type(retries) is int and 0 <= retries <= 4 else 4
    event.consume_status, event.consume_token, event.consume_until = "completed", None, None
    disabled = not getattr(get_settings(), "memory_vector_projection_enabled", False)
    if retries >= 4 and not disabled:
        event.result = {"status": "failed", "errorCode": "memory_projection_retry_exhausted",
                        "automaticRetry": False}
        return event.result
    replacement = await stage(session, event.kind, event.entity_id, event.tenant_id,
        event_key=f"projection-redelivery:{event.id}", available_at=timestamp + timedelta(seconds=30))
    pending = await session.get(OutboxEvent, replacement)
    pending.result = {"projectionRetries": retries + (0 if disabled else 1)}
    event.result = {"status": "redelivered", "replacementEventId": str(replacement),
                    "automaticRetry": True}
    return event.result


async def consume_event(ctx, event_id, tenant_id):
    """Durable receipt blocks duplicate effects even after Redis results expire."""
    async with SessionLocal() as lookup:
        candidate = await lookup.get(OutboxEvent, event_id)
        memory_write = candidate is not None and candidate.tenant_id == tenant_id and candidate.kind == "memory-write"
    if memory_write:
        from eic.core.memory_writes import consume_memory_write
        return await consume_memory_write(SessionLocal, event_id, tenant_id)
    token = uuid.uuid4().hex
    async with SessionLocal() as session:
        await new_id(session)
        timestamp = await now(session)
        seconds = 30 if candidate and candidate.kind == 'scheduled-task' else CONSUMER_LEASE_SECONDS
        changed = await session.execute(update(OutboxEvent).where(
            OutboxEvent.id == event_id, OutboxEvent.tenant_id == tenant_id,
            OutboxEvent.consume_status == "pending", OutboxEvent.available_at <= timestamp,
        ).values(consume_status="running", consume_token=token,
                 consume_until=timestamp + timedelta(seconds=seconds)))
        event = await session.get(OutboxEvent, event_id)
        if changed.rowcount != 1:
            response = (event.result or {"status": event.consume_status}
                        if event and event.tenant_id == tenant_id else {"status": "missing"})
            await session.rollback()
            return response
        await session.commit()
    heartbeat = asyncio.create_task(_renew_consumer(event_id, token, seconds))
    try:
        try:
            result = await _invoke(ctx, event)
        except BaseException as exc:
            if event.kind == "memory-projection":
                async with SessionLocal() as session:
                    await new_id(session)
                    stored = await session.get(OutboxEvent, event_id, populate_existing=True)
                    if stored is not None and stored.consume_status == "running" and stored.consume_token == token:
                        response = await _redeliver_projection(session, stored, await now(session))
                    else:
                        response = stored.result if stored else {"status": "missing"}
                    await session.commit()
                if isinstance(exc, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
                    raise
                return response
            if event.kind == "index-cleanup":
                async with SessionLocal() as session:
                    await new_id(session)
                    await session.execute(update(OutboxEvent).where(
                        OutboxEvent.id == event_id, OutboxEvent.consume_token == token,
                    ).values(consume_status="pending", consume_token=None, consume_until=None,
                             next_attempt_at=await now(session) + timedelta(seconds=30), last_error="cleanup_retry"))
                    await session.commit()
                raise Retry(defer=30)
            # 已开始的外部副作用可能不确定；不通过重投递再次调用 provider。
            async with SessionLocal() as session:
                await new_id(session)
                changed = await session.execute(update(OutboxEvent).where(
                    OutboxEvent.id == event_id, OutboxEvent.consume_status == "running",
                    OutboxEvent.consume_token == token,
                ).values(consume_status="uncertain", consume_token=None, consume_until=None,
                         last_error="consumer_interrupted",
                         result={"status": "uncertain", "automaticRetry": False}))
                if changed.rowcount == 1:
                    await _uncertain_business(session, event)
                await session.commit()
            raise
    finally:
        heartbeat.cancel()
        await asyncio.gather(heartbeat, return_exceptions=True)
    if isinstance(result, dict) and (result.get("status") == "locked"
            or event.kind == "scheduled-task" and (result.get("status") in {'already_claimed', 'fenced'}
            or result.get("status") == "retrying" and result.get("safeToRetry") is True)):
        # 资源锁尚未取得，尚无副作用；此类重试不同于不确定的 provider 重发。
        async with SessionLocal() as session:
            await new_id(session)
            if event.kind == 'scheduled-task':
                runtime, lease, run = await _scheduled_runtime(session, event)
                timestamp = await now(session)
                stored = await session.get(OutboxEvent, event_id, populate_existing=True)
                if stored is None or stored.consume_status != 'running' or stored.consume_token != token:
                    return stored.result if stored else {'status': 'missing'}
                decision = (await runtime.recovery_gate_locked(session, lease, timestamp)
                            if runtime is not None else 'quarantine')
                if decision == 'wait':
                    # 不续租、不清 token、不投递；过期 scanner 再处理。
                    await session.commit()
                    return {'status': 'already_claimed', 'safeToRetry': False}
                if run is not None and run.status in tasks.TERMINAL:
                    stored.consume_status, stored.consume_token, stored.consume_until = 'completed', None, None
                    stored.result = {'status': run.status, 'safeToRetry': False}
                    await session.commit()
                    return stored.result
                if decision != 'known':
                    if lease is not None and lease.state == 'active' and utc(lease.expires_at) > timestamp:
                        await session.commit()
                        return {'status': 'already_claimed', 'safeToRetry': False}
                    await _uncertain_business(session, event)
                    stored.consume_status, stored.consume_token, stored.consume_until = 'uncertain', None, None
                    stored.result = {'status': 'uncertain', 'automaticRetry': False}
                    await session.commit()
                    return stored.result
            changed = await session.execute(update(OutboxEvent).where(
                OutboxEvent.id == event_id, OutboxEvent.consume_status == "running",
                OutboxEvent.consume_token == token,
            ).values(consume_status="pending", consume_token=None, consume_until=None))
            stored = await session.get(OutboxEvent, event_id)
            response = stored.result if stored else {"status": "missing"}
            await session.commit()
        if changed.rowcount != 1:
            return response
        raise Retry(defer=5)
    async with SessionLocal() as session:
        await new_id(session)
        changed = await session.execute(update(OutboxEvent).where(
            OutboxEvent.id == event_id, OutboxEvent.consume_status == "running",
            OutboxEvent.consume_token == token,
            OutboxEvent.consume_until > await now(session),
        ).values(consume_status="completed", consume_token=None, consume_until=None, result=result))
        if changed.rowcount != 1:
            stored = await session.get(OutboxEvent, event_id)
            result = stored.result if stored else {"status": "missing"}
        await session.commit()
    return result


async def _renew_consumer(event_id, token, seconds=None):
    seconds = seconds or CONSUMER_LEASE_SECONDS
    while True:
        await asyncio.sleep(max(0.1, seconds / 3))
        try:
            async with SessionLocal() as session:
                await new_id(session)
                timestamp = await now(session)
                changed = await session.execute(update(OutboxEvent).where(
                    OutboxEvent.id == event_id, OutboxEvent.consume_status == "running",
                    OutboxEvent.consume_token == token, OutboxEvent.consume_until > timestamp,
                ).values(consume_until=timestamp + timedelta(seconds=seconds)))
                await session.commit()
                if changed.rowcount != 1:
                    return
        except asyncio.CancelledError:
            raise
        except Exception:
            log.error("Outbox consumer renewal failed event_id=%s", event_id)


async def recover_consumers(*, kind=None):
    async with SessionLocal() as session:
        timestamp = await now(session)
        has_expired = await session.scalar(select(OutboxEvent.id).where(
            *([OutboxEvent.kind == kind] if kind else []),
            OutboxEvent.consume_status == "running", OutboxEvent.consume_until <= timestamp,
        ).limit(1))
    if has_expired is None:
        return 0
    async with SessionLocal() as session:
        await new_id(session)
        timestamp = await now(session)
        expired = list(await session.scalars(select(OutboxEvent).where(
            *([OutboxEvent.kind == kind] if kind else []),
            OutboxEvent.consume_status == "running", OutboxEvent.consume_until <= timestamp,
        ).order_by(OutboxEvent.id).limit(50)))
        for event in expired:
            if event.kind == "memory-projection":
                await _redeliver_projection(session, event, timestamp)
                continue
            if event.kind in {"chat-compaction", "memory-extraction", "memory-index", "conversation-label"}:
                from eic.models import ContextJob
                job = await session.get(ContextJob, event.entity_id, populate_existing=True)
                if job and job.tenant_id == event.tenant_id and job.status in {"completed", "failed", "cancelled", "uncertain"}:
                    event.consume_status, event.consume_token, event.consume_until = "completed", None, None
                    event.result = {"status": job.status, "automaticRetry": False}
                    continue
                if job and job.tenant_id == event.tenant_id and job.status in {"pending", "running", "provider_done"}:
                    if job.lease_until and utc(job.lease_until) > timestamp:
                        continue
                    replacement = await stage(session, event.kind, event.entity_id, event.tenant_id,
                        event_key=f"context-redelivery:{event.id}")
                    event.consume_status, event.consume_token, event.consume_until = "completed", None, None
                    event.result = {"status": "redelivered", "replacementEventId": str(replacement)}
                    continue
            if event.kind == "scheduled-task":
                if not get_settings().task_execution_enabled:
                    continue
                from eic.core.task_graph_runtime import TaskRuntime
                run = await session.get(TaskRun, event.entity_id)
                task = await session.get(ScheduledTask, run.task_id) if run else None
                lease = await session.get(TaskGraphLease, run.id) if run else None
                recoverable = False
                if run and task and run.tenant_id == event.tenant_id and task.tenant_id == event.tenant_id:
                    if run.status in tasks.TERMINAL:
                        event.consume_status, event.consume_token, event.consume_until = 'completed', None, None
                        event.result = {'status': run.status}
                        continue
                    if (not task.enabled or task.deleted_at is not None
                            or task.schedule_revision != run.schedule_revision):
                        await tasks.cancel_runs(session, task, run_id=run.id, reason='task_disabled_or_schedule_replaced')
                        continue
                    if not await tasks.owner_authorized(session, task):
                        run.status, run.error_message, run.finished_at = 'failed', 'permission_revoked', timestamp
                        event.consume_status, event.consume_token, event.consume_until = 'completed', None, None
                        event.result = {'status': 'failed'}
                        continue
                    if lease and run.status in {'running', 'retrying'}:
                        runtime = TaskRuntime(SessionLocal, task.id, event.tenant_id, run.id)
                        decision = await runtime.recovery_gate_locked(session, lease, timestamp)
                        if decision == 'wait':
                            continue
                        recoverable = decision == 'known'
                        if not recoverable:
                            await runtime.quarantine_locked(session, lease, timestamp)
                event.consume_token, event.consume_until = None, None
                if recoverable:
                    replacement = await stage(session, 'scheduled-task', event.entity_id, event.tenant_id,
                        event_key=f'task-redelivery-event:{event.id}')
                    # 新持久事件获得新 ARQ job identity，不受旧任务 result/in-progress key 阻挡。
                    event.consume_status = "completed"
                    event.last_error = "consumer_lease_expired_redelivery"
                    event.result = {"status": "redelivered", "automaticRetry": True,
                                    "replacementEventId": str(replacement)}
                    continue
            event.consume_status, event.consume_token, event.consume_until = "uncertain", None, None
            event.last_error = "consumer_lease_expired"
            event.result = {"status": "uncertain", "automaticRetry": False}
            await _uncertain_business(session, event)
        await session.commit()
        return len(expired)


async def recover_task_runs():
    """scheduled worker 直达入口的孤立 running 也由数据库扫描投递。"""
    if not get_settings().task_execution_enabled:
        return 0
    from eic.core.task_graph_runtime import TaskRuntime
    async with SessionLocal() as session:
        await new_id(session)
        timestamp = await now(session)
        runs = list(await session.scalars(select(TaskRun).join(TaskGraphLease,
            TaskGraphLease.run_id == TaskRun.id).where(TaskRun.status == 'running',
            TaskGraphLease.expires_at <= timestamp).order_by(TaskRun.id).limit(50)))
        staged = 0
        for run in runs:
            existing = await session.scalar(select(OutboxEvent.id).where(
                OutboxEvent.kind == 'scheduled-task', OutboxEvent.entity_id == run.id,
                OutboxEvent.tenant_id == run.tenant_id,
                OutboxEvent.consume_status.in_(['pending', 'running'])).limit(1))
            if existing is not None:
                continue
            task = await session.get(ScheduledTask, run.task_id)
            if not task or task.tenant_id != run.tenant_id:
                continue
            if not task.enabled or task.deleted_at is not None or task.schedule_revision != run.schedule_revision:
                await tasks.cancel_runs(session, task, run_id=run.id, reason='task_disabled_or_schedule_replaced')
                continue
            if not await tasks.owner_authorized(session, task):
                run.status, run.error_message, run.finished_at = 'failed', 'permission_revoked', timestamp
                continue
            lease = await session.get(TaskGraphLease, run.id)
            runtime = TaskRuntime(SessionLocal, task.id, run.tenant_id, run.id)
            decision = await runtime.recovery_gate_locked(session, lease, timestamp)
            if decision == 'wait':
                continue
            if decision != 'known':
                await runtime.quarantine_locked(session, lease, timestamp)
                continue
            await stage(session, 'scheduled-task', run.id, run.tenant_id,
                event_key=f'task-redelivery:{run.id}:{lease.fencing_version}')
            staged += 1
        await session.commit()
        return staged


async def dispatch_loop():
    kinds = tuple(dict.fromkeys(get_settings().outbox_dispatch_kinds))
    scheduler = DispatchPool(getattr(get_settings(), "outbox_inline_concurrency", 2))
    offset = 0
    try:
        while True:
            try:
                if kinds:
                    for kind in kinds[offset:] + kinds[:offset]:
                        await dispatch_pending(kind=kind, scheduler=scheduler)
                        await recover_consumers(kind=kind)
                    offset = (offset + 1) % len(kinds)
                else:
                    await dispatch_pending(scheduler=scheduler)
                    await recover_consumers()
                if not kinds or "scheduled-task" in kinds:
                    await recover_task_runs()
                if not kinds or {"knowledge-document", "document-embeddings"}.intersection(kinds):
                    from eic.knowledge.indexing import recover_index_runs
                    await recover_index_runs()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.error("Outbox background scan failed")
            await asyncio.sleep(3)
    finally:
        await complete_cleanup(scheduler.close())
