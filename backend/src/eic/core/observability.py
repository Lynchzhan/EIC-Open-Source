"""有界、提交后、独立事务采集；所有诊断计数仅为本进程观测。"""
from __future__ import annotations

import asyncio
from collections import Counter
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime, timedelta
from functools import wraps
import hashlib
import hmac
import math
import re
import sqlite3
import time
import uuid

from sqlalchemy import delete, event, inspect, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from eic.core.config import get_settings
from eic.core.langfuse_exporter import exporter as langfuse_exporter
from eic.core.langfuse_context import ingestion_context, prompt_summary, task_context
from eic.models import AgentRun, ChatEvent, ChatMessage, KnowledgeIngestionRun, LangfuseDelivery, ObservabilityEvent, TaskRun, ToolAudit

CONTEXT: ContextVar[dict | None] = ContextVar("eic_observation_context", default=None)
SUPPRESSED: ContextVar[bool] = ContextVar("eic_observation_suppressed", default=False)
CATEGORIES = {"request", "agent_run", "model_call", "tool_call", "rag_retrieval",
              "sse", "approval", "error", "budget"}
SUMMARY_NUMBERS = {"httpStatus", "afterSequence", "resultCount", "topK", "sparseCount",
                   "denseCount", "replayedEvents", "duplicateEvents", "candidateCount",
                   "queryEmbeddingCount", "summaryVersion", "indexedDimensions",
                   "adapterChunkCount", "textBatchCount", "guardCheckCount", "heartbeatCount"}
SUMMARY_TIMINGS = {"adapterReadMs", "consumerPauseMs", "adapterFirstTextMs", "guardCheckMs",
                   "eventPersistenceMs", "emitWaitMs", "firstTextEmittedMs"}
SUMMARY_TAGS = {"method", "route", "eventType", "denseStatus", "rerankStatus", "cacheSource",
                "promptTemplateId", "promptTemplateVersion", "promptTemplateDigest", "promptIdentityStatus",
                "modelName"}
TAG = re.compile(r"^[A-Za-z0-9_./:{}-]{1,120}$")


def actor_key(tenant_id, user_id):
    if user_id is None:
        return None
    secret = get_settings().secret_encryption_key.encode("utf-8")
    return hmac.new(secret, f"observation:v1:{tenant_id}:{user_id}".encode(), hashlib.sha256).hexdigest()


def integer(value):
    return value if type(value) is int and 0 <= value <= 2**63 - 1 else None


def turn_trace(tenant_id, request_id):
    return uuid.uuid5(uuid.NAMESPACE_URL, f"eic:turn:{tenant_id}:{request_id}").hex


def turn_span(tenant_id, request_id):
    return uuid.uuid5(uuid.NAMESPACE_URL, f"eic:span:{tenant_id}:{request_id}").hex


def usage_fields(payload, *, ollama=False):
    """只复制提供方真实字段；总数不从部分字段补造。"""
    if not isinstance(payload, dict):
        return {}
    usage = payload if ollama else payload.get("usage")
    if not isinstance(usage, dict):
        return {}
    fields = {}
    names = {"input_tokens": "prompt_eval_count", "output_tokens": "eval_count"} if ollama else {
        "input_tokens": "prompt_tokens", "output_tokens": "completion_tokens", "total_tokens": "total_tokens",
    }
    for destination, name in names.items():
        value = integer(usage.get(name))
        if value is not None:
            fields[destination] = value
    if not ollama:
        # Responses 风格使用不同名称，但仍只读取明确返回的值。
        for destination in ("input_tokens", "output_tokens", "total_tokens"):
            if destination not in fields and integer(usage.get(destination)) is not None:
                fields[destination] = usage[destination]
        details = usage.get("prompt_tokens_details") or usage.get("input_tokens_details")
        cached = integer(details.get("cached_tokens")) if isinstance(details, dict) else None
        if cached is not None and ("input_tokens" not in fields or cached <= fields["input_tokens"]):
            fields["cached_tokens"] = cached
            fields["cache_outcome"] = "hit" if cached > 0 else "miss"
    if fields:
        fields["usage_source"] = "provider_ollama" if ollama else "provider_usage"
    return fields


