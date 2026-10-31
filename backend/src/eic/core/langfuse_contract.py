"""本地已提交观测到 Langfuse legacy ingestion 的无副作用映射。"""
from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
import hashlib
import json
import math
import re
import uuid


__all__ = ["build_ingestion_events", "trace_identity"]

_SOURCES = {
    "chat_messages": {"request"},
    "task_runs": {"agent_run"},
    "knowledge_ingestion_runs": {"agent_run"},
    "knowledge_pipeline": {"agent_run"},
    "chat_pipeline": {"sse"},
    "agent_runs": {"agent_run", "approval", "budget", "error"},
    "agent_events": {"approval", "budget", "error"},
    "provider_stream": {"model_call", "budget", "error"},
    "provider_response": {"model_call"},
    "knowledge_ops": {"rag_retrieval"},
    "memory_ops": {"agent_run", "rag_retrieval"},
    "memory_pipeline": {"agent_run"},
    "tool_audits": {"tool_call"},
    "chat_events": {"sse"},
    "sse_subscription": {"sse"},
    "http": {"request"},
}
_STATUSES = {
    "started", "pending", "queued", "running", "waiting_approval", "recorded",
    "success", "completed", "closed", "failed", "error", "timeout", "denied",
    "cancelled", "canceled", "unknown", "retrying", "uncertain",
}
_ERRORS = {"failed", "error", "timeout", "denied"}
_CANCELLED = {"cancelled", "canceled"}
_TERMINAL = _ERRORS | _CANCELLED | {"success", "completed", "closed"}
_MEMORY_TERMINAL = {"active", "invalidated", "forgotten", "rejected"}
_INGESTION_STATUSES = {"pending", "parsing", "parsed", "embedding", "staged",
                       "ready", "failed", "uncertain", "superseded", "cancelled",
                       "seeded_pending_embedding"}
_CONFIG = re.compile(r"[a-z0-9][a-z0-9_-]{0,39}\Z")
_SPAN = re.compile(r"(?:[0-9a-fA-F]{32}|[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12})\Z")
_PROMPT_ID = re.compile(r"eic-prompt:([1-9][0-9]{0,18})\Z")
_PROMPT_STATUSES = {"none", "id_only", "multiple_unversioned", "unavailable"}
_COUNTS = {"resultCount", "topK", "denseCount", "sparseCount", "afterSequence",
           "replayedEvents", "duplicateEvents", "httpStatus", "candidateCount",
           "queryEmbeddingCount", "summaryVersion", "indexedDimensions",
           "adapterChunkCount", "textBatchCount", "guardCheckCount", "heartbeatCount"}
_TIMINGS = {"adapterReadMs", "consumerPauseMs", "adapterFirstTextMs", "guardCheckMs",
            "eventPersistenceMs", "emitWaitMs", "firstTextEmittedMs"}


def _get(row, key):
    return row.get(key) if isinstance(row, Mapping) else getattr(row, key, None)


def _text(value):
    return value if isinstance(value, str) and value.strip() and len(value) <= 200 else None


def _integer(value):
    return value if type(value) is int and 0 <= value <= 2**63 - 1 else None


def _span(value):
    return value if isinstance(value, str) and _SPAN.fullmatch(value) else None


def _scope(row, environment, namespace):
    if not all(isinstance(v, str) and _CONFIG.fullmatch(v) for v in (environment, namespace)):
        return None
    tenant, trace = _get(row, "tenant_id"), _text(_get(row, "trace_id"))
    if type(tenant) is not int or tenant <= 0 or trace is None:
        return None
    return [environment, namespace, tenant, trace]


def _identity(scope, domain, *parts):
    raw = json.dumps(["eic-langfuse-v1", *scope, domain, *parts],
                     ensure_ascii=True, separators=(",", ":")).encode("ascii")
    return hashlib.sha256(raw).hexdigest()[:32]


def trace_identity(row, *, environment, namespace) -> str | None:
    """只对具备真实 tenant/trace 的记录返回确定性公共 API ID。"""
    scope = _scope(row, environment, namespace)
    return _identity(scope, "trace") if scope is not None else None


def _utc(value):
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if not isinstance(value, datetime):
        return None
    # 与既有 UTC 存储约定一致，SQLite 可能返回无时区时间。
    try:
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    except (ValueError, OverflowError):
        return None


def _iso(value):
    return value.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _duration(value):
    if type(value) in (int, float) and math.isfinite(value) and 0 <= value <= 31536000000:
        return value
    return None


