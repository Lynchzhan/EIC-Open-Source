"""复用现有运行表的审批状态机；参数和续跑上下文只以加密信封持久化。"""
from __future__ import annotations

import json
import hashlib
import inspect
from typing import Any

from fastapi import HTTPException
from sqlalchemy import and_, func, or_, select

from eic.core.agent_runtime import digest_payload, runtime_store, utcnow
from eic.core.generation import database_now
from eic.core.ids import new_id
from eic.core.security import decrypt_secret, encrypt_secret
from eic.models import AgentRun, AgentRunLease, ChatEvent, ChatMessage, GenerationLease

APPROVAL_TTL_SECONDS = 900


def conflict(code: str, message: str = "审批状态已变化，请刷新后复核") -> HTTPException:
    return HTTPException(409, detail={"code": code, "errMsg": message})


def seal(value: dict) -> str:
    return encrypt_secret(json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False))


def unseal(value: str) -> dict:
    try:
        result = json.loads(decrypt_secret(value))
        if not isinstance(result, dict):
            raise ValueError("invalid envelope")
        return result
    except Exception as exc:
        raise conflict("approval_envelope_invalid", "审批快照校验失败，未执行工具") from exc


def definition_digest(definition) -> str:
    try:
        implementation = hashlib.sha256(inspect.getsource(definition.handler).encode()).hexdigest()
    except (OSError, TypeError):
        implementation = None
    return digest_payload({
        "name": definition.name, "schema": definition.input_schema,
        "timeout": definition.timeout_ms, "maxOutput": definition.max_output_length,
        "requiresApproval": definition.requires_approval, "idempotent": definition.idempotent,
        "type": definition.tool_type,
        "handler": f"{definition.handler.__module__}.{definition.handler.__qualname__}",
        "implementation": implementation,
    }, redact=False)


async def emit(session, run: AgentRun, kind: str, text: str, **payload) -> int:
    sequence = (await session.scalar(select(func.max(ChatEvent.sequence)).where(
        ChatEvent.tenant_id == run.tenant_id, ChatEvent.request_id == run.request_id,
    )) or 0) + 1
    session.add(ChatEvent(
        id=await new_id(session), tenant_id=run.tenant_id, request_id=run.request_id,
        chat_id=int(run.request_id) if run.request_id.isdigit() else 0, sequence=sequence,
        event_type=kind, payload=json.dumps({"text": text, **payload}, ensure_ascii=False),
    ))
    await session.flush()
    return sequence


async def pause(session, run: AgentRun, message: ChatMessage, *, tool, arguments: dict,
                call_id: str, envelope: dict, ttl: int = APPROVAL_TTL_SECONDS) -> dict:
    if await runtime_store.get_tool_call(session, run_id=run.run_id, call_id=call_id):
        raise conflict("duplicate_tool_call_id")
    await runtime_store.append_tool_call(
        session, run_id=run.run_id, call_id=call_id, tool_name=tool.name,
        arguments=arguments, status="approval_required", timeout_ms=tool.timeout_ms,
        effect_status="none",
    )
    now = await database_now(session)
    cp = await runtime_store.append_checkpoint(
        session, run_id=run.run_id, node_name="waiting_approval",
        state={
            "approvalVersion": 1, "graphVersion": run.graph_version,
            "callId": call_id, "toolName": tool.name,
            "inputDigest": digest_payload(arguments, redact=False),
            "definitionDigest": definition_digest(tool),
            "expiresAt": int(now.timestamp()) + ttl,
            "approvalEnvelope": seal({**envelope, "arguments": arguments}),
            "providerStarted": False, "providerOutcome": "known",
        }, resume_policy="manual_review",
    )
    run.status = message.status = "waiting_approval"
    message.token_usage = {**(message.token_usage or {}), "reason": "approval_required"}
    # producer 的 finally 才关闭租约；暂停事务仍需要持有有效 fencing。
    await runtime_store.append_event(
        session, run_id=run.run_id, node_name="approval", event_type="approval_pending",
        status="waiting_approval", payload={"callId": call_id},
    )
    await emit(session, run, "aIToolsContentMsg", f"工具 {tool.name} 等待人工确认。",
               callId=call_id, toolName=tool.name, status="approval_required")
    sequence = await emit(session, run, "done", "", status="waiting_approval",
                          reason="approval_required", retryable=False)
    run.run_metadata = {**(run.run_metadata or {}), "streamAfterSequence": sequence}
    # 序号不改变 checkpoint 身份；仅密封续跑数据，不输出提示词或参数。
    cp.state_json = {**cp.state_json, "resumeAfterSequence": sequence}
    cp.state_hash = digest_payload(cp.state_json)
    await session.flush()
    return summary(run, cp, can_decide=True)


