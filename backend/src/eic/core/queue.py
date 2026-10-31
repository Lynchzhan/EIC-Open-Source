from __future__ import annotations

from datetime import timedelta
import asyncio
import json

from arq import create_pool
from arq.connections import ArqRedis, RedisSettings
from arq.jobs import Job, JobStatus
from redis.exceptions import RedisError

from eic.core.config import get_settings

_pool: ArqRedis | None = None


async def task_capabilities() -> dict:
    """只有 Redis 实测与同协议 worker 的短租约均有效时才允许执行。"""
    from eic.core.tasks import CONTRACT, HANDLERS, worker_scope
    settings = get_settings()
    result = {"configured": settings.task_execution_enabled,
              "redis": False, "worker": False, "handlerTypes": list(HANDLERS),
              "agentExecution": False, "execution": False, "scheduling": False,
              "reason": "task_execution_disabled", "contract": CONTRACT}
    if not settings.task_execution_enabled:
        return result
    async def probe():
        pool = await get_queue()
        result["redis"] = bool(await pool.ping())
        count = 0
        async for key in pool.scan_iter(match="eic:tasks:worker:*", count=20):
            count += 1
            if count > 100:
                break
            raw = await pool.get(key)
            try:
                heartbeat = json.loads(raw or "{}")
                if (isinstance(heartbeat, dict) and heartbeat.get("contract") == CONTRACT
                        and heartbeat.get("scope") == worker_scope()
                        and isinstance(heartbeat.get("handlers"), list)
                        and set(HANDLERS).issubset(heartbeat["handlers"])):
                    result["worker"] = True
                    break
            except (ValueError, TypeError):
                continue
    try:
        await asyncio.wait_for(probe(), timeout=2)
    except (TimeoutError, OSError, RedisError, ValueError):
        pass
    result["execution"] = result["redis"] and result["worker"] and settings.outbox_dispatch_enabled
    result["scheduling"] = result["execution"]
    result["agentExecution"] = result["execution"] and "agent" in HANDLERS
    result["reason"] = ("" if result["execution"] else
                        "redis_unavailable" if not result["redis"] else
                        "worker_unavailable" if not result["worker"] else "outbox_disabled")
    return result


async def get_queue() -> ArqRedis:
    global _pool
    if _pool is None:
        _pool = await create_pool(RedisSettings.from_dsn(get_settings().redis_url))
    return _pool


async def close_queue() -> None:
    global _pool
    if _pool is not None:
        await _pool.aclose()
        _pool = None


async def enqueue_outbox(event_id: int, tenant_id: int, *, available_at=None) -> str | None:
    """Stable identity also covers publish-success/DB-ack failure."""
    from eic.core.dependency_controls import DependencyDenied, guard
    job_id = f"eic-outbox:{tenant_id}:{event_id}"
    try:
        async with guard("redis", tenant_id, endpoint=get_settings().redis_url):
            async with asyncio.timeout(3):
                queue = await get_queue()
                job = await queue.enqueue_job(
                    "consume_outbox_event", event_id, tenant_id,
                    _job_id=job_id, _defer_until=available_at,
                )
                if job is not None:
                    return job.job_id
                # 重复发送仍绑定相同 job identity。
                return job_id if await Job(job_id, redis=queue).status() != JobStatus.not_found else None
    except (OSError, RedisError, TimeoutError, DependencyDenied):
        return None


async def enqueue_knowledge_base(knowledge_base_id: int, tenant_id: int) -> str | None:
    """Enqueue a tenant-bound indexing job, returning None when Redis is unavailable."""
    try:
        queue = await get_queue()
        job = await queue.enqueue_job("process_knowledge_base", knowledge_base_id, tenant_id)
        return job.job_id if job is not None else None
    except (OSError, RedisError):
        return None


async def enqueue_knowledge_document(document_id: int, tenant_id: int) -> str | None:
    try:
        queue = await get_queue()
        job = await queue.enqueue_job("process_knowledge_document", document_id, tenant_id)
        return job.job_id if job is not None else None
    except (OSError, RedisError):
        return None


async def enqueue_document_embeddings(document_id: int, tenant_id: int) -> str | None:
    try:
        queue = await get_queue()
        job = await queue.enqueue_job("process_document_embeddings", document_id, tenant_id)
        return job.job_id if job is not None else None
    except (OSError, RedisError):
        return None


async def enqueue_chat_compaction(chat_id: int, tenant_id: int) -> str | None:
    try:
        queue = await get_queue()
        job = await queue.enqueue_job("compact_chat_history", chat_id, tenant_id, _job_id=f"eic-chat-compaction:{tenant_id}:{chat_id}")
        return job.job_id if job is not None else None
    except (OSError, RedisError):
        return None


async def enqueue_scheduled_task(
    task_id: int,
    tenant_id: int,
    run_id: int,
    *,
    defer_by: float | timedelta | None = None,
) -> str | None:
    """Submit a durable task run with an explicit tenant-bound job identity."""
    try:
        queue = await get_queue()
        kwargs = {"_job_id": f"eic-task-run:{run_id}"}
        if defer_by is not None:
            kwargs["_defer_by"] = defer_by
        job = await queue.enqueue_job("run_scheduled_task", task_id, tenant_id, run_id, **kwargs)
        return job.job_id if job is not None else None
    except (OSError, RedisError):
        return None
