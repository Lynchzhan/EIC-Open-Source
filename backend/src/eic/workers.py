import hashlib
import asyncio
import json
import uuid
import time
from datetime import UTC, datetime, timedelta
from functools import wraps
from pathlib import Path
from typing import ClassVar

from arq import cron
from arq.connections import RedisSettings
from arq.worker import Retry
from redis.exceptions import RedisError
from sqlalchemy import delete, select, update

from eic.ai.embeddings import embed_texts, validate_embedding_model
from eic.ai.providers import ModelProviderError
from eic.ai.agent_graph import AGENT_GRAPH_VERSION, AgentBudget, ModelDecision, ToolCallRequest
from eic.ai.shared_executor import ExecutionServices, execute_shared
from eic.core.config import get_settings
from eic.core.langfuse_context import task_context
from eic.core.database import SessionLocal, engine
from eic.core.schema import require_schema
from eic.core.runtime_io import start_runtime_io, stop_runtime_io
from eic.core.ids import new_id as new_worker_id
from eic.core import observability, outbox, tasks as task_contract, task_schedule
from eic.core.file_access import can_bind_shared_file, file_source_path
from eic.core.runtime_refs import RuntimeReferenceUnavailable, require_runtime_model, require_runtime_resource
from eic.core.queue import enqueue_document_embeddings
from eic.knowledge.documents import extract_document, extract_text
from eic.knowledge.ingestion import build_ingestion_chunks
from eic.knowledge.strategy import chunk_settings_from_base
from eic.models import (
    AIModel,
    ChatMessage,
    KnowledgeBase,
    KnowledgeChunk,
    KnowledgeDocument,
    KnowledgeIngestionRun,
    Resource,
    ScheduledTask,
    TaskRun,
)
from eic.vector.pgvector_store import ChunkRecord, PgVectorStore



def redis_locked(prefix: str):
    """Prevent duplicate side effects when ARQ retries or workers overlap."""
    def decorator(function):
        @wraps(function)
        async def wrapped(ctx: dict, *args, **kwargs):
            redis = ctx.get("redis") if isinstance(ctx, dict) else None
            lock = None
            if redis is not None:
                tenant_id = kwargs.get("tenant_id") or (args[1] if len(args) > 1 else "unknown")
                item_id = kwargs.get("document_id") or kwargs.get("task_id") or (args[0] if args else "unknown")
                lock = redis.lock(f"eic:lock:{prefix}:{tenant_id}:{item_id}", timeout=900, blocking_timeout=0)
                if not await lock.acquire():
                    return {"status": "locked", "resource": f"{prefix}:{item_id}"}
            try:
                return await function(ctx, *args, **kwargs)
            finally:
                if lock is not None:
                    try:
                        await lock.release()
                    except (OSError, RedisError, RuntimeError):
                        pass
        return wrapped
    return decorator




def cron_field_matches(value: int, field: str, minimum: int, maximum: int) -> bool:
    """Small dependency-free matcher for the five-field cron subset used by UI tasks."""
    for item in field.split(","):
        item = item.strip()
        if item == "*":
            return True
        if item.startswith("*/"):
            try:
                step = int(item[2:])
            except ValueError:
                continue
            if step > 0 and (value - minimum) % step == 0:
                return True
            continue
        if "-" in item:
            try:
                start, end = (int(part) for part in item.split("-", 1))
            except ValueError:
                continue
            if start <= value <= end:
                return True
            continue
        try:
            if value == int(item):
                return True
        except ValueError:
            continue
    return False


def cron_matches(expression: str, moment: datetime) -> bool:
    fields = expression.split()
    if len(fields) != 5:
        return False
    minute, hour, day, month, weekday = fields
    return (
        cron_field_matches(moment.minute, minute, 0, 59)
        and cron_field_matches(moment.hour, hour, 0, 23)
        and cron_field_matches(moment.day, day, 1, 31)
        and cron_field_matches(moment.month, month, 1, 12)
        and cron_field_matches((moment.weekday() + 1) % 7, weekday, 0, 6)
    )