def observation(category, stage, status, *, tenant_id=None, owner_id=None, event_key=None,
                summary=None, occurred_at=None, **fields):
    if SUPPRESSED.get():
        return None
    context = CONTEXT.get() or {}
    tenant = tenant_id if tenant_id is not None else context.get("tenant_id")
    if category not in CATEGORIES or tenant is None:
        return None
    # 显式跨轮次投影不能继承原协程的父节点或用户关联。
    same_scope = context.get("tenant_id") == tenant and all(
        fields.get(key) is None or fields[key] == context.get(key)
        for key in ("trace_id", "request_id"))
    valid_context = context if same_scope else {}
    row = {key: valid_context[key] for key in
           ("request_id", "chat_id", "run_id", "trace_id", "agent_id", "owner_key") if key in valid_context}
    if valid_context.get("span_id") and valid_context["span_id"] != fields.get("span_id"):
        row["parent_span_id"] = valid_context["span_id"]
    if owner_id is not None:
        row["owner_key"] = actor_key(tenant, owner_id)
    stage = stage if isinstance(stage, str) and TAG.fullmatch(stage) else "unknown"
    status = status if isinstance(status, str) and TAG.fullmatch(status) else "unknown"
    row.update(event_key=event_key or uuid.uuid4().hex, tenant_id=tenant, schema_version=1,
               project="eic", category=category, stage=stage[:80], status=status[:32],
               source="runtime", usage_source="not_provided", occurred_at=occurred_at or datetime.now(UTC))
    allowed = set(ObservabilityEvent.__table__.columns.keys()) - {
        "tenant_id", "project", "owner_key", "category", "stage", "status", "event_key",
        "occurred_at", "collected_at", "summary", "schema_version",
    }
    for name, value in fields.items():
        if name not in allowed or value is None:
            continue
        column = ObservabilityEvent.__table__.columns[name]
        if name in {"error_code", "source", "usage_source", "cache_outcome"} and (
            not isinstance(value, str) or not TAG.fullmatch(value)
        ):
            continue
        if name.endswith("_tokens") or name in {"attempt", "source_sequence"}:
            value = integer(value)
        elif name == "duration_ms":
            value = float(value) if isinstance(value, (float, int)) and math.isfinite(value) and value >= 0 else None
        elif isinstance(value, str):
            value = value[:getattr(column.type, "length", 120)]
        else:
            continue
        if value is not None:
            row[name] = value
    clean = {}
    for key, value in (summary or {}).items():
        if key in SUMMARY_NUMBERS and integer(value) is not None:
            clean[key] = value
        elif (key in SUMMARY_TIMINGS and type(value) in (int, float)
              and math.isfinite(value) and 0 <= value <= 31536000000):
            clean[key] = value
        elif key in SUMMARY_TAGS and isinstance(value, str) and TAG.fullmatch(value):
            clean[key] = value
    row["summary"] = clean
    return row


