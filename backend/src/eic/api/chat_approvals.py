"""chat_approvals 领域；通过显式装配提供跨域依赖。"""
from dataclasses import dataclass
from collections.abc import Callable
from types import SimpleNamespace as _RouteExports
from typing import Any
from eic.api.runtime import RuntimeBindings
from eic.ai.agent_graph import AGENT_GRAPH_VERSION
from eic.models import AgentEvent
from eic.models import AgentRun
from eic.models import AgentToolCall
from eic.api.schemas import ApprovalDecision as ApprovalDecision
from eic.api.schemas import ApprovalTicket as ApprovalTicket
from sqlalchemy.ext.asyncio import AsyncSession
from eic.models import ChatMessage
from fastapi import Depends
from fastapi import HTTPException
from eic.models import KnowledgeBase
from eic.models import KnowledgeChunk
from eic.models import KnowledgeDocument
from eic.core.authentication import Principal
from fastapi import Query
from eic.models import Resource
from eic.core.runtime_refs import RuntimeReferenceUnavailable
from eic.ai.tool_center import ToolApprovalGrant
from eic.ai.tool_center import ToolExecutionError
from eic.models import User
from eic.core import agent_approval
from eic.core import agent_runtime
from contextlib import asynccontextmanager
from eic.ai import attachments as chat_attachments
from eic.ai import chat_mcp_tools
from eic.ai.stream_reader import complete_cleanup
from eic.ai.context_policy import context_settings
from eic.ai import conversation_context
from eic.core import generation
from eic.core.database import get_session
from eic.ai.memory_context import memory_snapshot_valid
from eic.core.runtime_refs import require_runtime_model
from eic.core.runtime_refs import require_runtime_resource
from sqlalchemy import select
import time
from eic.ai.reliable_calls import validate_tool_arguments


@dataclass(frozen=True)
class Dependencies:
    authorize_chat_mcp_resource: Callable[..., Any]
    chat_skill_callable: Callable[..., Any]
    ensure_permission: Callable[..., Any]
    finish_approved_tool: Callable[..., Any]
    first_int: Callable[..., Any]
    has_permission: Callable[..., Any]
    load_chat_skill_packages: Callable[..., Any]
    load_chat_tool_names: Callable[..., Any]
    load_prompt_contents: Callable[..., Any]
    new_id: Callable[..., Any]
    normalize_chat_files: Callable[..., Any]
    reliable_tool: Callable[..., Any]
    require_owned_chat_request: Callable[..., Any]
    require_runtime_chat_permission: Callable[..., Any]
    restore_mcp_bindings: Callable[..., Any]
    runtime_app_config: Callable[..., Any]
    skill_tool_name: Callable[..., Any]