@redis_locked("knowledge-base")
async def process_knowledge_base(ctx, knowledge_base_id, tenant_id):
    from eic.knowledge.indexing import process_knowledge_base as implementation
    return await implementation(ctx, knowledge_base_id, tenant_id)


@redis_locked("knowledge-document")
async def process_knowledge_document(ctx, document_id, tenant_id):
    from eic.knowledge.indexing import process_knowledge_document as implementation
    return await implementation(ctx, document_id, tenant_id)


@redis_locked("document-embeddings")
async def process_document_embeddings(ctx, document_id, tenant_id):
    async with SessionLocal() as session:
        document = await session.get(KnowledgeDocument, document_id)
        if document and document.tenant_id == tenant_id and document.file_id:
            file = await session.get(Resource, document.file_id)
            if not can_bind_shared_file(file, tenant_id):
                return {"status": "file_unavailable", "document_id": document_id}
    from eic.knowledge.indexing import process_document_embeddings as implementation
    return await implementation(ctx, document_id, tenant_id)


async def cleanup_document_index(ctx, document_id, tenant_id):
    from eic.knowledge.indexing import cleanup_document_index as implementation
    return await implementation(ctx, document_id, tenant_id)



async def compact_chat_history(ctx: dict, job_id: int, tenant_id: int):
    from eic.ai.conversation_context import process_compaction
    return await process_compaction(ctx, job_id, tenant_id, sessions=SessionLocal)


