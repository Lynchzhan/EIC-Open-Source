"""只读、权限前置、有界查询；缺失值不补零，观测行数不冒充调用数。"""
from __future__ import annotations

import asyncio
from collections import Counter
from datetime import UTC, datetime, timedelta
import math

from fastapi import Depends, HTTPException, Query
from sqlalchemy import exists, or_, select

from eic.core import observability as obs
from eic.core import langfuse_query
from eic.core.conversation_observation import live_chat, session_data
from eic.core.config import get_settings
from eic.models import AgentRun, ChatSession, ObservabilityEvent as Event, Tenant, User

MAX_AGGREGATE_ROWS = 10000
MAX_OFFSET = 10000
FAILURES = {"error", "failed", "timeout", "denied"}
SUCCESS = {"success", "completed"}
TERMINAL = SUCCESS | FAILURES | {"cancelled", "canceled"}


def reject(code, message, status=422):
    raise HTTPException(status, detail={"code": code, "errMsg": message})


def utc(value):
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def window(start=None, end=None):
    now = datetime.now(UTC)

    def parse(value):
        try:
            parsed = datetime.fromisoformat(value)
            if parsed.tzinfo is None or parsed.utcoffset() is None:
                raise ValueError
            return parsed.astimezone(UTC)
        except (ValueError, TypeError, OverflowError):
            reject("observation_timezone_required", "时间必须为包含时区的 ISO 8601 格式")

    finish = parse(end) if end is not None else now
    begin = parse(start) if start is not None else finish - timedelta(days=1)
    if finish <= begin or finish - begin > timedelta(days=30):
        reject("observation_window_invalid", "查询区间必须大于零且不超过 30 天")
    effective = max(begin, now - timedelta(days=get_settings().observability_retention_days))
    return begin, finish, effective


def safe_tag(value, maximum=120):
    return value if isinstance(value, str) and len(value) <= maximum and obs.TAG.fullmatch(value) else None


def clean_summary(value):
    if not isinstance(value, dict):
        return {}
    return {key: item for key, item in value.items()
            if (key in obs.SUMMARY_NUMBERS and obs.integer(item) is not None)
            or (key in obs.SUMMARY_TIMINGS and type(item) in (int, float)
                and math.isfinite(item) and 0 <= item <= 31536000000)
            or (key in obs.SUMMARY_TAGS and safe_tag(item))}


def public_event(row, detail=False):
    result = {
        "eventKey": row.event_key, "category": row.category, "stage": safe_tag(row.stage),
        "status": safe_tag(row.status), "source": safe_tag(row.source),
        "occurredAt": utc(row.occurred_at).isoformat(),
        "collectedAt": utc(row.collected_at).isoformat(),
        "requestId": row.request_id, "chatId": row.chat_id, "runId": row.run_id,
        "traceId": row.trace_id, "spanId": row.span_id, "parentSpanId": row.parent_span_id,
        "agentId": row.agent_id, "modelId": row.model_id, "toolName": safe_tag(row.tool_name),
        "workspaceId": row.workspace_id, "durationMs": row.duration_ms,
        "errorCode": safe_tag(row.error_code), "attempt": obs.integer(row.attempt),
        "usage": {**{key: obs.integer(getattr(row, name)) for key, name in (
            ("inputTokens", "input_tokens"), ("outputTokens", "output_tokens"),
            ("totalTokens", "total_tokens"), ("cachedTokens", "cached_tokens"),
            ("savedTokens", "saved_tokens"))},
            "source": safe_tag(row.usage_source) or "not_provided"},
    }
    if detail:
        result["summary"] = clean_summary(row.summary)
        result["sourceSequence"] = obs.integer(row.source_sequence)
    return result


def call_identity(row, field):
    value = getattr(row, field)
    # 旧 ToolAudit 的稳定事件键来自真实审计主键，不是未知调用的猜测标识。
    return value or (row.event_key if row.category == "tool_call" and row.source == "tool_audits" else None)


def distinct(rows, key):
    # 输入按时间升序；更新状态只保留同一真实标识的最新观测。
    result = {}
    for row in rows:
        identity = key(row)
        if identity:
            result[identity] = row
    return list(result.values())


def ratio(numerator, denominator):
    return numerator / denominator if denominator else None