def summary(run, cp, *, can_decide: bool, now: int | None = None) -> dict:
    state = cp.state_json
    expires = state["expiresAt"]
    expired = now is not None and now >= expires
    return {
        "requestId": run.request_id, "runId": run.run_id,
        "callId": state["callId"], "toolName": state["toolName"],
        "inputDigest": state["inputDigest"], "checkpointSeq": cp.checkpoint_seq,
        "expiresAt": expires, "status": "expired" if expired else run.status,
        "canDecide": can_decide and not expired and run.status == "waiting_approval",
        "canContinue": can_decide and not expired and run.status == "approval_approved",
        "inputSummary": "参数已加密保存；确认绑定此 SHA-256，不显示原始参数。",
    }


async def pending(session, run) -> Any:
    if run.status not in {"waiting_approval", "approval_approved"}:
        return None
    cp = await runtime_store.latest_checkpoint(session, run_id=run.run_id)
    if cp is None or cp.node_name != "waiting_approval":
        return None
    if cp.state_hash != digest_payload(cp.state_json) or cp.state_schema_version != 1:
        raise conflict("checkpoint_integrity_failed")
    if cp.state_json.get("approvalVersion") != 1 or cp.state_json.get("graphVersion") != run.graph_version:
        raise conflict("checkpoint_version_mismatch")
    return cp


async def require_closed_leases(session, run, message):
    lease = await session.get(GenerationLease, message.id)
    worker = await session.get(AgentRunLease, run.run_id)
    if (lease is not None and lease.state not in {"finished", "cancelled_closed"}
        or worker is not None and worker.state == "active"):
        raise conflict("producer_cleanup_pending", "上一轮仍在清理，请稍后重试；未执行工具")


async def require_ticket(session, run, ticket):
    cp = await pending(session, run)
    if (cp is None or cp.checkpoint_seq != ticket.checkpointSeq
        or cp.state_json["callId"] != ticket.callId
        or cp.state_json["inputDigest"] != ticket.inputDigest):
        raise conflict("approval_identity_conflict")
    if int((await database_now(session)).timestamp()) >= cp.state_json["expiresAt"]:
        raise conflict("approval_expired", "审批已过期，未执行工具")
    call = await runtime_store.get_tool_call(session, run_id=run.run_id, call_id=ticket.callId)
    if (call is None or call.input_digest != ticket.inputDigest or call.effect_status != "none"
        or call.status not in {"approval_required", "approved"}):
        raise conflict("tool_outcome_uncertain", "工具已被消费或副作用待复核，未自动重放")
    return cp, call


def identity(ticket, decision: str) -> str:
    return digest_payload({
        "run": ticket.runId, "call": ticket.callId, "checkpoint": ticket.checkpointSeq,
        "input": ticket.inputDigest, "decision": decision,
    }, redact=False)