async def run_scheduled_task(ctx: dict, task_id: int, tenant_id: int, run_id: int) -> dict[str, object]:
    """共享执行图使用独立持久 task runtime；报告与 TaskRun 终态同事务。"""
    from eic.ai.shared_executor import build_execution_graph, ReadOnlyCancellationSupervisor
    from eic.core.task_graph_runtime import TaskRuntime, TaskGraphCheckpointer, TaskLeaseLost
    from langgraph.runtime import get_runtime

    runtime = TaskRuntime(SessionLocal, task_id, tenant_id, run_id,
        consumer=(ctx['event_id'], ctx['consume_token']) if ctx.get('consume_token') else None)
    try:
        status = await runtime.claim()
    except TaskLeaseLost:
        return {"status": "fenced", "task_id": task_id, "run_id": run_id}
    except (ValueError, TypeError):
        return {"status": "failed", "task_id": task_id, "run_id": run_id}
    if status != "claimed":
        return {"status": status, "task_id": task_id, "run_id": run_id}
    observation_token = observability.CONTEXT.set(task_context(
        tenant_id=runtime.tenant_id, run_id=runtime.run_id))
    try:
        payload = runtime.payload
        if payload['handlerType'] == 'agent':
            from eic.core.task_agent import run_agent
            return await run_agent(runtime)
        read_only_interrupted = None
        read_only_effect_known = False
        supervision = ReadOnlyCancellationSupervisor()

        async def task_model(state):
            if not state['tool_results']:
                return ModelDecision('tool_calls', tool_calls=(
                    ToolCallRequest('handler', payload['handlerType'], payload),))
            return ModelDecision('answer', content=json.dumps(state['tool_results'][-1].get('result') or {}))

        async def task_validate(request):
            if request.name != 'knowledge-check' or request.arguments != payload:
                return {'errorCode': 'handler_not_registered'}
            # 在持久 in-flight 标记之前拒绝已撤销资源；handler 内仍重复检查。
            await runtime.guard()
            async with SessionLocal() as session:
                try:
                    await task_contract.check_resources(session, tenant_id, request.arguments)
                except ValueError:
                    return {'errorCode': 'knowledge_base_unavailable'}
            return {'requiresApproval': False}

        async def task_tool(request, remaining_ms):
            nonlocal read_only_interrupted, read_only_effect_known
            read_only_interrupted, read_only_effect_known = None, False
            await runtime.guard()
            await runtime.begin_inflight(get_runtime().execution_info, request)
            session_witness = supervision.session(SessionLocal())
            runtime.bind_session(session_witness, supervision)
            # 只读 handler 不持有 allocator 写锁，允许独立租约续租。
            handler_error = None
            handler_entered = False
            try:
                async with session_witness as session:
                    try:
                        async with supervision.handler_budget(max(0, remaining_ms / 1000)) as handler_timeout:
                            handler_entered = True
                            supervision.handler_entered(request, handler_timeout)
                            try:
                                value = await task_contract.execute_handler(session, tenant_id, request.arguments)
                            except asyncio.CancelledError as exc:
                                supervision.observe_handler_cancel(request, exc)
                                raise
                            finally:
                                supervision.handler_left()
                    except (OSError, TimeoutError) as exc:
                        # 普通异常仅在零取消历史下可重试；预算取消走专属投递凭据。
                        if not handler_entered:
                            raise
                        handler_error = exc
            except asyncio.CancelledError as exc:
                # 只有 handler 原异常穿过未抑制、未替换且成功的 Session 退出才可确认。
                supervision.raise_tool_exit(request, exc)
            supervision.reject_suppressed_cancel()
            runtime.session_closed(session_witness, supervision)
            # 仅handler的await捕获异常；走到这里证明Session进入及退出均成功。
            if handler_error is not None:
                if request.name == 'knowledge-check' and request.arguments == payload:
                    read_only_interrupted = handler_error
                raise handler_error
            await runtime.guard()
            result = {'callId': request.call_id, 'toolName': request.name, 'status': 'completed',
                      'result': value, 'effectStatus': 'none'}
            runtime.issue_completion(result)
            return result

        def known_read_only_interruption(request, exc):
            nonlocal read_only_interrupted, read_only_effect_known
            if exc is supervision.accepted_error and exc is not None:
                read_only_interrupted = exc
            # 由共享节点在原effect/provider状态均已知后调用；异常必须是本次凭据本体。
            read_only_effect_known = (exc is read_only_interrupted
                and request.name == 'knowledge-check' and request.arguments == payload)
            return read_only_effect_known

        async def renew():
            from eic.core import task_graph_runtime
            while True:
                await asyncio.sleep(max(.1, task_graph_runtime.LEASE_SECONDS / 3))
                await runtime.guard()

        heartbeat = asyncio.create_task(renew())
        try:
            graph = build_execution_graph(TaskGraphCheckpointer(runtime))
            execution = await execute_shared(json.dumps(payload, sort_keys=True),
                run_id=f'task:{tenant_id}:{run_id}', graph=graph, services=ExecutionServices(
                    model=task_model, execute_tool=task_tool, validate_tool=task_validate,
                    guard=runtime.guard, known_read_only_interruption=known_read_only_interruption,
                    cancellation_supervision=supervision,
                    budget=AgentBudget(max_steps=2, max_tool_calls=1, max_total_ms=300_000)))
            if supervision.rejected:
                raise ValueError('shared_checkpoint_supervision_rejected')
            if (execution.get('stop_reason') == 'total_budget_exceeded'
                    and read_only_effect_known
                    and execution.get('effect_status') == 'none'
                    and execution.get('provider_outcome') == 'known'
                    and execution.get('active_call', {}).get('name') == 'knowledge-check'
                    and execution.get('active_call', {}).get('arguments') == payload):
                read_only_interrupted = supervision.accepted_error or TimeoutError('read_only_handler_budget_exceeded')
                raise read_only_interrupted
            result = (execution['tool_results'][-1].get('result', {})
                      if execution.get('stop_reason') == 'answer' and execution.get('tool_results') else {})
            result = {**result, 'graphVersion': AGENT_GRAPH_VERSION}
            valid = task_contract.valid_result(result, payload)
            outcome, error = ('success', '') if valid else ('failed', 'invalid_handler_result')
            if (execution.get('effect_status') == 'unknown'
                    or execution.get('provider_outcome') == 'uncertain'):
                outcome, result, error = 'uncertain', {}, 'task_checkpoint_outcome_unknown'
        except TaskLeaseLost:
            return {"status": "fenced", "task_id": task_id, "run_id": run_id}
        except (OSError, TimeoutError) as exc:
            if (read_only_effect_known and exc is read_only_interrupted
                    and supervision.retry_permitted(exc)):
                async with SessionLocal() as session:
                    _, run = await runtime.current(session)
                    task = await session.get(ScheduledTask, task_id)
                    outcome = 'retrying' if run.attempt < max(1, task.max_retries) else 'failed'
                result, error = {'status': outcome, 'safeToRetry': outcome == 'retrying'}, 'read_only_handler_unavailable'
            else:
                outcome, result, error = 'uncertain', {}, 'task_checkpoint_outcome_unknown'
        except (ValueError, TypeError) as exc:
            known = supervision.rejected or str(exc).startswith(('task_checkpoint', 'shared_checkpoint'))
            outcome, result, error = ('uncertain' if known else 'failed'), {}, (
                str(exc) if known and str(exc).isidentifier() else 'task_checkpoint_or_input_invalid')
        finally:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)
        try:
            outcome = await runtime.finish(outcome, result, error, supervision=supervision,
                                           interruption=read_only_interrupted)
        except TaskLeaseLost:
            return {"status": "fenced", "task_id": task_id, "run_id": run_id}
        return {"status": outcome, "safeToRetry": outcome == 'retrying', "task_id": task_id, "run_id": run_id}
    finally:
        observability.CONTEXT.reset(observation_token)