def duration_summary(rows):
    samples = sorted(float(row.duration_ms) for row in rows
                     if isinstance(row.duration_ms, (int, float))
                     and math.isfinite(row.duration_ms) and row.duration_ms >= 0)

    def percentile(q):
        if not samples:
            return None
        index = (len(samples) - 1) * q
        low, high = math.floor(index), math.ceil(index)
        return samples[low] + (samples[high] - samples[low]) * (index - low)

    return {"p50Ms": percentile(.5), "p95Ms": percentile(.95), "p99Ms": percentile(.99),
            "sampleCount": len(samples), "population": len(rows),
            "method": "linear_interpolation", "source": "recorded_duration"}


def outcomes(rows):
    values = Counter(row.status for row in rows)
    denominator = sum(row.status in TERMINAL for row in rows)
    success = sum(values[x] for x in SUCCESS)
    errors = sum(values[x] for x in FAILURES - {"timeout"})
    cancelled = values["cancelled"] + values["canceled"]
    return {"count": len(rows), "terminalCount": denominator, "statuses": dict(values),
            "successCount": success, "errorCount": errors, "timeoutCount": values["timeout"],
            "cancelledCount": cancelled, "successRate": ratio(success, denominator),
            "errorRate": ratio(errors, denominator), "timeoutRate": ratio(values["timeout"], denominator),
            "cancelledRate": ratio(cancelled, denominator), "denominator": "observed_terminal_calls"}


def summarize(rows):
    groups = {name: [row for row in rows if row.category == name] for name in obs.CATEGORIES}
    identities = {"request": "request_id", "agent_run": "run_id", "model_call": "span_id",
                  "tool_call": "span_id", "rag_retrieval": "span_id"}
    calls = {name: distinct(groups[name], lambda row: call_identity(row, identities[name]))
             for name in identities}
    requests, agents, models, tools, rag = (calls[name] for name in identities)
    usage = {}
    for name, public in (("input_tokens", "inputTokens"), ("output_tokens", "outputTokens"),
                         ("total_tokens", "totalTokens"), ("cached_tokens", "cachedTokens"),
                         ("saved_tokens", "savedTokens")):
        provided = [obs.integer(getattr(row, name)) for row in models
                    if row.usage_source in {"provider_usage", "provider_ollama"}]
        provided = [value for value in provided if value is not None]
        usage[public] = {"value": sum(provided) if provided else None,
                         "providedCalls": len(provided), "population": len(models),
                         "complete": bool(models) and len(provided) == len(models),
                         "source": "provider_reported" if provided else "not_provided"}
    cache = Counter(row.cache_outcome for row in models if row.cache_outcome in {"hit", "miss"})
    measured = cache["hit"] + cache["miss"]
    result_counts = [obs.integer(clean_summary(row.summary).get("resultCount")) for row in rag]
    result_counts = [value for value in result_counts if value is not None]
    lags = [max(0, (utc(row.collected_at) - utc(row.occurred_at)).total_seconds() * 1000)
            for row in rows]
    errors = [row for row in rows if row.status in FAILURES or row.category == "error"]
    return {
        "requests": {**outcomes(requests), "latency": duration_summary(requests)},
        "activeUsers": len({row.owner_key for row in requests if row.owner_key}),
        "sessions": len({row.chat_id for row in rows if row.chat_id}),
        "agentRuns": outcomes(agents),
        "modelCalls": {**outcomes(models), "latency": duration_summary(models)},
        "usage": usage,
        "cache": {"hits": cache["hit"] if measured else None, "misses": cache["miss"] if measured else None,
                  "hitRate": ratio(cache["hit"], measured), "measuredCalls": measured,
                  "population": len(models), "denominator": "explicit_hit_or_miss_model_calls"},
        "tools": {**outcomes(tools), "latency": duration_summary(tools),
                  "byName": dict(Counter(safe_tag(row.tool_name) or "unknown" for row in tools))},
        "rag": {"count": len(rag), "latency": duration_summary(rag),
                "measuredCalls": len(result_counts),
                "noResultRate": ratio(result_counts.count(0), len(result_counts)),
                "recallAtK": None, "mrr": None, "ndcg": None, "citationRate": None,
                "qualitySource": "not_provided",
                "byTopK": dict(Counter(str(clean_summary(row.summary)["topK"]) for row in rag
                                      if "topK" in clean_summary(row.summary))),
                "sourceDistribution": None, "sourceDistributionSource": "not_provided"},
        "sse": {"connectionCount": sum(row.stage == "connection" for row in groups["sse"]),
                "statuses": dict(Counter(row.status for row in groups["sse"])),
                "replayedEvents": sum(clean_summary(row.summary).get("replayedEvents", 0)
                                      for row in groups["sse"])
                if any("replayedEvents" in clean_summary(row.summary) for row in groups["sse"]) else None,
                "duplicateEvents": sum(clean_summary(row.summary).get("duplicateEvents", 0)
                                       for row in groups["sse"])
                if any("duplicateEvents" in clean_summary(row.summary) for row in groups["sse"]) else None},
        "approval": {"eventCount": len(groups["approval"]),
                     "statuses": dict(Counter(row.status for row in groups["approval"])),
                     "approvalRate": None, "rateSource": "not_provided"},
        "budget": {"eventCount": len(groups["budget"]), "amount": None, "currency": None,
                   "remaining": None, "source": "not_provided"},
        "errors": {"eventCount": len(errors),
                   "byStage": dict(Counter(safe_tag(row.stage) or "unknown" for row in errors)),
                    "byCode": dict(Counter(safe_tag(row.error_code) or "unknown" for row in errors)),
                    "byAgent": dict(Counter(row.agent_id or "unknown" for row in errors)),
                    "byModel": dict(Counter(row.model_id or "unknown" for row in errors)),
                    "byTool": dict(Counter(safe_tag(row.tool_name) or "unknown" for row in errors)),
                    "byHourUtc": dict(Counter(utc(row.occurred_at).replace(minute=0, second=0,
                                                                         microsecond=0).isoformat()
                                              for row in errors)),
                    "denominator": "error_event_rows_not_failed_requests"},
        "collection": {"maxLagMs": max(lags) if lags else None,
                       "sampleCount": len(lags), "source": "persisted_event_timestamps"},
        "coverage": {"eventRows": len(rows), "scope": "observed_events_only",
                      "historicalBackfill": False, "unobservedCallsMeasurable": False,
                      "unidentifiedCallRows": sum(not call_identity(row, identities[row.category])
                                                  for row in rows if row.category in identities)},
    }