class Collector:
    def __init__(self):
        self.queue = None
        self.worker = None
        self.engine = None
        self.sessions = None
        self.counters = Counter()
        self.running = False
        self.last_written_at = None
        self.sqlite = False
        self.delivery_worker = None
        self.retention_worker = None
        self.retention_cursor = None
        self.retention_more = False
        self.delivery_diagnostics = {}

    async def start(self, sessions=None):
        if self.running or not get_settings().observability_enabled:
            return
        self.queue = asyncio.Queue(maxsize=get_settings().observability_queue_size)
        if sessions is None:
            url = get_settings().database_url
            options = ({"poolclass": NullPool, "connect_args": {"timeout": 0.1}}
                       if "sqlite" in url else {"pool_size": 3, "max_overflow": 0,
                           "pool_timeout": 1, "pool_pre_ping": True, "pool_recycle": 1800})
            self.engine = create_async_engine(url, **options)
            sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        self.sessions = sessions
        self.retention_cursor, self.retention_more = None, False
        bind = getattr(sessions, "kw", {}).get("bind")
        self.sqlite = getattr(getattr(bind, "dialect", None), "name", None) == "sqlite"
        self.running = True
        self.worker = asyncio.create_task(self._consume(), name="eic-observation-writer")
        self.retention_worker = asyncio.create_task(self._prune_loop(), name="eic-observation-retention")
        try:
            await langfuse_exporter.start()
            if langfuse_exporter._accepting:
                from eic.core.langfuse_delivery import loop
                self.delivery_worker = asyncio.create_task(loop(self.sessions, langfuse_exporter, self.delivery_diagnostics), name="eic-langfuse-delivery")
        except Exception:
            self.counters["exportStartupFailures"] += 1

    def submit(self, row):
        if SUPPRESSED.get():
            return False
        if row is None:
            return False
        if not self.running or self.queue is None:
            self.counters["unavailable"] += 1
            return False
        try:
            self.queue.put_nowait(row)
            self.counters["enqueued"] += 1
            return True
        except asyncio.QueueFull:
            self.counters["dropped"] += 1
            return False

    async def _write(self, row):
        async with self.sessions() as session:
            existing = await session.get(ObservabilityEvent, row["event_key"], with_for_update=True)
            if existing is not None:
                self.counters["duplicates"] += 1
                # 上次commit可能在超时边界已成功；只导出已落盘字节，由exporter去重。
                row = {column.name: getattr(existing, column.name)
                       for column in ObservabilityEvent.__table__.columns
                       if column.name != "collected_at"}
            else:
                session.add(ObservabilityEvent(**row, collected_at=datetime.now(UTC)))
            from eic.core.langfuse_delivery import stage
            await stage(session, row, langfuse_exporter)
            await session.commit()
            if existing is None:
                self.counters["written"] += 1
                self.last_written_at = datetime.now(UTC)

    async def prune(self):
        cutoff = datetime.now(UTC) - timedelta(days=get_settings().observability_retention_days)
        async with self.sessions() as session:
            # 当前 schema 没有全局 occurred_at 索引；先按主键扫描有界窗口，避免全表排序/锁定。
            candidates = select(ObservabilityEvent.event_key).order_by(ObservabilityEvent.event_key).limit(200)
            if self.retention_cursor is not None:
                candidates = candidates.where(ObservabilityEvent.event_key > self.retention_cursor)
            scanned = list(await session.scalars(candidates))
            if not scanned:
                self.retention_cursor, self.retention_more = None, False
                return 0
            expired = list(await session.scalars(select(ObservabilityEvent.event_key)
                                              .where(ObservabilityEvent.event_key.in_(scanned),
                                                   ObservabilityEvent.occurred_at < cutoff)
                                              .order_by(ObservabilityEvent.event_key)
                                              .limit(200).with_for_update()))
            # 与写入保持事件 -> 投递的锁顺序；锁定事件后再以当前读复核，不能使用旧快照。
            protected = set(await session.scalars(select(LangfuseDelivery.event_key).where(
                LangfuseDelivery.event_key.in_(expired),
                LangfuseDelivery.status.notin_(["acknowledged", "not_applicable"]),
            ).order_by(LangfuseDelivery.event_key, LangfuseDelivery.destination).with_for_update())) if expired else set()
            keys = [key for key in expired if key not in protected]
            if keys:
                await session.execute(delete(LangfuseDelivery).where(LangfuseDelivery.event_key.in_(keys),
                    LangfuseDelivery.status.in_(["acknowledged", "not_applicable"])))
                await session.execute(delete(ObservabilityEvent).where(ObservabilityEvent.event_key.in_(keys)))
                await session.commit()
            self.retention_more = len(scanned) == 200
            self.retention_cursor = scanned[-1] if self.retention_more else None
            self.counters["retentionScanned"] += len(scanned)
            return len(keys)

    async def _prune_loop(self):
        while True:
            backlog = False
            for _ in range(10):
                try:
                    async with asyncio.timeout(get_settings().observability_write_timeout):
                        count = await self.prune()
                    self.counters["retentionPruned"] += count
                except asyncio.CancelledError:
                    raise
                except Exception:
                    self.counters["retentionFailures"] += 1
                    backlog = True
                    break
                backlog = self.retention_more
                if not backlog:
                    break
                await asyncio.sleep(0.05)
            await asyncio.sleep(60 if backlog else 3600)

    async def _consume(self):
        while True:
            row = await self.queue.get()
            try:
                settings = get_settings()
                async with asyncio.timeout(max(settings.observability_write_timeout,
                                               settings.observability_lock_retry_timeout)):
                    await self._write_with_retry(row)
            except Exception as exc:
                # 不记录原始异常/SQL/连接串，避免采集错误自身泄露正文或凭据。
                self.counters["writeFailures"] += 1
                kind = type(exc).__name__
                if kind in {"TimeoutError", "OperationalError", "IntegrityError", "StatementError"}:
                    self.counters[f"writeFailure.{kind}"] += 1
            except asyncio.CancelledError:
                self.counters["shutdownDropped"] += 1
                raise
            finally:
                self.queue.task_done()

    async def _write_with_retry(self, row):
        # SQLite短暂写锁不是坏观测；在消费端有界重试窗口内重开独立事务。
        delay = 0.01
        while True:
            try:
                async with asyncio.timeout(get_settings().observability_write_timeout):
                    await self._write(row)
                return
            except (OperationalError, TimeoutError) as exc:
                if isinstance(exc, TimeoutError):
                    # 单次SQLite事务已退出并清理；同一event_key重试，不重跑业务。
                    if not self.sqlite:
                        raise
                    self.counters["writeTimeoutRetries"] += 1
                else:
                    code = getattr(exc.orig, "sqlite_errorcode", None)
                    if not isinstance(exc.orig, sqlite3.OperationalError) or not isinstance(code, int) or (
                        code & 0xff
                    ) not in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}:
                        raise
                self.counters["writeRetries"] += 1
                await asyncio.sleep(delay)
                delay = min(delay * 2, 0.05)

    async def flush(self):
        if self.queue is not None:
            await self.queue.join()

    async def stop(self):
        self.running = False
        if self.retention_worker is not None:
            self.retention_worker.cancel()
            await asyncio.gather(self.retention_worker, return_exceptions=True)
            self.retention_worker = None
        if self.delivery_worker is not None:
            self.delivery_worker.cancel()
            await asyncio.gather(self.delivery_worker, return_exceptions=True)
            self.delivery_worker = None
        try:
            if self.worker is not None:
                try:
                    async with asyncio.timeout(2):
                        await self.flush()
                except TimeoutError:
                    self.counters["shutdownTimeouts"] += 1
                finally:
                    self.worker.cancel()
                    try:
                        await asyncio.gather(self.worker, return_exceptions=True)
                    finally:
                        self.worker = None
                        while not self.queue.empty():
                            self.queue.get_nowait()
                            self.queue.task_done()
                            self.counters["shutdownDropped"] += 1
        finally:
            try:
                await langfuse_exporter.stop()
            except Exception:
                self.counters["exportShutdownFailures"] += 1
            finally:
                if self.engine is not None:
                    await self.engine.dispose()
                    self.engine = None

    def health(self):
        return {"running": self.running, "queueCapacity": get_settings().observability_queue_size,
                "queueDepth": self.queue.qsize() if self.queue else 0,
                "counters": dict(self.counters), "scope": "current_process_since_start",
                "durableLossAccounting": False, "crashLoss": "not_measurable",
                "crashLossWindow": "before_local_commit",
                "deliveryLedger": dict(self.delivery_diagnostics),
                "deliveryMeaning": "acknowledged_is_transport_receipt_not_remote_readback",
                "retentionDays": get_settings().observability_retention_days,
                "lastWrittenAt": self.last_written_at.isoformat() if self.last_written_at else None}