async def process_due_tasks(ctx: dict) -> dict[str, int]:
    """扫描持久 next_run_at，计划时刻/版本幂等；没有默认停机补跑。"""
    if not get_settings().task_execution_enabled:
        return {"queued": 0}
    await outbox.recover_consumers(kind='scheduled-task')
    await outbox.recover_task_runs()
    await outbox.dispatch_pending(kind='scheduled-task')
    async with SessionLocal() as session:
        events = await task_contract.scan_due(session, datetime.now(UTC))
        await session.commit()
    queued = 0
    for event_id in events:
        queued += bool(await outbox.dispatch_one(event_id))
    return {"queued": queued}


async def consume_outbox_event(ctx: dict, event_id: int, tenant_id: int):
    return await outbox.consume_event(ctx, event_id, tenant_id)


async def worker_startup(_: dict) -> None:
    settings = get_settings()
    if settings.auto_create_schema or settings.runtime_schema_updates:
        raise RuntimeError("Startup DDL switches are retired; run the isolated release migration step")
    try:
        await require_schema(engine)
        start_runtime_io()
        _["runtime_io_started"] = True
        await observability.collector.start()
        if settings.task_execution_enabled:
            _["task_heartbeat_key"] = "eic:tasks:worker:" + uuid.uuid4().hex
            await publish_task_heartbeat(_)
            _["task_heartbeat"] = asyncio.create_task(task_heartbeat_loop(_))
    except BaseException:
        try:
            await observability.collector.stop()
        finally:
            try:
                if _.pop("runtime_io_started", False):
                    await stop_runtime_io()
            finally:
                await engine.dispose()
        raise


async def publish_task_heartbeat(ctx):
    await ctx["redis"].set(ctx["task_heartbeat_key"], json.dumps({
        "contract": task_contract.CONTRACT, "handlers": list(task_contract.HANDLERS),
        "scope": task_contract.worker_scope(),
    }), ex=30)


async def task_heartbeat_loop(ctx):
    while True:
        await asyncio.sleep(10)
        try:
            await publish_task_heartbeat(ctx)
        except (OSError, RedisError):
            pass


async def worker_shutdown(ctx):
    try:
        heartbeat = ctx.get("task_heartbeat")
        if heartbeat:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)
            try:
                await ctx["redis"].delete(ctx["task_heartbeat_key"])
            except (OSError, RedisError):
                pass
    finally:
        try:
            await observability.collector.stop()
        finally:
            try:
                if ctx.pop("runtime_io_started", False):
                    await stop_runtime_io()
            finally:
                await engine.dispose()


class WorkerSettings:
    on_startup = worker_startup
    on_shutdown = worker_shutdown
    redis_settings = RedisSettings.from_dsn(get_settings().redis_url)
    functions: ClassVar[list] = [consume_outbox_event, process_knowledge_base, process_knowledge_document, process_document_embeddings, compact_chat_history, run_scheduled_task, process_due_tasks]
    cron_jobs: ClassVar[list] = [cron(process_due_tasks, minute=set(range(60)), second=0, unique=True)]