async def fetch(session, statement):
    try:
        async with asyncio.timeout(3):
            return list(await session.scalars(statement))
    except TimeoutError:
        reject("observation_query_timeout", "观测查询超时，请缩小时间范围", 503)


async def aggregate_rows(session, criteria):
    rows = await fetch(session, select(Event).where(*criteria)
                       .order_by(Event.occurred_at, Event.event_key).limit(MAX_AGGREGATE_ROWS + 1))
    if len(rows) > MAX_AGGREGATE_ROWS:
        reject("observation_query_too_large", "观测事件超过 10000 条，请缩小查询范围")
    return rows


def bind_routes(app, current_principal, get_session, ensure_permission):
    async def query_context(
        start: str | None = Query(None, max_length=64), end: str | None = Query(None, max_length=64),
        scope: str = Query("own", pattern="^(own|tenant)$"),
        tenantId: int | None = None, workspaceId: str | None = Query(None, max_length=80),
        agentId: str | None = Query(None, max_length=80),
        modelId: str | None = Query(None, max_length=80),
        toolName: str | None = Query(None, max_length=200),
        category: str | None = Query(None, max_length=24),
        status: str | None = Query(None, max_length=32),
        stage: str | None = Query(None, max_length=80),
        errorStage: str | None = Query(None, max_length=80),
        principal=Depends(current_principal), session=Depends(get_session),
    ):
        # 每次查询从现有身份/RBAC 加载权限，撤销立即生效，不依赖客户端开关。
        user = await session.get(User, principal.user_id)
        tenant = await session.get(Tenant, principal.tenant_id)
        if not user or not user.is_active or user.tenant_id != principal.tenant_id or not tenant or not tenant.active:
            reject("observation_principal_unavailable", "用户或租户不可用", 401)
        await ensure_permission("ai.chat.read", principal, session)
        admin = bool(user.is_super_admin)
        if tenantId is not None and tenantId != principal.tenant_id:
            reject("observation_tenant_forbidden", "不支持跨租户查询", 403)
        if scope == "tenant" and not admin:
            reject("observation_scope_forbidden", "仅管理员可查询本租户聚合", 403)
        begin, finish, effective = window(start, end)
        criteria = [Event.tenant_id == principal.tenant_id, Event.project == "eic",
                    Event.occurred_at >= effective, Event.occurred_at < finish]
        if scope == "own":
            criteria.append(Event.owner_key == obs.actor_key(principal.tenant_id, principal.user_id))
            # owner 摘要不替代当前会话归属。已删除/转移会话的关联事件默认隐藏。
            criteria.append(or_(Event.chat_id.is_(None), exists(select(ChatSession.id).where(
                ChatSession.tenant_id == principal.tenant_id,
                ChatSession.owner_id == principal.user_id,
                live_chat(),
                ChatSession.id.cast(Event.chat_id.type) == Event.chat_id))))
            criteria.append(or_(Event.run_id.is_(None), exists(select(AgentRun.run_id).where(
                AgentRun.run_id == Event.run_id, AgentRun.tenant_id == principal.tenant_id,
                AgentRun.session_id.in_(select(ChatSession.id).where(
                    ChatSession.tenant_id == principal.tenant_id,
                    ChatSession.owner_id == principal.user_id, live_chat()))))))
        for column, value in ((Event.workspace_id, workspaceId), (Event.agent_id, agentId),
                              (Event.model_id, modelId), (Event.tool_name, toolName),
                              (Event.category, category), (Event.status, status), (Event.stage, stage)):
            if value is not None:
                criteria.append(column == value)
        if category is not None and category not in obs.CATEGORIES:
            reject("observation_category_invalid", "观测事件类型无效")
        if errorStage is not None:
            criteria += [Event.stage == errorStage, or_(Event.status.in_(FAILURES), Event.category == "error")]
        return {"session": session, "criteria": criteria, "admin": admin,
                "eventFilters": any(value is not None for value in
                    (workspaceId, agentId, modelId, toolName, category, status, stage, errorStage)),
                "window": {"start": begin.isoformat(), "end": finish.isoformat(),
                           "effectiveStart": effective.isoformat(), "interval": "[start,end)",
                           "timezone": "UTC", "retentionDays": get_settings().observability_retention_days},
                "scope": scope, "principal": principal}

    @app.get("/api/Observability/Summary")
    async def summary_api(context=Depends(query_context)):
        rows = await aggregate_rows(context["session"], context["criteria"])
        return {"window": context["window"], "scope": context["scope"], "metrics": summarize(rows)}

    @app.get("/api/Observability/Session")
    async def session_api(chatId: int = Query(..., ge=1), page: int = Query(1, ge=1),
                          pageSize: int = Query(50, ge=1, le=200),
                          requestIds: str | None = Query(None, max_length=8192), context=Depends(query_context)):
        offset = (page - 1) * pageSize
        if offset > MAX_OFFSET:
            reject("observation_page_too_deep", "分页偏移超过查询上限")
        try:
            async with asyncio.timeout(10):
                result = await session_data(context, chatId, fetch, reject)
        except TimeoutError:
            reject("observation_query_timeout", "会话统计查询超时，请稍后重试", 503)
        items = result.pop("turns")
        if requestIds is not None:
            selected = set(requestIds.split(","))
            if len(selected) > 100:
                reject("observation_request_filter_too_large", "每次最多读取 100 个轮次")
            items = [item for item in items if item["requestId"] in selected]
        return {**result, "window": context["window"], "scope": context["scope"],
                "page": page, "pageSize": pageSize, "hasMore": offset + pageSize < len(items),
                "data": items[offset:offset + pageSize]}

    @app.get("/api/Observability/Sessions")
    async def sessions_api(page: int = Query(1, ge=1), pageSize: int = Query(20, ge=1, le=20),
                           context=Depends(query_context)):
        offset = (page - 1) * pageSize
        if offset > MAX_OFFSET:
            reject("observation_page_too_deep", "分页偏移超过查询上限")
        criteria = [ChatSession.tenant_id == context["principal"].tenant_id,
                    ChatSession.updated_at >= datetime.fromisoformat(context["window"]["start"]),
                    ChatSession.updated_at < datetime.fromisoformat(context["window"]["end"])]
        if context["scope"] == "own":
            criteria.extend([ChatSession.owner_id == context["principal"].user_id, live_chat()])
        if context["eventFilters"]:
            criteria.append(exists(select(Event.event_key).where(*context["criteria"],
                Event.chat_id == ChatSession.id.cast(Event.chat_id.type))))
        chats = await fetch(context["session"], select(ChatSession).where(*criteria)
                            .order_by(ChatSession.updated_at.desc(), ChatSession.id.desc())
                            .offset(offset).limit(pageSize + 1))
        data = []
        try:
            async with asyncio.timeout(10):
                for chat in chats[:pageSize]:
                    result = await session_data(context, chat.id, fetch, reject)
                    data.append({"chatId": str(chat.id), "updatedAt": utc(chat.updated_at).isoformat(),
                                 "summary": result["summary"], "coverage": result["coverage"]})
        except TimeoutError:
            reject("observation_query_timeout", "会话列表查询超时，请缩小范围或每页数量", 503)
        return {"window": context["window"], "scope": context["scope"], "page": page,
                "pageSize": pageSize, "hasMore": len(chats) > pageSize, "data": data}

    @app.get("/api/Observability/Events")
    async def events_api(page: int = Query(1, ge=1), pageSize: int = Query(50, ge=1, le=200),
                         detail: bool = False, context=Depends(query_context)):
        offset = (page - 1) * pageSize
        if offset > MAX_OFFSET:
            reject("observation_page_too_deep", "分页偏移超过 10000，请缩小查询范围")
        if detail and not context["admin"]:
            reject("observation_detail_forbidden", "仅管理员可查看脱敏详情", 403)
        rows = await fetch(context["session"], select(Event).where(*context["criteria"])
                           .order_by(Event.occurred_at.desc(), Event.event_key.desc())
                           .offset(offset).limit(pageSize + 1))
        return {"window": context["window"], "scope": context["scope"], "page": page,
                "pageSize": pageSize, "hasMore": len(rows) > pageSize,
                "data": [public_event(row, detail) for row in rows[:pageSize]]}

    @app.get("/api/Observability/Trace")
    async def trace_api(traceId: str = Query(..., min_length=1, max_length=64),
                        page: int = Query(1, ge=1), pageSize: int = Query(50, ge=1, le=200),
                        detail: bool = False, context=Depends(query_context)):
        if detail and not context["admin"]:
            reject("observation_detail_forbidden", "仅管理员可查看脱敏详情", 403)
        offset = (page - 1) * pageSize
        if offset > MAX_OFFSET:
            reject("observation_page_too_deep", "分页偏移超过 10000，请缩小查询范围")
        rows = await aggregate_rows(context["session"], [*context["criteria"], Event.trace_id == traceId])
        if not rows:
            reject("observation_trace_not_found", "Trace 不存在或无权访问", 404)
        failure = next((row for row in rows if row.status in FAILURES or row.category == "error"), None)
        models = distinct([row for row in rows if row.category == "model_call"], lambda row: row.span_id)
        attempts = [row.attempt for row in models if obs.integer(row.attempt) is not None]
        spans = {}
        for row in rows:
            if row.span_id or row.category == "tool_call":
                key = row.span_id or row.event_key
                item = public_event(row)
                if key in spans and row.category == "error" and spans[key]["category"] == "model_call":
                    continue
                if key in spans and row.stage == "chat_turn":
                    item["occurredAt"] = spans[key]["occurredAt"]
                spans[key] = item
        langfuse_fields = {}
        if context["admin"]:
            langfuse_fields["langfuse"] = langfuse_query.trace_details(context, rows)
        return {**langfuse_fields, "traceId": traceId, "window": context["window"], "scope": context["scope"],
                "spans": list(spans.values())[:500], "spanCount": len(spans),
                "spansComplete": len(spans) <= 500,
                "eventCount": len(rows), "firstFailure": public_event(failure) if failure else None,
                "firstFailureScope": "visible_events_in_query_window",
                "modelCallCount": len(models),
                "retryCount": sum(attempt > 1 for attempt in attempts) if attempts else None,
                "retrySource": "explicit_attempt" if attempts else "not_provided",
                "retryCoverage": {"providedCalls": len(attempts), "population": len(models)},
                "page": page, "pageSize": pageSize, "hasMore": offset + pageSize < len(rows),
                "data": [public_event(row, detail) for row in rows[offset:offset + pageSize]]}

    @app.get("/api/Observability/CollectionHealth")
    async def health_api(context=Depends(query_context)):
        if not context["admin"]:
            reject("observation_health_forbidden", "仅管理员可读取采集进程诊断", 403)
        # 进程健康不是 tenant 业务数据，避免泄漏其他租户的活动计数。
        health = obs.collector.health()
        langfuse_health = langfuse_query.exporter_health(context, obs.langfuse_exporter.health)
        return {**{key: health[key] for key in ("running", "scope", "durableLossAccounting",
                                               "crashLoss", "retentionDays")},
                "queueCapacity": health["queueCapacity"], "counters": None, "queueDepth": None,
                "langfuse": langfuse_health,
                "lastWrittenAt": None, "diagnosticSource": "tenant_scoped_counters_not_provided",
                "message": "当前进程全局计数未按租户隔离，未向租户查询暴露；崩溃丢失不可测"}