collector = Collector()


def stage(session, row):
    if row is None or not collector.running:
        return
    session = getattr(session, "sync_session", session)
    if not session.in_transaction():
        session.begin()
    transaction = session.get_nested_transaction() or session.get_transaction()
    buckets = session.info.setdefault("observability_pending", {})
    pending = buckets.setdefault(transaction, {})
    if sum(len(rows) for rows in buckets.values()) < get_settings().observability_queue_size or row["event_key"] in pending:
        pending[row["event_key"]] = row
    else:
        collector.counters["transactionDropped"] += 1


@event.listens_for(Session, "after_commit")
def _committed(session):
    transaction = session.get_nested_transaction() or session.get_transaction()
    buckets = session.info.get("observability_pending", {})
    rows = buckets.pop(transaction, {})
    if transaction is not None and transaction.nested:
        buckets.setdefault(transaction.parent, {}).update(rows)
    else:
        for row in rows.values():
            collector.submit(row)


@event.listens_for(Session, "after_rollback")
def _rolled_back(session):
    transaction = session.get_nested_transaction() or session.get_transaction()
    session.info.get("observability_pending", {}).pop(transaction, None)


@event.listens_for(Session, "after_transaction_end")
def _transaction_ended(session, transaction):
    session.info.get("observability_pending", {}).pop(transaction, None)
    if transaction.parent is None:
        session.info.pop("observability_pending", None)


