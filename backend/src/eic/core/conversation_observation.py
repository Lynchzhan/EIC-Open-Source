"""会话统计仅引用真实请求、usage 和生命周期时间戳，不读取正文。"""
from collections import defaultdict
from datetime import UTC, datetime

from sqlalchemy import exists, select
from sqlalchemy.orm import load_only

from eic.core import observability as obs
from eic.models import AgentEvent, AgentRun, ChatEvent, ChatMessage, ChatSession, ObservabilityEvent, Resource

FIELDS = {"inputTokens": "input_tokens", "outputTokens": "output_tokens",
          "totalTokens": "total_tokens", "cachedTokens": "cached_tokens"}
TERMINAL = {"completed", "failed", "cancelled", "canceled"}


def live_chat():
    return ~exists(select(Resource.id).where(Resource.id == ChatSession.id,
        Resource.tenant_id == ChatSession.tenant_id, Resource.resource_type == "chats",
        Resource.is_deleted.is_(True)))


def utc(value):
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def usage(calls):
    result = {}
    for key, field in FIELDS.items():
        values = [obs.integer(getattr(row, field)) for row in calls
                  if row.usage_source in {"provider_usage", "provider_ollama"}]
        values = [value for value in values if value is not None]
        result[key] = {"value": sum(values) if values else None, "providedCalls": len(values),
                       "population": len(calls), "complete": bool(calls) and len(values) == len(calls)}
    return result


def approval_wait(events, finish):
    intervals, pending, known, incomplete = [], {}, set(), False
    for event in sorted(events, key=lambda item: utc(item.created_at)):
        key, at = event.run_id, utc(event.created_at)
        if event.event_type == "approval_pending":
            pending.setdefault(key, at)
            known.add(key)
        elif event.event_type in {"approval_consumed", "approval_decision"}:
            # 批准决定结束人工等待；后续执行排队仍计入处理耗时。
            if key in pending:
                intervals.append((pending.pop(key), at))
            elif key not in known:
                incomplete = True
    for at in pending.values():
        if finish is not None:
            intervals.append((at, finish))
        else:
            incomplete = True
    merged = []
    for start, end in sorted(intervals):
        if end < start:
            incomplete = True
            continue
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    return sum((end - start).total_seconds() * 1000 for start, end in merged), not incomplete


def turns(messages, observations, runs, agent_events, chat_events):
    grouped, by_request, run_requests = {}, defaultdict(list), {}
    for message in messages:
        # 同一接受请求只计一轮，续传和同请求的 assistant 不增加轮次。
        key = message.request_id or f"legacy-message:{message.id}"
        grouped.setdefault(key, message)
    for row in observations:
        if row.request_id:
            by_request[row.request_id].append(row)
    for run in runs:
        run_requests[run.run_id] = run.request_id
    approvals, terminals = defaultdict(list), defaultdict(list)
    for event in agent_events:
        approvals[run_requests.get(event.run_id)].append(event)
    for event in chat_events:
        if event.event_type in {"done", "error", "cancelled", "canceled"}:
            terminals[event.request_id].append(event)
    output = []
    for key, message in grouped.items():
        rows = by_request[key]
        calls = {}
        for row in rows:
            if row.category == "model_call" and row.span_id:
                calls[row.span_id] = row
        calls = list(calls.values())
        roots = [row for row in rows if row.stage == "chat_turn" and row.status in TERMINAL]
        root = max(roots, key=lambda row: utc(row.occurred_at), default=None)
        begin, finish, duration, source = utc(message.created_at), None, None, "not_collected"
        if message.status in TERMINAL:
            if root is not None and root.duration_ms is not None:
                finish, duration, source = utc(root.occurred_at), root.duration_ms, "chat_turn"
            elif terminals[key]:
                finish = max(utc(row.created_at) for row in terminals[key])
                duration, source = max(0, (finish - begin).total_seconds() * 1000), "chat_events"
            else:
                endings = [utc(run.finished_at) for run in runs
                           if run.request_id == key and run.finished_at is not None]
                if endings:
                    finish = max(endings)
                    duration, source = max(0, (finish - begin).total_seconds() * 1000), "agent_runs"
        wait, wait_complete = approval_wait(approvals[key], finish)
        # 历史恢复没有完整轮次采集标记，处理时长不冒充完整端到端计量。
        complete = source == "chat_turn" and wait_complete
        measured_usage = usage(calls)
        kind_usage = {kind: usage([row for row in calls if row.stage == kind])
                      for kind in ("model", "embedding", "rerank")}
        if root is None:
            for group in [measured_usage, *kind_usage.values()]:
                for field in group.values():
                    field["complete"] = False
        output.append({"requestId": message.request_id, "messageId": str(message.id),
                       "status": message.status, "startedAt": begin.isoformat(),
                       "finishedAt": finish.isoformat() if finish else None,
                       "traceIds": list(dict.fromkeys([row.trace_id for row in rows if row.trace_id] +
                           [run.trace_id for run in runs if run.request_id == key and run.trace_id])),
                       "usage": measured_usage, "modelCallCount": len(calls),
                       "usageByKind": kind_usage,
                       "durationMs": duration,
                       "processingMs": max(0, duration - wait) if duration is not None and wait_complete else None,
                       "approvalWaitMs": wait if wait_complete else None,
                       "timingSource": source, "timingComplete": complete,
                       "retryCount": sum(row.attempt > 1 for row in calls if obs.integer(row.attempt) is not None),
                       "retryProvidedCalls": sum(obs.integer(row.attempt) is not None for row in calls),
                       "toolCallCount": len({row.span_id or row.event_key for row in rows if row.category == "tool_call"}),
                       "errorCount": sum(row.status in {"error", "failed", "timeout", "denied"} for row in calls)})
    return sorted(output, key=lambda item: (item["startedAt"], item["messageId"]))