async def decide(session, run, message, ticket) -> dict:
    key = digest_payload(ticket.idempotencyKey, redact=False)
    receipts = dict((run.run_metadata or {}).get("approvalReceipts") or {})
    fingerprint = identity(ticket, ticket.decision)
    receipt = receipts.get(key)
    if receipt:
        if receipt["identity"] != fingerprint:
            raise conflict("idempotency_key_conflict")
        return {**receipt["response"], "replayed": True}
    cp, call = await require_ticket(session, run, ticket)
    if run.status != "waiting_approval" or call.status != "approval_required":
        raise conflict("approval_already_decided")
    if ticket.decision == "approve":
        call.status = "approved"
        run.status = "approval_approved"
    else:
        call.status = "rejected"
        call.finished_at = utcnow()
        message.status = "failed"
        message.token_usage = {**(message.token_usage or {}), "reason": "approval_rejected"}
        await runtime_store.finish_run(session, run_id=run.run_id, status="failed",
                                      error_code="approval_rejected")
        await emit(session, run, "done", "", status="failed", reason="approval_rejected",
                   retryable=False)
    await runtime_store.append_event(
        session, run_id=run.run_id, node_name="approval", event_type="approval_decision",
        status=call.status, payload={"decision": ticket.decision, "callId": call.call_id},
    )
    response = {"status": call.status, "runId": run.run_id, "replayed": False,
                "checkpointSeq": cp.checkpoint_seq}
    receipts[key] = {"identity": fingerprint, "response": response}
    run.run_metadata = {**(run.run_metadata or {}), "approvalReceipts": receipts}
    return response