def _business_events(session, _context):
    if not collector.running:
        return
    for entity in list(session.new) + list(session.dirty):
        if isinstance(entity, ChatMessage) and entity.role == "user" and entity.request_id:
            changed = entity in session.new or inspect(entity).attrs.status.history.has_changes()
            if not changed:
                continue
            if entity in session.new:
                status, at, duration = "started", entity.created_at, None
            elif entity.status in {"completed", "failed", "cancelled", "canceled", "waiting_approval"}:
                status, at = entity.status, datetime.now(UTC)
                start = entity.created_at.replace(tzinfo=UTC) if entity.created_at.tzinfo is None else entity.created_at
                duration = max(0, (at - start).total_seconds() * 1000)
            else:
                continue
            stage(session, observation(
                "request", "chat_turn", status, tenant_id=entity.tenant_id,
                event_key=f"turn:{entity.tenant_id}:{entity.request_id}:{status}:{uuid.uuid4().hex}",
                request_id=entity.request_id, chat_id=str(entity.session_id),
                trace_id=(CONTEXT.get() or {}).get("trace_id") or turn_trace(entity.tenant_id, entity.request_id),
                span_id=turn_span(entity.tenant_id, entity.request_id),
                source="chat_messages", occurred_at=at, duration_ms=duration))
        elif isinstance(entity, AgentRun):
            if entity not in session.new and not inspect(entity).attrs.status.history.has_changes():
                continue
            finished = entity.finished_at
            duration = None
            if finished and entity.started_at:
                duration = (finished.replace(tzinfo=UTC) - entity.started_at.replace(tzinfo=UTC)).total_seconds() * 1000
            context_job = (entity.run_metadata or {}).get("contextJobId")
            root = ({"span_id": turn_span(entity.tenant_id, entity.request_id)}
                    if type(context_job) is int and entity.request_id == f"context:{context_job}"
                    else {})
            stage(session, observation(
                "agent_run", entity.current_node, entity.status, tenant_id=entity.tenant_id,
                event_key=f"agent-run:{entity.run_id}:{entity.status}:{entity.event_seq}",
                run_id=entity.run_id, trace_id=entity.trace_id, request_id=entity.request_id,
                chat_id=str(entity.session_id) if entity.session_id is not None else None,
                source="agent_runs", error_code=entity.error_code or None,
                occurred_at=finished or entity.started_at, duration_ms=duration, **root))
            category = ("approval" if "approval" in entity.status else
                        "budget" if "budget" in (entity.error_code or "") else
                        "error" if entity.status == "failed" else None)
            if category:
                stage(session, observation(
                    category, entity.current_node, entity.status, tenant_id=entity.tenant_id,
                    event_key=f"run-outcome:{entity.run_id}:{entity.status}:{entity.event_seq}",
                    run_id=entity.run_id, trace_id=entity.trace_id, request_id=entity.request_id,
                    source="agent_runs", error_code=entity.error_code or None,
                    occurred_at=finished or entity.heartbeat_at))
        elif isinstance(entity, TaskRun):
            if (entity not in session.new
                    and not inspect(entity).attrs.status.history.has_changes()
                    and not inspect(entity).attrs.attempt.history.has_changes()):
                continue
            identity = f"task:{entity.tenant_id}:{entity.id}"
            finished, started = entity.finished_at, entity.started_at
            duration = None
            if finished is not None and started is not None:
                start = started.replace(tzinfo=UTC) if started.tzinfo is None else started.astimezone(UTC)
                end = finished.replace(tzinfo=UTC) if finished.tzinfo is None else finished.astimezone(UTC)
                duration = max(0, (end - start).total_seconds() * 1000)
            root_token = CONTEXT.set(task_context(tenant_id=entity.tenant_id, run_id=entity.id))
            try:
                row = observation(
                    "agent_run", "scheduled_task", entity.status, tenant_id=entity.tenant_id,
                    event_key=f"task-run:{entity.tenant_id}:{entity.id}:{entity.attempt}:{entity.status}",
                    request_id=identity, run_id=identity, trace_id=turn_trace(entity.tenant_id, identity),
                    span_id=turn_span(entity.tenant_id, identity), source="task_runs",
                    occurred_at=finished or started or entity.created_at,
                    attempt=entity.attempt, duration_ms=duration)
            finally:
                CONTEXT.reset(root_token)
            stage(session, row)
        elif isinstance(entity, KnowledgeIngestionRun):
            attributes = inspect(entity).attrs
            if (entity not in session.new and not attributes.status.history.has_changes()
                    and not attributes.finished_at.history.has_changes()):
                continue
            scope = ingestion_context(tenant_id=entity.tenant_id,
                document_id=entity.document_id, run_id=entity.id)
            if not scope:
                continue
            finished, started = entity.finished_at, entity.started_at
            completion_phase = "finished" if finished is not None else "open"
            duration = None
            superseded = entity.status == "superseded"
            if finished is not None and started is not None and not superseded:
                start = started.replace(tzinfo=UTC) if started.tzinfo is None else started.astimezone(UTC)
                end = finished.replace(tzinfo=UTC) if finished.tzinfo is None else finished.astimezone(UTC)
                duration = max(0, (end - start).total_seconds() * 1000)
            root_token = CONTEXT.set(scope)
            try:
                row = observation(
                    "agent_run", "ingestion", entity.status, tenant_id=entity.tenant_id,
                    event_key=f"ingestion-run:{entity.tenant_id}:{entity.id}:{entity.status}:{completion_phase}",
                    request_id=scope["request_id"], run_id=scope["run_id"],
                    trace_id=scope["trace_id"], span_id=scope["span_id"],
                    source="knowledge_ingestion_runs", occurred_at=datetime.now(UTC) if superseded else finished or started,
                    duration_ms=duration)
            finally:
                CONTEXT.reset(root_token)
            stage(session, row)
        elif isinstance(entity, ToolAudit) and entity in session.new:
            stage(session, observation(
                "tool_call", "tool", entity.outcome, tenant_id=entity.tenant_id, owner_id=entity.user_id,
                event_key=f"tool-audit:{entity.tenant_id}:{entity.id}", source="tool_audits",
                tool_name=entity.tool_name, duration_ms=entity.duration_ms if entity.duration_ms > 0 else None,
                occurred_at=entity.created_at))
        elif isinstance(entity, ChatEvent) and entity in session.new:
            stage(session, observation(
                "sse", "persisted_event", "recorded", tenant_id=entity.tenant_id,
                event_key=f"chat-event:{entity.tenant_id}:{entity.request_id}:{entity.sequence}",
                request_id=entity.request_id, source="chat_events", source_sequence=entity.sequence,
                occurred_at=entity.created_at, summary={"eventType": entity.event_type}))