def _usage(row):
    if _get(row, "usage_source") not in {"provider_usage", "provider_ollama"}:
        return {}
    incoming = _integer(_get(row, "input_tokens"))
    outgoing = _integer(_get(row, "output_tokens"))
    cached = _integer(_get(row, "cached_tokens"))
    total = _integer(_get(row, "total_tokens"))
    result = {}
    # input 与 input_cached 是互斥桶，不能将已包含缓存的 input 再相加。
    if incoming is not None:
        if cached is not None and cached <= incoming:
            result.update(input=incoming - cached, input_cached=cached)
        else:
            result["input"] = incoming
    if outgoing is not None:
        result["output"] = outgoing
    # total 仅作为提供方报告值留在 metadata，不是另一个可累加桶。
    # 只有 total 的提供方响应仍保留真实总量，不补造输入输出拆分。
    if not result and total is not None:
        result["total"] = total
    return result


def _metadata(row, category, source, status):
    result = {"category": category, "source": source, "status": status}
    for key in ("attempt", "source_sequence"):
        value = _integer(_get(row, key))
        if value is not None:
            result[key] = value
    summary = _get(row, "summary")
    if isinstance(summary, Mapping):
        for key in _COUNTS:
            value = _integer(summary.get(key))
            if value is not None and (key != "httpStatus" or 100 <= value <= 599):
                result[key] = value
        for key in _TIMINGS:
            value = _duration(summary.get(key))
            if value is not None:
                result[key] = value
        for key in ("denseStatus", "rerankStatus"):
            value = summary.get(key)
            if isinstance(value, str) and value in {"ok", "skipped", "not_configured", "unavailable", "reindex_required"}:
                result[key] = value
    if category == "model_call":
        usage_source = _get(row, "usage_source")
        if usage_source in {"provider_usage", "provider_ollama", "not_provided"}:
            result["usage_source"] = usage_source
        if usage_source in {"provider_usage", "provider_ollama"}:
            total = _integer(_get(row, "total_tokens"))
            if total is not None:
                result["provider_total_tokens"] = total
    return result


def _prompt_metadata(row, scope):
    # 授权由主的可信 loader 完成；格式检查本身不提供授权证明。
    summary = _get(row, "summary")
    if not isinstance(summary, Mapping):
        return {}
    status = summary.get("promptIdentityStatus")
    if not isinstance(status, str) or status not in _PROMPT_STATUSES:
        # 包括 release_manifest：本轮没有可信版本清单，不能采信自报版本。
        return {"promptIdentityStatus": "unavailable"}
    if status != "id_only":
        return {"promptIdentityStatus": status}
    template = summary.get("promptTemplateId")
    match = _PROMPT_ID.fullmatch(template) if isinstance(template, str) else None
    if match is None or not 0 < int(match.group(1)) <= 2**63 - 1:
        return {"promptIdentityStatus": "unavailable"}
    return {"promptIdentityStatus": "id_only",
            "prompt_template_key": _identity(scope[:3], "association:prompt_template", template)}