async def consume_intent(session, run, message, ticket, definition):
    """调用方先持有 allocator 写锁，intent 必须在外部调用前 commit。"""
    cp, call = await require_ticket(session, run, ticket)
    if run.status != "approval_approved" or call.status != "approved":
        raise conflict("approval_not_approved", "请先批准此调用；未执行工具")
    await require_closed_leases(session, run, message)
    if definition is None or definition_digest(definition) != cp.state_json["definitionDigest"]:
        raise conflict("tool_definition_changed", "工具定义已变化，请重新发起请求")
    envelope = unseal(cp.state_json["approvalEnvelope"])
    if digest_payload(envelope.get("arguments"), redact=False) != call.input_digest:
        raise conflict("approval_input_changed")
    call.status, call.effect_status = "running", "unknown"
    run.status = message.status = "approval_executing"
    now = await database_now(session)
    run.run_metadata = {
        **(run.run_metadata or {}),
        "approvalExecutionDeadline": int(now.timestamp())
        + max(1, min(definition.timeout_ms, envelope["remainingMs"]) // 1000) + 30,
    }
    await runtime_store.append_checkpoint(
        session, run_id=run.run_id, node_name="approved_tool_pending",
        state={"providerStarted": False, "providerOutcome": "known",
               "pendingCallId": call.call_id, "effectStatus": "unknown"},
        resume_policy="manual_review",
    )
    await runtime_store.append_event(
        session, run_id=run.run_id, node_name="approval", event_type="approval_consumed",
        status="running", payload={"callId": call.call_id, "inputDigest": call.input_digest},
    )
    return envelope


async def fail_uncertain(session, run, message, call, code):
    if call.status != "completed":
        call.status, call.effect_status = "failed", "unknown"
        call.error_code, call.finished_at = code, utcnow()
    message.status = "failed"
    message.token_usage = {**(message.token_usage or {}), "reason": code, "automaticRetry": False}
    await runtime_store.finish_run(session, run_id=run.run_id, status="manual_review",
                                   error_code=code, result={"automaticRetry": False})
    await emit(session, run, "done", "", status="failed", reason=code, retryable=False)


async def recover_uncertain(sessions, *, tenant_id=None, request_id=None):
    """审批 intent 无 generation 活租约；独立期限巡检只收口，不重放外部调用。"""
    count = await recover_interrupted_pause(sessions, tenant_id=tenant_id, request_id=request_id)
    async with sessions() as session:
        query = select(AgentRun.run_id).where(AgentRun.status == "approval_executing")
        if tenant_id is not None:
            query = query.where(AgentRun.tenant_id == tenant_id)
        if request_id is not None:
            query = query.where(AgentRun.request_id == request_id)
        candidates = list(await session.scalars(query.limit(100)))
    for run_id in candidates:
        async with sessions() as session:
            await new_id(session)
            run = await session.get(AgentRun, run_id)
            now = int((await database_now(session)).timestamp())
            deadline = (run.run_metadata or {}).get("approvalExecutionDeadline", 0)
            if run.status != "approval_executing" or deadline > now:
                continue
            message = await session.scalar(select(ChatMessage).where(
                ChatMessage.tenant_id == run.tenant_id, ChatMessage.request_id == run.request_id,
                ChatMessage.role == "user",
            ))
            cp = await runtime_store.latest_checkpoint(session, run_id=run_id)
            call_id = (cp.state_json or {}).get("pendingCallId") if cp else None
            call = await runtime_store.get_tool_call(session, run_id=run_id, call_id=call_id)
            if message is None or call is None:
                continue
            await fail_uncertain(session, run, message, call, "approval_execution_interrupted")
            await session.commit()
            count += 1
    return count


async def recover_interrupted_pause(sessions, *, tenant_id=None, request_id=None):
    """失效租约不是清理证明：撤销旧 producer，转人工复核，绝不恢复工具。"""
    def candidates(now):
        query = (select(AgentRun).join(ChatMessage, and_(
            ChatMessage.tenant_id == AgentRun.tenant_id,
            ChatMessage.request_id == AgentRun.request_id, ChatMessage.role == 'user'))
            .outerjoin(GenerationLease, GenerationLease.message_id == ChatMessage.id)
            .outerjoin(AgentRunLease, AgentRunLease.run_id == AgentRun.run_id)
            .where(AgentRun.status.in_(['waiting_approval', 'approval_approved']),
                or_(GenerationLease.message_id.is_(None),
                    GenerationLease.state.in_(['finished', 'cancelled_closed']),
                    GenerationLease.expires_at <= now),
                or_(AgentRunLease.run_id.is_(None), AgentRunLease.state != 'active',
                    AgentRunLease.expires_at <= now),
                or_(and_(GenerationLease.state.notin_(['finished', 'cancelled_closed']),
                         GenerationLease.expires_at <= now),
                    and_(AgentRunLease.state == 'active', AgentRunLease.expires_at <= now))))
        if tenant_id is not None:
            query = query.where(AgentRun.tenant_id == tenant_id)
        if request_id is not None:
            query = query.where(AgentRun.request_id == request_id)
        return query

    async with sessions() as session:
        now = await database_now(session)
        run_ids = [run.run_id for run in await session.scalars(candidates(now).limit(100))]
    count = 0
    for run_id in run_ids:
        async with sessions() as session:
            # 与审批消费、heartbeat 和 producer 写入共用 allocator fencing。
            await new_id(session)
            now = await database_now(session)
            run = await session.scalar(candidates(now).where(AgentRun.run_id == run_id))
            if run is None:
                continue
            message = await session.scalar(select(ChatMessage).where(
                ChatMessage.tenant_id == run.tenant_id,
                ChatMessage.request_id == run.request_id, ChatMessage.role == 'user'))
            lease = await session.get(GenerationLease, message.id)
            if lease is not None and lease.state not in {'finished', 'cancelled_closed'}:
                lease.state = 'expired'
            code = 'approval_pause_cleanup_unconfirmed'
            message.status = 'failed'
            message.token_usage = {**(message.token_usage or {}), 'reason': code,
                                   'automaticRetry': False}
            await runtime_store.append_checkpoint(session, run_id=run_id,
                node_name='approval_cleanup_unconfirmed',
                state={'providerOutcome': 'uncertain', 'effectStatus': 'unknown',
                       'automaticRetry': False, 'reason': code}, resume_policy='manual_review')
            await runtime_store.append_event(session, run_id=run_id, node_name='approval',
                event_type='approval_cleanup_unconfirmed', status='manual_review',
                payload={'reason': code, 'automaticRetry': False})
            await runtime_store.finish_run(session, run_id=run_id, status='manual_review',
                error_code=code, result={'automaticRetry': False,
                    'action': 'review_previous_run_then_start_new_request'})
            await emit(session, run, 'done', '暂停后的执行器清理无法确认，请复核旧运行后重新发起请求；未自动续跑工具。',
                       status='failed', reason=code, retryable=False)
            await session.commit()
            count += 1
    return count