@event.listens_for(Session, "after_flush")
def _capture_business_events(session, context):
    try:
        _business_events(session, context)
    except Exception:
        collector.counters["metadataFailures"] += 1


def observe_sse(function):
    @wraps(function)
    async def wrapper(request_id, principal, after_sequence=0):
        # 与 HTTP context 分离；principal 已验证，关联对象仍由原函数验证。
        token = CONTEXT.set({"tenant_id": principal.tenant_id,
                             "owner_key": actor_key(principal.tenant_id, principal.user_id),
                             "request_id": request_id})
        started, span, status = time.monotonic(), uuid.uuid4().hex, "cancelled"
        source = function(request_id, principal, after_sequence)
        try:
            async for chunk in source:
                yield chunk
            status = "closed"
        except Exception:
            status = "error"
            raise
        finally:
            try:
                await source.aclose()
            finally:
                collector.submit(observation("sse", "connection", status, event_key=f"sse:{span}",
                                              source="sse_subscription", span_id=span,
                                              duration_ms=(time.monotonic() - started) * 1000,
                                              summary={"afterSequence": after_sequence}))
                CONTEXT.reset(token)
    return wrapper


def observe_model_stream(function):
    @wraps(function)
    async def wrapper(model, *args, **kwargs):
        sink = kwargs.get("usage_sink")
        if sink is None:
            sink = kwargs["usage_sink"] = {}
        started, span = time.monotonic(), uuid.uuid4().hex
        context = CONTEXT.get() or {}
        matched = context.get("tenant_id") == model.tenant_id
        parent = context.get("span_id") if matched else None
        model_summary = prompt_summary(context.get("_prompt_identities")) if matched else {}
        model_summary["modelName"] = getattr(model, "model_name", None)
        model_summary.update(adapterReadMs=0.0, consumerPauseMs=0.0, adapterChunkCount=0)
        occurred = datetime.now(UTC)
        status, code = "cancelled", None
        source = function(model, *args, **kwargs)
        from eic.ai.runtime_policy import begin_call, finish_call
        ticket = await begin_call(model, "model")
        deadline_at = asyncio.get_running_loop().time() + ticket[2] if ticket else None
        try:
            while True:
                read_started = time.monotonic()
                try:
                    async with asyncio.timeout_at(deadline_at):
                        chunk = await anext(source)
                except StopAsyncIteration:
                    break
                finally:
                    model_summary["adapterReadMs"] += (time.monotonic() - read_started) * 1000
                model_summary["adapterChunkCount"] += 1
                if chunk:
                    model_summary.setdefault("adapterFirstTextMs", (time.monotonic() - started) * 1000)
                # 包括批量刷新窗口和消费者处理，不将该时间误报为上游推理耗时。
                consumer_started = time.monotonic()
                try:
                    yield chunk
                finally:
                    model_summary["consumerPauseMs"] += (time.monotonic() - consumer_started) * 1000
            status = "success"
        except asyncio.CancelledError:
            deadline = (CONTEXT.get() or {}).get("model_deadline")
            if deadline is not None and deadline.expired():
                status, code = "timeout", "provider_outcome_uncertain"
            raise
        except Exception as exc:
            status = "timeout" if isinstance(exc, TimeoutError) else "error"
            candidate = getattr(exc, "reason", "provider_error")
            code = candidate if isinstance(candidate, str) and TAG.fullmatch(candidate) else "provider_error"
            raise
        finally:
            try:
                await source.aclose()
            finally:
                from eic.core.async_cleanup import complete_cleanup
                await complete_cleanup(finish_call(ticket, sink, status != "success"))
            if sink.pop("_invoked", False):
                row = observation("model_call", "model", status, tenant_id=model.tenant_id,
                                  event_key=f"model:{span}", span_id=span, model_id=str(model.id),
                                  parent_span_id=parent, occurred_at=occurred,
                                  attempt=(CONTEXT.get() or {}).get("model_attempt"),
                                  source="provider_stream", error_code=code, summary=model_summary,
                                  duration_ms=(time.monotonic() - started) * 1000, **sink)
                collector.submit(row)
            if code:
                collector.submit(observation(
                    "budget" if "budget" in code else "error", "model", status,
                    tenant_id=model.tenant_id, event_key=f"model-error:{span}", span_id=span,
                    model_id=str(model.id), error_code=code, source="provider_stream"))
    return wrapper