def build_ingestion_events(row, *, environment, namespace) -> list[dict]:
    """接受提交后的 dict 或已加载行；不访问 DB、配置、时钟或网络。"""
    scope = _scope(row, environment, namespace)
    if scope is None:
        return []
    category, source = _get(row, "category"), _get(row, "source")
    if not isinstance(source, str) or not isinstance(category, str) or category not in _SOURCES.get(source, ()):
        return []
    stage = _get(row, "stage")
    if source == "knowledge_pipeline" and stage not in {"document_parse", "document_chunking"}:
        return []
    if source == "memory_pipeline" and stage not in {"memory_index", "memory_index_recovery"}:
        return []
    if source == "chat_pipeline" and stage != "stream_delivery":
        return []
    if source == "knowledge_ops" and stage not in {
            "retrieval", "authorize", "dense_recall", "sparse_recall", "fusion", "rerank", "citations"}:
        return []
    if source == "memory_ops" and stage not in {"memory_recall", "memory_save", "memory_invalidation",
            "memory_index", "memory_candidates", "memory_confirmation", "context_compaction", "memory_extraction"}:
        return []
    if category == "model_call":
        valid_stages = {"model"} if source == "provider_stream" else {"embedding", "rerank"}
        if not isinstance(stage, str) or stage not in valid_stages:
            return []
    if source == "chat_messages" and stage != "chat_turn":
        return []
    if source == "task_runs" and stage != "scheduled_task":
        return []
    if source == "knowledge_ingestion_runs" and stage != "ingestion":
        return []
    event_key = _text(_get(row, "event_key"))
    occurred = _utc(_get(row, "occurred_at"))
    if event_key is None or occurred is None:
        return []
    trace = _identity(scope, "trace")
    status = _get(row, "status")
    allowed_statuses = (_INGESTION_STATUSES if source == "knowledge_ingestion_runs" else
                        _STATUSES | _MEMORY_TERMINAL if source == "memory_ops" else _STATUSES)
    status = status if isinstance(status, str) and status in allowed_statuses else "unknown"
    span = _span(_get(row, "span_id"))
    parent = _span(_get(row, "parent_span_id"))
    # 没有 span_id 的 AgentRun 真实形状用 run_id；诊断不能覆盖业务 span。
    run = _text(_get(row, "run_id"))
    legacy_run = (run is not None and source in {"agent_runs", "memory_ops"}
                  and _get(row, "trace_id") == uuid.uuid5(uuid.NAMESPACE_URL, run).hex)
    superseded = source == "knowledge_ingestion_runs" and status == "superseded"
    if superseded:
        # 重试替换是后续状态收据，不能覆写原运行的失败/成功 span。
        observation = _identity(scope, "ingestion-state", event_key)
    elif category in {"error", "budget", "approval"}:
        observation = _identity(scope, "diagnostic", source, event_key)
    elif legacy_run and source == "agent_runs" and category == "agent_run":
        # approved 旧根按 run_id 导出，恢复不能因补入 span_id 更换原根身份。
        observation = _identity(scope, "run", run)
    elif span is not None:
        observation = _identity(scope, "observation", span)
    elif category == "agent_run" and run is not None:
        observation = _identity(scope, "run", run)
    else:
        observation = _identity(scope, "source-observation", source, event_key)
    body = {"id": observation, "traceId": trace, "environment": environment,
            "name": "eic." + ("chat_turn" if source == "chat_messages" else
                               "scheduled_task" if source == "task_runs" else
                                "ingestion_state" if superseded else
                                "ingestion" if source == "knowledge_ingestion_runs" else
                                  stage if source in {"memory_ops", "memory_pipeline", "knowledge_pipeline", "knowledge_ops", "chat_pipeline"}
                                or category == "model_call" and stage in {"embedding", "rerank"} else category)}
    if superseded and span is not None:
        body["parentObservationId"] = _identity(scope, "observation", span)
    elif category in {"error", "budget", "approval"} and span is not None:
        body["parentObservationId"] = _identity(scope, "observation", span)
    elif parent is not None and parent != span:
        body["parentObservationId"] = _identity(scope, "observation", parent)
    if (legacy_run and source == "memory_ops" and parent is not None
            and parent == uuid.uuid5(uuid.NAMESPACE_URL,
                f"eic:span:{_get(row, 'tenant_id')}:{_get(row, 'request_id')}").hex):
        body["parentObservationId"] = _identity(scope, "run", run)
    if body.get("parentObservationId") == observation:
        body.pop("parentObservationId")
    duration = None if superseded else _duration(_get(row, "duration_ms"))
    terminal = status in _TERMINAL or source == "memory_ops" and status in _MEMORY_TERMINAL
    if source in {"memory_pipeline", "agent_runs"} and status == "uncertain" and duration is not None:
        # 持久生命周期已收敛且有真实时间区间，上游结果仍保持未知状态。
        terminal = True
    recovered = (source == "agent_runs" and category == "agent_run"
                 and _get(row, "error_code") == f"context_job_recovered_{status}"
                 and status in {"completed", "uncertain"})
    if recovered:
        terminal = duration is not None
    if source == "knowledge_ingestion_runs":
        # parsed 既可能等待 embedding，也可能为已激活的词法索引终态。
        terminal = terminal or status == "superseded" or (status in {"ready", "parsed"} and duration is not None)
    try:
        if superseded:
            body["startTime"] = _iso(occurred)
        elif (source in {"provider_stream", "provider_response", "knowledge_ops", "memory_ops"} and category in {"model_call", "rag_retrieval"}
                or source in {"knowledge_pipeline", "memory_pipeline"} and category == "agent_run"
                or source == "chat_pipeline" and category == "sse"):
            body["startTime"] = _iso(occurred)
            if terminal and duration is not None:
                body["endTime"] = _iso(occurred + timedelta(milliseconds=duration))
        elif source == "memory_ops" and category == "agent_run":
            # 业务收据的发生时间是提交/失效时间，不能冒充模型调用开始时间。
            body["endTime" if terminal else "startTime"] = _iso(occurred)
        elif source in {"chat_messages", "agent_runs", "task_runs", "knowledge_ingestion_runs"} and category in {"request", "agent_run"}:
            if terminal and duration is not None:
                body["startTime"] = _iso(occurred - timedelta(milliseconds=duration))
                body["endTime"] = _iso(occurred)
            elif source in {"task_runs", "knowledge_ingestion_runs"} and status == "uncertain":
                # occurred_at 可能是 finished_at，不能冒充开始或成功结束。
                pass
            elif status == "started" or (category == "agent_run" and not terminal):
                body["startTime"] = _iso(occurred)
            elif terminal and source == "chat_messages":
                body["endTime"] = _iso(occurred)
        else:
            # 审计 created_at / 订阅 finally 的时间不是可靠调用开始时间。
            body["startTime"] = _iso(occurred)
    except (OverflowError, ValueError):
        return []
    if terminal:
        body["level"] = "ERROR" if status in _ERRORS else "WARNING" if status in _CANCELLED or status in {"superseded", "uncertain"} else "DEFAULT"
        body["statusMessage"] = status
    elif source in {"task_runs", "knowledge_ingestion_runs"} and status == "uncertain":
        body["level"] = "WARNING"
        body["statusMessage"] = "uncertain"
    body["metadata"] = _metadata(row, category, source, status)
    if source == "memory_pipeline":
        if stage == "memory_index_recovery":
            body["metadata"].update(start_time_missing=True, parent_graph_complete=False,
                                    completion_time_source="recovery_convergence")
        else:
            body["metadata"]["span_scope"] = "task_lifecycle"
            if _get(row, "error_code") == "memory_index_lifecycle_recovered":
                body["metadata"]["includes_recovery_wait"] = True
    if recovered:
        body["metadata"]["recovery_time"] = _iso(occurred)
        body["metadata"]["completion_time_source"] = "recovery_convergence"
        body["metadata"]["includes_recovery_wait"] = True
        body["metadata"]["original_execution_end_unknown"] = True
        if run is not None and _get(row, "trace_id") == uuid.uuid5(uuid.NAMESPACE_URL, run).hex:
            body["metadata"]["legacy_trace_preserved"] = True
            body["metadata"]["historical_parent_graph_verified"] = False
    for field in ("chat_id", "agent_id", "model_id", "tool_name", "request_id", "run_id"):
        value = _text(_get(row, field))
        if value is not None:
            body["metadata"][field + "_key"] = _identity(scope[:3], "association:" + field, value)
    owner = _get(row, "owner_key")
    user = (_identity(scope[:3], "user", owner) if isinstance(owner, str)
            and re.fullmatch(r"[0-9a-f]{64}", owner) else None)
    if user is not None:
        body["metadata"]["user_key"] = user
    if span is None:
        body["metadata"]["span_link_missing"] = True
    if "parentObservationId" not in body and source not in {"chat_messages", "task_runs", "knowledge_ingestion_runs"}:
        body["metadata"]["parent_link_missing"] = True
    if category == "model_call" and stage in {"model", "embedding", "rerank"}:
        body["metadata"]["model_kind"] = stage
    if duration is not None:
        body["metadata"]["duration_ms"] = duration
    kind = "generation-create" if category == "model_call" else "span-create"
    if category == "model_call":
        body["metadata"].update(_prompt_metadata(row, scope))
        summary = _get(row, "summary")
        model_name = summary.get("modelName") if isinstance(summary, Mapping) else None
        if isinstance(model_name, str) and re.fullmatch(r"[A-Za-z0-9_./:{}-]{1,120}", model_name):
            body["model"] = model_name
        usage = _usage(row)
        if usage:
            body["usageDetails"] = usage

    def envelope(kind, payload):
        # BaseEvent 文档要求 UUID v4；确定性哈希作为字节来源，固定版本/variant。
        digest = _identity(scope, "event", source, category, event_key, kind)
        return {"id": str(uuid.UUID(hex=digest, version=4)), "type": kind,
                "timestamp": _iso(occurred), "body": payload}

    # 每次 upsert 都只写相同不变量。绝不以子事件时间、状态、metadata
    # 覆盖轮次属性；生命周期完整保存在 chat_turn observation。
    trace_body = {"id": trace, "name": "eic.turn", "environment": environment}
    # 只有轮次权威源能补充身份。缺字段时省略，绝不发送 null/空值清除。
    if source == "chat_messages":
        chat = _text(_get(row, "chat_id"))
        if chat is not None:
            trace_body["sessionId"] = _identity(scope[:3], "association:chat_id", chat)
        if user is not None:
            trace_body["userId"] = user
    return [envelope("trace-create", trace_body), envelope(kind, body)]