def summary(items):
    totals = {}
    for key in FIELDS:
        values = [item["usage"][key] for item in items]
        provided = [item["value"] for item in values if item["value"] is not None]
        totals[key] = {"value": sum(provided) if provided else None,
                       "providedCalls": sum(item["providedCalls"] for item in values),
                       "population": sum(item["population"] for item in values),
                       "complete": bool(items) and all(item["complete"] for item in values)}
    measured = [item["processingMs"] for item in items if item["processingMs"] is not None]
    return {"roundCount": len(items), "usage": totals,
            "processingMs": sum(measured) if measured else None,
            "timingProvidedRounds": len(measured),
            "timingComplete": bool(items) and all(item["timingComplete"] for item in items),
            "modelCallCount": sum(item["modelCallCount"] for item in items),
            "failedRounds": sum(item["status"] == "failed" for item in items),
            "cancelledRounds": sum(item["status"] in {"cancelled", "canceled"} for item in items)}


async def session_data(context, chat_id, fetch, reject):
    principal, session = context["principal"], context["session"]
    criteria = [ChatSession.id == chat_id, ChatSession.tenant_id == principal.tenant_id]
    if context["scope"] == "own":
        criteria.extend([ChatSession.owner_id == principal.user_id, live_chat()])
    chats = await fetch(session, select(ChatSession).where(*criteria))
    if not chats:
        reject("observation_session_not_found", "会话不存在或无权访问", 404)

    async def bounded(statement):
        rows = await fetch(session, statement.limit(10001))
        if len(rows) > 10000:
            reject("observation_session_too_large", "会话记录超过查询上限，请归档或缩小会话")
        return rows

    messages = await bounded(select(ChatMessage).options(load_only(
        ChatMessage.id, ChatMessage.request_id, ChatMessage.status, ChatMessage.created_at)).where(
        ChatMessage.tenant_id == principal.tenant_id, ChatMessage.session_id == chat_id,
        ChatMessage.role == "user").order_by(ChatMessage.created_at, ChatMessage.id))
    rows = await bounded(select(ObservabilityEvent).where(
        ObservabilityEvent.tenant_id == principal.tenant_id, ObservabilityEvent.project == "eic",
        ObservabilityEvent.chat_id == str(chat_id)).order_by(ObservabilityEvent.occurred_at, ObservabilityEvent.event_key))
    runs = await bounded(select(AgentRun).options(load_only(AgentRun.run_id, AgentRun.request_id,
        AgentRun.trace_id, AgentRun.finished_at)).where(
        AgentRun.tenant_id == principal.tenant_id, AgentRun.session_id == chat_id))
    run_ids = select(AgentRun.run_id).where(AgentRun.tenant_id == principal.tenant_id, AgentRun.session_id == chat_id)
    approvals = await bounded(select(AgentEvent).options(load_only(AgentEvent.id, AgentEvent.run_id,
        AgentEvent.event_type, AgentEvent.created_at)).where(AgentEvent.run_id.in_(run_ids),
        AgentEvent.event_type.in_(["approval_pending", "approval_decision", "approval_consumed"])))
    terminal = await bounded(select(ChatEvent).options(load_only(ChatEvent.id, ChatEvent.request_id,
        ChatEvent.event_type, ChatEvent.created_at)).where(ChatEvent.tenant_id == principal.tenant_id,
        ChatEvent.chat_id == chat_id, ChatEvent.event_type.in_(["done", "error", "cancelled", "canceled"])))
    items = turns(messages, rows, runs, approvals, terminal)
    return {"chatId": str(chat_id), "summary": summary(items), "turns": items,
            "coverage": {"scope": "whole_session_available_records", "historicalEstimated": False,
                         "usageRetentionDays": obs.get_settings().observability_retention_days,
                         "unobservedCallsMeasurable": False}}