@contextmanager
def observe_chat_stream():
    """每轮模型仅汇总一次正文交付开销，不采集正文或逐片段新增观测。"""
    started, occurred, span = time.monotonic(), datetime.now(UTC), uuid.uuid4().hex
    summary = {"guardCheckMs": 0.0, "eventPersistenceMs": 0.0, "emitWaitMs": 0.0,
               "textBatchCount": 0, "guardCheckCount": 0, "heartbeatCount": 0}
    status = "cancelled"
    try:
        yield summary
        status = "success"
    except Exception:
        status = "error"
        raise
    finally:
        try:
            collector.submit(observation("sse", "stream_delivery", status,
                source="chat_pipeline", span_id=span, occurred_at=occurred,
                summary=summary, duration_ms=(time.monotonic() - started) * 1000))
        except Exception:
            collector.counters["streamObservationFailures"] += 1


@contextmanager
def observe_retrieval_stage(stage_name, *, tenant_id):
    """只采集实际执行的子图节点，不导出查询、正文或文件名。"""
    started, occurred, span = time.monotonic(), datetime.now(UTC), uuid.uuid4().hex
    context = CONTEXT.get() or {}
    context = context if context.get("tenant_id") == tenant_id else {}
    parent = context.get("span_id")
    token = CONTEXT.set({**context, "tenant_id": tenant_id, "span_id": span})
    status = "cancelled"
    summary = {}
    try:
        yield summary
        status = "success"
    except Exception:
        status = "error"
        raise
    finally:
        try:
            collector.submit(observation("rag_retrieval", stage_name, status,
                tenant_id=tenant_id, source="knowledge_ops", event_key=f"rag-stage:{span}",
                span_id=span, parent_span_id=parent, occurred_at=occurred,
                summary=summary,
                duration_ms=(time.monotonic() - started) * 1000))
        except Exception:
            collector.counters["retrievalObservationFailures"] += 1
        finally:
            CONTEXT.reset(token)