def bind_routes(app, *, dependencies: Dependencies, runtime: RuntimeBindings, current_principal):
    def approval_model_digest(model):
        return agent_runtime.digest_payload({
            key: getattr(model, key) for key in (
                "id", "tenant_id", "provider", "model_name", "endpoint", "enabled",
                "secret_encrypted", "max_input_tokens", "max_output_tokens", "chat_parameter_profile", "reasoning_policy",
            )
        }, redact=False)


    async def make_approval_snapshot(session, principal, run, model, app_id, config,
                                     prompt_ids, prompts, attachment_text, attachments,
                                     memory_rows, kms_ids, allowed, *, history_snapshot=None):
        resource, stored = await dependencies.require_owned_chat_request(session, principal, run.request_id)
        if history_snapshot is None and context_settings(config)["shortTermEnabled"]:
            _, summary, history_info = await conversation_context.load_history(session,
                tenant_id=principal.tenant_id, owner_id=principal.user_id, session_id=stored.session_id,
                before_id=stored.id, config=config)
            history_snapshot = {"history": history_info, "summary": summary}
        history_snapshot = history_snapshot or {"history": {}, "summary": None}
        return {
            "tenantId": principal.tenant_id, "ownerId": principal.user_id, "runId": run.run_id,
            "requestId": run.request_id, "sessionId": stored.session_id, "appId": app_id,
            "messageDigest": agent_runtime.digest_payload(
                {"data": resource.data if resource else {}, "content": stored.content}, redact=False),
            "appDigest": agent_runtime.digest_payload(config, redact=False),
            "promptIds": prompt_ids, "promptDigest": agent_runtime.digest_payload(prompts, redact=False),
            "attachmentDigest": agent_runtime.digest_payload([attachment_text, attachments], redact=False),
            "memoryRows": memory_rows, "kmsIds": kms_ids, "allowedTools": sorted(allowed),
            "historySnapshot": history_snapshot,
            "modelId": model.id, "modelDigest": approval_model_digest(model),
        }


    async def validate_approval_context(session, principal, run, envelope):
        """密封快照不是权限通行证；批准和执行前均重新读取当前授权对象。"""
        await dependencies.require_runtime_chat_permission(session, principal)
        await dependencies.ensure_permission("tool.execute", principal, session)
        resource, stored = await dependencies.require_owned_chat_request(session, principal, run.request_id)
        snapshot = envelope["snapshot"]
        if (snapshot["tenantId"], snapshot["ownerId"], snapshot["runId"],
            snapshot["requestId"], snapshot["sessionId"]) != (
            principal.tenant_id, principal.user_id, run.run_id, run.request_id, stored.session_id):
            raise agent_approval.conflict("approval_owner_changed")
        data = resource.data if resource else {}
        await dependencies.normalize_chat_files(data or {}, principal, session)
        approval_attachment_ids = await chat_attachments.conversation_file_ids(
            (data or {}).get('fileIds') or [], principal, session, stored.session_id, before_message_id=stored.id,
            max_files=runtime.settings.chat_attachment_max_files)
        if approval_attachment_ids:
            await dependencies.ensure_permission('file.read', principal, session)
        text_value, attachment_snapshot = await chat_attachments.load_context(
            approval_attachment_ids, principal, session, runtime.settings)
        config = await dependencies.runtime_app_config(session, principal.tenant_id, snapshot["appId"])
        model = await require_runtime_model(session, principal.tenant_id, snapshot["modelId"], "chat")
        allowed = await dependencies.load_chat_tool_names(session, principal, snapshot["appId"], config)
        bindings = dependencies.restore_mcp_bindings(envelope.get("mcpSnapshots", []))
        definitions = await chat_mcp_tools.revalidate_chat_mcp_snapshots(
            bindings, app_data=config, tenant_id=principal.tenant_id,
            authorize_resource=lambda resource_id: dependencies.authorize_chat_mcp_resource(
                session, principal, snapshot["appId"], config, resource_id))
        allowed.update(spec.name for spec in definitions)
        packages = await dependencies.load_chat_skill_packages(session, principal, snapshot["appId"], config)
        if {str(key): value.content_hash for key, value in packages.items()} != envelope.get("skillHashes", {}):
            raise agent_approval.conflict("approval_context_changed")
        allowed.update(dependencies.skill_tool_name(identifier, package) for identifier, package in packages.items()
                       if dependencies.chat_skill_callable(package, allowed, definitions))
        actual = await make_approval_snapshot(
            session, principal, run, model, snapshot["appId"], config, snapshot["promptIds"],
            await dependencies.load_prompt_contents(session, principal.tenant_id, snapshot["promptIds"]),
            text_value, attachment_snapshot, snapshot["memoryRows"], snapshot["kmsIds"], allowed,
        )
        if actual != snapshot or not await memory_snapshot_valid(
            session, snapshot["memoryRows"], tenant_id=principal.tenant_id, owner_id=principal.user_id,
            session_id=stored.session_id, agent_id=dependencies.first_int(config.get("agentId")),
        ):
            raise agent_approval.conflict("approval_context_changed", "上下文或权限已变化，未执行工具")
        for base_id in snapshot["kmsIds"]:
            await require_runtime_resource(session, principal.tenant_id, base_id, "knowledge_bases",
                                           KnowledgeBase, require_dedicated=True)
        # 复核保存证据仍可见且正文未改变；不再调用检索 provider。
        for row in envelope["plan"]["retrieved_documents"]:
            chunk_id = dependencies.first_int(row.get("chunkId"))
            chunk = await session.get(KnowledgeChunk, chunk_id) if chunk_id is not None else None
            document = await session.get(KnowledgeDocument, chunk.document_id) if chunk else None
            if (chunk is None or chunk.tenant_id != principal.tenant_id
                or document is None or document.tenant_id != principal.tenant_id
                or document.is_deleted or document.status not in {"parsed", "ready"}
                or document.active_source_revision != document.source_revision
                or chunk.index_version != document.active_index_version
                or chunk.knowledge_base_id not in snapshot["kmsIds"]
                or str(document.id) != str(row.get("documentId"))
                or chunk.content != row.get("childContent", row.get("content"))):
                raise agent_approval.conflict("approval_evidence_changed")
            parent = await session.get(KnowledgeChunk, chunk.parent_chunk_id) if chunk.parent_chunk_id else None
            if (row.get("parentContent") and (parent is None
                or parent.tenant_id != principal.tenant_id or parent.document_id != document.id
                or parent.index_version != chunk.index_version
                or parent.content != row["parentContent"])
                or row.get("parentFilled") and row.get("content") != row.get("parentContent")):
                raise agent_approval.conflict("approval_evidence_changed")
            if document.file_id:
                file = await session.get(Resource, document.file_id)
                # 与检索层同一共享文件绑定规则；删除或撤销共享后不得继续旧证据。
                from eic.core.file_access import can_bind_shared_file
                if not can_bind_shared_file(file, principal.tenant_id):
                    raise agent_approval.conflict("approval_evidence_changed")
        return model, allowed


    async def require_approval_run(session, principal, request_id, run_id=None):
        _, stored = await dependencies.require_owned_chat_request(session, principal, request_id)
        run = await session.scalar(select(AgentRun).where(
            AgentRun.tenant_id == principal.tenant_id, AgentRun.request_id == request_id,
        ))
        if run is None or run.session_id != stored.session_id or (run_id and run.run_id != run_id):
            raise HTTPException(404, detail={"errMsg": "运行记录不存在"})
        if run.graph_version != AGENT_GRAPH_VERSION:
            raise agent_approval.conflict("checkpoint_version_mismatch")
        return run, stored


    @app.get("/api/AIChatHistorys/GetApproval")
    async def chat_get_approval(requestId: str, principal: Principal = Depends(current_principal),
                                session: AsyncSession = Depends(get_session)):
        await dependencies.ensure_permission("ai.chat.read", principal, session)
        run, _ = await require_approval_run(session, principal, requestId)
        cp = await agent_approval.pending(session, run)
        return {"approval": agent_approval.summary(
            run, cp, can_decide=await dependencies.has_permission("ai.chat.write", principal, session)
            and await dependencies.has_permission("tool.execute", principal, session),
            now=int((await generation.database_now(session)).timestamp()),
        ) if cp else None, "status": run.status}


    @asynccontextmanager
    async def approval_write_session():
        """执行前资源撤销统一返回受控错误，事务退出时仍按原规则回滚。"""
        try:
            async with runtime.sessions() as session:
                yield session
        except RuntimeReferenceUnavailable as exc:
            code = str(exc)
            if code == "runtime_authorization_unavailable":
                raise HTTPException(403, detail={"code": code, "errMsg": "当前权限已撤销，未执行工具"}) from exc
            raise agent_approval.conflict(code, "运行资源已变化，未执行工具") from exc


    @app.post("/api/AIChatHistorys/DecideApproval")
    async def chat_decide_approval(ticket: ApprovalDecision,
                                   principal: Principal = Depends(current_principal)):
        async with approval_write_session() as session:
            await dependencies.new_id(session)
            run, stored = await require_approval_run(session, principal, ticket.requestId, ticket.runId)
            await dependencies.require_runtime_chat_permission(session, principal)
            await dependencies.ensure_permission("tool.execute", principal, session)
            cp = await agent_approval.pending(session, run)
            if ticket.decision == "approve" and cp is not None:
                await validate_approval_context(session, principal, run,
                                                agent_approval.unseal(cp.state_json["approvalEnvelope"]))
            result = await agent_approval.decide(session, run, stored, ticket)
            await session.commit()
            return result


    @app.post("/api/AIChatHistorys/ContinueApprovedTool")
    async def chat_continue_approved(ticket: ApprovalTicket,
                                     principal: Principal = Depends(current_principal)):
        key = agent_runtime.digest_payload(ticket.idempotencyKey, redact=False)
        fingerprint = agent_approval.identity(ticket, "consume")
        async with approval_write_session() as session:
            await dependencies.new_id(session)
            run, stored = await require_approval_run(session, principal, ticket.requestId, ticket.runId)
            await dependencies.require_runtime_chat_permission(session, principal)
            await dependencies.ensure_permission("tool.execute", principal, session)
            receipt = ((run.run_metadata or {}).get("consumeReceipts") or {}).get(key)
            if receipt:
                if receipt["identity"] != fingerprint:
                    raise agent_approval.conflict("idempotency_key_conflict")
                return {**receipt["response"], "replayed": True}
            cp, call = await agent_approval.require_ticket(session, run, ticket)
            envelope = agent_approval.unseal(cp.state_json["approvalEnvelope"])
            _, allowed = await validate_approval_context(session, principal, run, envelope)
            definition = runtime.tools.get(call.tool_name)
            validate_tool_arguments(runtime.tools, envelope["arguments"], definition.input_schema if definition else None)
            envelope = await agent_approval.consume_intent(session, run, stored, ticket, definition)
            name, arguments, input_digest = call.tool_name, envelope["arguments"], call.input_digest
            await session.commit()
        started = time.perf_counter()
        execution_key = f"{ticket.runId}:{ticket.callId}"
        stored_id = stored.id
        async def close_unknown(code):
            async with runtime.sessions() as session:
                await dependencies.new_id(session)
                run = await session.get(AgentRun, ticket.runId)
                stored = await session.get(ChatMessage, stored_id)
                call = await agent_runtime.runtime_store.get_tool_call(
                    session, run_id=run.run_id, call_id=ticket.callId)
                if run.status == "approval_executing":
                    await agent_approval.fail_uncertain(session, run, stored, call, code)
                    await session.commit()
        try:
            if envelope["remainingMs"] <= 0:
                raise ToolExecutionError("tool_budget_exceeded", "执行预算已耗尽")
            execution = await dependencies.reliable_tool(
                runtime.tools, name, arguments, allowed, tenant_id=principal.tenant_id, sessions=runtime.sessions,
                idempotency_key=execution_key, timeout_ms=min(definition.timeout_ms, envelope["remainingMs"]),
                approval_grant=ToolApprovalGrant(name, input_digest, execution_key),
            )
            return await dependencies.finish_approved_tool(
                ticket, principal, stored_id, envelope, allowed, definition, execution,
                key, fingerprint, started,
            )
        except BaseException as exc:
            await complete_cleanup(close_unknown(
                exc.code if isinstance(exc, ToolExecutionError) else "tool_outcome_uncertain"))
            if not isinstance(exc, Exception):
                raise
            raise agent_approval.conflict("tool_outcome_uncertain", "工具结果需人工复核；系统未自动重放") from exc


    @app.get("/api/AIChatHistorys/GetTrace")
    async def chat_get_trace(requestId: str, detail: bool = False, afterSequence: int = Query(0, ge=0),
                             limit: int = Query(50, ge=1, le=100),
                             principal: Principal = Depends(current_principal),
                             session: AsyncSession = Depends(get_session)):
        await dependencies.ensure_permission("ai.chat.read", principal, session)
        # 管理员脱敏详情仍限于本租户、本人会话，不隐式授予跨用户访问。
        run, stored = await require_approval_run(session, principal, requestId)
        user = await session.get(User, principal.user_id)
        if detail and (user is None or not user.is_super_admin):
            raise HTTPException(403, detail={"errMsg": "仅管理员可读取脱敏详情"})
        usage = stored.token_usage or {}
        response = {"requestId": requestId, "runId": run.run_id, "traceId": run.trace_id,
                    "status": run.status, "currentNode": run.current_node,
                    "errorCode": run.error_code or None, "eventCount": run.event_seq,
                    "checkpointSeq": run.checkpoint_seq, "usage": {
                        "inputTokens": None, "outputTokens": None, "totalTokens": None,
                        "source": "not_provided"}, "providerInvocations": usage.get("providerInvocations"),
                    "canReadDetail": bool(user and user.is_super_admin)}
        if detail:
            rows = list(await session.scalars(select(AgentEvent).where(
                AgentEvent.run_id == run.run_id, AgentEvent.sequence > afterSequence,
            ).order_by(AgentEvent.sequence).limit(limit + 1)))
            response["events"] = [{"sequence": item.sequence, "node": item.node_name,
                                   "eventType": item.event_type, "status": item.status,
                                   "createdAt": item.created_at.isoformat()} for item in rows[:limit]]
            response["hasMore"] = len(rows) > limit
            response["nextSequence"] = rows[min(len(rows), limit) - 1].sequence if rows else afterSequence
            tools = list(await session.scalars(select(AgentToolCall).where(
                AgentToolCall.run_id == run.run_id).order_by(AgentToolCall.id).limit(100)))
            response["tools"] = [{"callId": item.call_id, "toolName": item.tool_name,
                                  "status": item.status, "effectStatus": item.effect_status,
                                  "inputDigest": item.input_digest, "outputDigest": item.output_digest,
                                  "errorCode": item.error_code or None} for item in tools]
        return response

    return _RouteExports(
        approval_model_digest=approval_model_digest,
        make_approval_snapshot=make_approval_snapshot,
        validate_approval_context=validate_approval_context,
        require_approval_run=require_approval_run,
        chat_get_approval=chat_get_approval,
        approval_write_session=approval_write_session,
        chat_decide_approval=chat_decide_approval,
        chat_continue_approved=chat_continue_approved,
        chat_get_trace=chat_get_trace,
    )