def observe_retrieval(function):
    @wraps(function)
    async def wrapper(*args, **kwargs):
        started, span = time.monotonic(), uuid.uuid4().hex
        occurred, parent = datetime.now(UTC), (CONTEXT.get() or {}).get("span_id")
        token = CONTEXT.set({**(CONTEXT.get() or {}), "span_id": span})
        status, code, summary = "cancelled", None, {}
        try:
            result = await function(*args, **kwargs)
            status = "success"
            summary = {"resultCount": len(result.get("results") or []),
                       "topK": result.get("config", {}).get("topK"),
                       "denseCount": result.get("denseCount"), "sparseCount": result.get("sparseCount"),
                       "denseStatus": result.get("denseStatus"), "rerankStatus": result.get("rerankStatus")}
            return result
        except Exception:
            status, code = "error", "retrieval_failed"
            raise
        finally:
            CONTEXT.reset(token)
            collector.submit(observation("rag_retrieval", "retrieval", status,
                                          tenant_id=kwargs.get("tenant_id"), event_key=f"rag:{span}",
                                          span_id=span, source="knowledge_ops", summary=summary,
                                          parent_span_id=parent, occurred_at=occurred,
                                          error_code=code, duration_ms=(time.monotonic() - started) * 1000))
    return wrapper


@contextmanager
def observe_ingestion_stage(stage_name):
    """采集已执行的解析/切分步骤，不记录文档正文，不改变业务事务。"""
    started, occurred, span = time.monotonic(), datetime.now(UTC), uuid.uuid4().hex
    parent = CONTEXT.get() or {}
    token = CONTEXT.set({**parent, "span_id": span})
    status = "cancelled"
    try:
        yield
        status = "completed"
    except Exception:
        status = "failed"
        raise
    finally:
        CONTEXT.reset(token)
        try:
            collector.submit(observation("agent_run", stage_name, status,
                source="knowledge_pipeline", span_id=span, parent_span_id=parent.get("span_id"),
                occurred_at=occurred, duration_ms=(time.monotonic() - started) * 1000,
                error_code="ingestion_stage_failed" if status == "failed" else None))
        except Exception:
            collector.counters["pipelineObservationFailures"] += 1


def observe_model_call(kind):
    def decorate(function):
        @wraps(function)
        async def wrapper(model, *args, **kwargs):
            sink = kwargs.get("usage_sink")
            if sink is None:
                sink = kwargs["usage_sink"] = {}
            started, occurred, span = time.monotonic(), datetime.now(UTC), uuid.uuid4().hex
            status, code = "cancelled", None
            from eic.ai.runtime_policy import begin_call, finish_call
            ticket = await begin_call(model, kind, args, kwargs)
            try:
                async with asyncio.timeout(ticket[2] if ticket else None):
                    result = await function(model, *args, **kwargs)
                status = "success"
                return result
            except Exception:
                status, code = "error", f"{kind}_provider_error"
                raise
            finally:
                from eic.core.async_cleanup import complete_cleanup
                await complete_cleanup(finish_call(ticket, sink, status != "success"))
                if sink.pop("_invoked", False):
                    collector.submit(observation(
                        "model_call", kind, status, tenant_id=model.tenant_id,
                        event_key=f"{kind}:{span}", span_id=span, model_id=str(model.id),
                        source="provider_response", occurred_at=occurred, error_code=code,
                        summary={"modelName": getattr(model, "model_name", None)},
                        duration_ms=(time.monotonic() - started) * 1000, **sink))
        return wrapper
    return decorate


class ObservationMiddleware:
    """纯 ASGI 包装，不消费/缓冲 SSE，不解析请求或响应正文。"""
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not scope.get("path", "").startswith("/api/") or scope["path"].startswith("/api/Observability/"):
            return await self.app(scope, receive, send)
        context = {"request_id": uuid.uuid4().hex}
        scope.setdefault("state", {})["observation_context"] = context
        token = CONTEXT.set(context)
        started, http_status, outcome = time.monotonic(), None, None

        async def wrapped_send(message):
            nonlocal http_status
            if message["type"] == "http.response.start":
                http_status = message["status"]
            await send(message)

        async def wrapped_receive():
            nonlocal outcome
            message = await receive()
            if message["type"] == "http.disconnect":
                outcome = "cancelled"
            return message

        try:
            await self.app(scope, wrapped_receive, wrapped_send)
        except BaseException:
            outcome = "cancelled" if asyncio.current_task().cancelling() else "error"
            raise
        finally:
            route = getattr(scope.get("route"), "path", None)
            status = outcome or ("error" if http_status is None or http_status >= 400 else "success")
            collector.submit(observation("request", "http", status,
                                          event_key=f"http:{context['request_id']}", source="http",
                                          duration_ms=(time.monotonic() - started) * 1000,
                                          summary={"method": scope["method"], "route": route,
                                                   "httpStatus": http_status}))
            CONTEXT.reset(token)
