"""真实审批路由/持久状态机，确定性协议桩不构成真实模型或生产证据。"""
import asyncio
from contextlib import asynccontextmanager
import json
import time

import httpx
import pytest
from sqlalchemy import select

from test_id_generation_safety import fixture_db
from eic import main
from eic.ai.tool_center import ToolCenter, ToolDefinition, ToolExecutionError
from eic.core import agent_approval, agent_runtime, generation
from eic.core.security import encrypt_secret
from eic.models import (
    AIModel, AgentRun, ChatEvent, ChatMessage, ChatSession, GenerationLease,
    KnowledgeBase, KnowledgeChunk, KnowledgeDocument, Resource, Tenant, User,
)

PRINCIPAL = main.Principal(11, 1, "fixture", True)
SENTINEL = "PRIVATE_PROMPT_TOOL_RESULT_NEVER_IN_TRACE"


@asynccontextmanager
async def scenario(monkeypatch, tmp_path, *, pause=True, ttl=900, remaining=None, handler=None):
    calls = []
    center = ToolCenter()
    def write(arguments):
        calls.append(arguments)
        return {"ok": True, "private": SENTINEL}
    definition = ToolDefinition(
        "write_fixture", "审批测试", handler or write,
        input_schema={"type": "object", "properties": {"value": {"type": "integer"}},
                      "required": ["value"], "additionalProperties": False},
        requires_approval=True, timeout_ms=100,
    )
    center.register(definition)
    monkeypatch.setattr(main, "tool_center", center)
    async with fixture_db(monkeypatch, tmp_path, is_super_admin=True) as sessions:
        # 真实 producer 的 LangGraph 必须也绑定同一个隔离库。
        monkeypatch.setattr(main.CHAT_AGENT_GRAPH.checkpointer, "sessions", sessions)
        async with sessions() as session:
            model = AIModel(id=501, tenant_id=1, model_name="fixture", model_type="chat",
                            endpoint="http://fixture.invalid/v1",
                            secret_encrypted=encrypt_secret("fixture-only-key"))
            session.add(model)
            session.add(Resource(id=601, tenant_id=1, owner_id=11, resource_type="skills",
                                 data={"allowedTools": ["write_fixture"], "enabled": True}))
            await session.flush()
            run = await agent_runtime.runtime_store.create_run(
                session, tenant_id=1, session_id=101, request_id="fixture",
                graph_version=main.AGENT_GRAPH_VERSION,
            )
            config = await main.runtime_app_config(session, 1, None)
            allowed = await main.load_chat_tool_names(session, PRINCIPAL, None, config)
            envelope = {
                "snapshot": await main.make_approval_snapshot(
                    session, PRINCIPAL, run, model, None, config, [], [], "", [], [], [],
                    allowed,
                ),
                "skillHashes": {},
                "conversation": [{"role": "user", "content": SENTINEL}],
                "answerParts": [], "attempts": [{"toolCalls": 0}], "budgetMetadata": {},
                "toolRounds": 1, "toolCallCount": 0, "modelSteps": 1,
                "toolResults": {}, "seenCalls": {}, "remainingMs": 10000,
                "plan": {"retrieved_documents": []}, "remainingCalls": remaining or [],
            }
            summary = None
            if pause:
                summary = await agent_approval.pause(
                    session, run, await session.get(ChatMessage, 301),
                    tool=definition, arguments={"value": 1}, call_id="call-1",
                    envelope=envelope, ttl=ttl,
                )
            await session.commit()
        try:
            yield sessions, calls, summary, envelope, definition
        finally:
            await generation.stop_producers()


def ticket(summary, **overrides):
    data = {key: summary[key] for key in (
        "requestId", "runId", "callId", "inputDigest", "checkpointSeq",
    )}
    return main.ApprovalTicket(**{**data, "idempotencyKey": "consume-key-1", **overrides})


async def approve(summary, **overrides):
    data = ticket(summary).model_dump()
    return await main.chat_decide_approval(main.ApprovalDecision(**{
        **data, "decision": "approve", "idempotencyKey": "decision-key-1", **overrides,
    }), PRINCIPAL)


async def state(sessions):
    async with sessions() as session:
        run = await session.scalar(select(AgentRun))
        call = await agent_runtime.runtime_store.get_tool_call(session, run_id=run.run_id, call_id="call-1")
        message = await session.get(ChatMessage, 301)
        return run, call, message


def test_approve_is_not_execution_and_consumption_is_at_most_once(monkeypatch, tmp_path):
    async def run():
        async with scenario(monkeypatch, tmp_path) as (sessions, calls, summary, _, _):
            assert calls == []
            first = await approve(summary)
            assert first["status"] == "approved" and not first["replayed"]
            assert (await approve(summary))["replayed"]
            assert calls == []
            result = await main.chat_continue_approved(ticket(summary), PRINCIPAL)
            replay = await main.chat_continue_approved(ticket(summary), PRINCIPAL)
            assert result["status"] == "running" and result["toolStatus"] == "completed"
            assert replay["replayed"] and calls == [{"value": 1}]
            current, call, message = await state(sessions)
            assert call.status == "completed" and call.effect_status == "applied"
            assert message.status == "streaming"
            assert result["afterEventId"] == "fixture-2"
            async with sessions() as session:
                assert await agent_approval.pending(session, current) is None
    asyncio.run(run())


@pytest.mark.parametrize("mutation", [
    "expired", "digest", "old-checkpoint", "definition", "message", "model", "skill",
    "session-deleted", "user-disabled", "lease-active", "not-approved",
])
def test_revalidation_rejects_stale_or_unapproved_inputs(monkeypatch, tmp_path, mutation):
    async def run():
        async with scenario(monkeypatch, tmp_path, ttl=0 if mutation == "expired" else 900) as (
            sessions, calls, summary, _, definition,
        ):
            if mutation not in {"expired", "not-approved"}:
                await approve(summary)
            current_ticket = ticket(summary)
            if mutation == "digest":
                current_ticket = ticket(summary, inputDigest="0" * 64)
            elif mutation == "old-checkpoint":
                current_ticket = ticket(summary, checkpointSeq=summary["checkpointSeq"] + 1)
            elif mutation == "definition":
                replacement = ToolCenter()
                replacement.register(ToolDefinition(
                    definition.name, definition.description, definition.handler,
                    input_schema=definition.input_schema, requires_approval=True, timeout_ms=101,
                ))
                monkeypatch.setattr(main, "tool_center", replacement)
            else:
                async with sessions() as session:
                    if mutation == "message":
                        (await session.get(ChatMessage, 301)).content = "changed"
                    elif mutation == "model":
                        (await session.get(AIModel, 501)).endpoint = "http://changed.invalid"
                    elif mutation == "skill":
                        (await session.get(Resource, 601)).is_deleted = True
                    elif mutation == "session-deleted":
                        session.add(Resource(id=101, tenant_id=1, owner_id=11,
                                             resource_type="chats", is_deleted=True, data={}))
                    elif mutation == "user-disabled":
                        (await session.get(User, 11)).is_active = False
                    elif mutation == "lease-active":
                        session.add(GenerationLease(message_id=301, tenant_id=1, request_id="fixture",
                                                    state="active", owner_token="old",
                                                    expires_at=agent_runtime.utcnow()))
                    await session.commit()
            with pytest.raises(main.HTTPException):
                await main.chat_continue_approved(current_ticket, PRINCIPAL)
            assert calls == []
    asyncio.run(run())


def test_reject_and_conflicting_idempotency_do_not_execute(monkeypatch, tmp_path):
    async def run():
        async with scenario(monkeypatch, tmp_path) as (sessions, calls, summary, _, _):
            data = ticket(summary).model_dump()
            rejected = main.ApprovalDecision(**{**data, "decision": "reject"})
            assert (await main.chat_decide_approval(rejected, PRINCIPAL))["status"] == "rejected"
            assert (await main.chat_decide_approval(rejected, PRINCIPAL))["replayed"]
            with pytest.raises(main.HTTPException):
                await main.chat_decide_approval(main.ApprovalDecision(**{**data, "decision": "approve"}), PRINCIPAL)
            with pytest.raises(main.HTTPException):
                await main.chat_continue_approved(ticket(summary), PRINCIPAL)
            current, call, message = await state(sessions)
            assert current.status == message.status == "failed" and call.status == "rejected"
            assert calls == []
            async with sessions() as session:
                result = await main.chat_get_approval("fixture", PRINCIPAL, session)
                assert result["approval"] is None
    asyncio.run(run())


def test_concurrent_consumers_only_one_external_effect(monkeypatch, tmp_path):
    async def run():
        async with scenario(monkeypatch, tmp_path) as (_, calls, summary, _, _):
            await approve(summary)
            outcomes = await asyncio.gather(*(
                main.chat_continue_approved(ticket(summary), PRINCIPAL) for _ in range(3)
            ), return_exceptions=True)
            assert calls == [{"value": 1}]
            assert any(isinstance(item, dict) and item["toolStatus"] == "completed" for item in outcomes)
            assert all(isinstance(item, dict) or isinstance(item, main.HTTPException) for item in outcomes)
    asyncio.run(run())


@pytest.mark.parametrize("identity", [main.Principal(22, 1, "other", True),
                                     main.Principal(23, 2, "other-tenant", True)])
def test_cross_user_and_tenant_are_hidden(monkeypatch, tmp_path, identity):
    async def run():
        async with scenario(monkeypatch, tmp_path) as (sessions, calls, summary, _, _):
            async with sessions() as session:
                session.add(Tenant(id=2, code=2, name="other-tenant"))
                await session.flush()
                session.add(User(id=22, tenant_id=1, username="other", password_hash="unused",
                                 is_super_admin=True))
                session.add(User(id=23, tenant_id=2, username="other-tenant", password_hash="unused",
                                 is_super_admin=True))
                await session.commit()
            async with sessions() as session:
                with pytest.raises(main.HTTPException) as exc:
                    await main.chat_get_approval("fixture", identity, session)
                assert exc.value.status_code == 404
            with pytest.raises(main.HTTPException):
                await main.chat_continue_approved(ticket(summary), identity)
            assert calls == []
    asyncio.run(run())


@pytest.mark.parametrize("failure", ["external-error", "timeout", "after-success"])
def test_uncertain_failure_never_replays_and_preserves_known_result(monkeypatch, tmp_path, failure):
    remaining = [{"callId": "call-2", "name": "write_fixture", "arguments": {"value": 2}}]
    def slow(arguments):
        time.sleep(.2)
        return {"ok": True}
    async def run():
        async with scenario(monkeypatch, tmp_path,
                            remaining=remaining if failure == "after-success" else None,
                            handler=slow if failure == "timeout" else None) as (
            sessions, calls, summary, _, _,
        ):
            await approve(summary)
            if failure == "external-error":
                async def fail(*args, **kwargs):
                    calls.append({"value": 1})
                    raise ToolExecutionError("tool_failed", "fixture")
                monkeypatch.setattr(main, "reliable_tool", fail)
            elif failure == "after-success":
                async def fail(*args, **kwargs):
                    raise RuntimeError("fixture continuation persistence failed")
                monkeypatch.setattr(agent_approval, "pause", fail)
            with pytest.raises(main.HTTPException):
                await main.chat_continue_approved(ticket(summary), PRINCIPAL)
            current, call, message = await state(sessions)
            assert current.status == "manual_review" and message.status == "failed"
            assert call.effect_status == ("applied" if failure == "after-success" else "unknown")
            assert call.status == ("completed" if failure == "after-success" else "failed")
            with pytest.raises(main.HTTPException):
                await main.chat_continue_approved(ticket(summary), PRINCIPAL)
            if failure != "timeout":
                assert calls == [{"value": 1}]
    asyncio.run(run())


def test_crash_intent_recovers_to_manual_review_idempotently(monkeypatch, tmp_path):
    async def run():
        async with scenario(monkeypatch, tmp_path) as (sessions, calls, summary, _, definition):
            await approve(summary)
            async with sessions() as session:
                from eic.core.ids import new_id
                await new_id(session)
                current, message = await main.require_approval_run(session, PRINCIPAL, "fixture")
                await agent_approval.consume_intent(session, current, message, ticket(summary), definition)
                current.run_metadata = {**current.run_metadata, "approvalExecutionDeadline": 0}
                await session.commit()
            assert await generation.recover_expired() == 1
            assert await generation.recover_expired() == 0
            current, call, message = await state(sessions)
            assert current.status == "manual_review" and call.effect_status == "unknown"
            assert message.status == "failed" and calls == []
    asyncio.run(run())


@pytest.mark.parametrize("fault", ["duplicate-call", "invalid-arguments", "missing-tool"])
def test_next_call_failure_preserves_applied_result(monkeypatch, tmp_path, fault):
    remaining = [{
        "callId": "call-1" if fault == "duplicate-call" else "call-2",
        "name": "missing" if fault == "missing-tool" else "write_fixture",
        "arguments": {"value": "invalid" if fault == "invalid-arguments" else 2},
    }]
    async def run():
        async with scenario(monkeypatch, tmp_path, remaining=remaining) as (sessions, calls, summary, _, _):
            await approve(summary)
            try:
                response = await main.chat_continue_approved(ticket(summary), PRINCIPAL)
                assert response["status"] == "manual_review"
            except main.HTTPException as exc:
                assert exc.status_code == 409
            current, call, message = await state(sessions)
            assert current.status == "manual_review" and message.status == "failed"
            assert call.status == "completed" and call.effect_status == "applied"
            assert calls == [{"value": 1}]
            if fault == "missing-tool":
                replay = await main.chat_continue_approved(ticket(summary), PRINCIPAL)
                assert replay["replayed"] and replay["status"] == "manual_review"
            else:
                with pytest.raises(main.HTTPException):
                    await main.chat_continue_approved(ticket(summary), PRINCIPAL)
            assert calls == [{"value": 1}]
    asyncio.run(run())


@pytest.mark.parametrize("fault", ["hash", "envelope"])
def test_tampered_checkpoint_never_executes(monkeypatch, tmp_path, fault):
    async def run():
        async with scenario(monkeypatch, tmp_path) as (sessions, calls, summary, _, _):
            await approve(summary)
            async with sessions() as session:
                current, _ = await main.require_approval_run(session, PRINCIPAL, "fixture")
                cp = await agent_runtime.runtime_store.latest_checkpoint(session, run_id=current.run_id)
                cp.state_json = {**cp.state_json, "approvalEnvelope": "tampered"}
                if fault == "envelope":
                    cp.state_hash = agent_runtime.digest_payload(cp.state_json)
                await session.commit()
            with pytest.raises(main.HTTPException) as exc:
                await main.chat_continue_approved(ticket(summary), PRINCIPAL)
            assert exc.value.status_code == 409 and calls == []
    asyncio.run(run())


@pytest.mark.parametrize("endpoint", ["DecideApproval", "ContinueApprovedTool"])
@pytest.mark.parametrize("change", ["user-disabled", "resource-deleted"])
def test_http_revoked_context_is_controlled(monkeypatch, tmp_path, endpoint, change):
    async def run():
        async with scenario(monkeypatch, tmp_path) as (sessions, calls, summary, _, _):
            if endpoint == "ContinueApprovedTool":
                await approve(summary)
            async with sessions() as session:
                if change == "user-disabled":
                    (await session.get(User, 11)).is_active = False
                else:
                    (await session.get(Resource, 601)).is_deleted = True
                await session.commit()
            # 模拟认证与执行前复核之间撤权，而非绕过真实 current_principal。
            main.app.dependency_overrides[main.current_principal] = lambda: PRINCIPAL
            try:
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app),
                                            base_url="http://fixture") as client:
                    payload = ticket(summary).model_dump()
                    if endpoint == "DecideApproval":
                        payload["decision"] = "approve"
                    response = await client.post(f"/api/AIChatHistorys/{endpoint}", json=payload)
                    assert response.status_code == (403 if change == "user-disabled" else 409)
                    assert response.json()["detail"]["code"] == (
                        "runtime_authorization_unavailable" if change == "user-disabled"
                        else "approval_context_changed")
                    assert calls == []
            finally:
                main.app.dependency_overrides.clear()
    asyncio.run(run())


def test_trace_whitelist_usage_null_and_http_contract(monkeypatch, tmp_path):
    async def run():
        async with scenario(monkeypatch, tmp_path) as (sessions, _, summary, _, _):
            async with sessions() as session:
                current, _ = await main.require_approval_run(session, PRINCIPAL, "fixture")
                await agent_runtime.runtime_store.append_event(
                    session, run_id=current.run_id, node_name="test", event_type="private",
                    payload={"raw": SENTINEL, "prompt": SENTINEL, "output": SENTINEL},
                )
                await session.commit()
            async def dependency_session():
                async with sessions() as session:
                    yield session
            main.app.dependency_overrides[main.current_principal] = lambda: PRINCIPAL
            main.app.dependency_overrides[main.get_session] = dependency_session
            try:
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app),
                                            base_url="http://fixture") as client:
                    response = await client.get("/api/AIChatHistorys/GetTrace",
                                                params={"requestId": "fixture", "detail": "true", "limit": 1})
                    assert response.status_code == 200
                    value = response.json().get("data", response.json())
                    assert value["usage"]["totalTokens"] is None
                    assert SENTINEL not in response.text and "approvalEnvelope" not in response.text
                    assert value["hasMore"] and value["nextSequence"] == 1
                    assert (await client.get("/api/AIChatHistorys/GetTrace",
                        params={"requestId": "fixture", "detail": "true", "limit": 101})).status_code == 422
                    invalid = {**ticket(summary).model_dump(), "decision": "approve", "extra": True}
                    assert (await client.post("/api/AIChatHistorys/DecideApproval", json=invalid)).status_code == 422
                    async with sessions() as session:
                        (await session.get(User, 11)).is_super_admin = False
                        await session.commit()
                    response = await client.get("/api/AIChatHistorys/GetTrace",
                                                params={"requestId": "fixture", "detail": "true"})
                    assert response.status_code == 403
            finally:
                main.app.dependency_overrides.clear()
    asyncio.run(run())


@pytest.mark.parametrize("change", ["none", "child", "parent", "deleted", "revision", "index", "file"])
def test_rag_revalidation_checks_parent_version_and_shared_file(monkeypatch, tmp_path, change):
    async def run():
        async with scenario(monkeypatch, tmp_path) as (sessions, _, _, envelope, _):
            async with sessions() as session:
                session.add(KnowledgeBase(id=701, tenant_id=1, name="fixture"))
                session.add(Resource(id=701, tenant_id=1, owner_id=11,
                                     resource_type="knowledge_bases", data={}))
                await session.flush()
                session.add(KnowledgeDocument(id=702, tenant_id=1, knowledge_base_id=701,
                                              source_name="fixture", status="parsed",
                                              active_index_version=5, source_revision=1,
                                              active_source_revision=1))
                await session.flush()
                session.add_all([
                    KnowledgeChunk(id=703, tenant_id=1, knowledge_base_id=701, document_id=702,
                                   chunk_index=-1, index_version=5, content="parent", content_hash="hash"),
                    KnowledgeChunk(id=704, tenant_id=1, knowledge_base_id=701, document_id=702,
                                   parent_chunk_id=703, chunk_index=0, index_version=5,
                                   content="child", content_hash="hash"),
                ])
                await session.flush()
                current, _ = await main.require_approval_run(session, PRINCIPAL, "fixture")
                envelope["snapshot"]["kmsIds"] = [701]
                envelope["plan"]["retrieved_documents"] = [{
                    "chunkId": 704, "documentId": 702, "childContent": "child",
                    "content": "parent", "parentContent": "parent", "parentFilled": True,
                }]
                if change in {"child", "parent"}:
                    (await session.get(KnowledgeChunk, 704 if change == "child" else 703)).content = "changed"
                document = await session.get(KnowledgeDocument, 702)
                if change == "deleted":
                    document.is_deleted = True
                elif change == "revision":
                    document.source_revision = 2
                elif change == "index":
                    document.active_index_version = 6
                elif change == "file":
                    document.file_id = 705
                    session.add(Resource(id=705, tenant_id=1, resource_type="files",
                                         owner_id=11, is_deleted=True, data={}))
                await session.flush()
                if change == "none":
                    await main.validate_approval_context(session, PRINCIPAL, current, envelope)
                else:
                    with pytest.raises(main.HTTPException):
                        await main.validate_approval_context(session, PRINCIPAL, current, envelope)
    asyncio.run(run())


def test_real_producer_pause_execute_resume_does_not_repeat_first_model(monkeypatch, tmp_path):
    provider_calls, provider_inputs = [], []
    async def provider(model, question, **kwargs):
        provider_calls.append(len(provider_calls) + 1)
        provider_inputs.append(json.loads(json.dumps(kwargs["messages"])))
        if len(provider_calls) == 1:
            kwargs["tool_call_sink"].append({
                "id": "call-1", "type": "function", "function": {
                    "name": "write_fixture", "arguments": '{"value":1}',
                },
            })
            yield ""
        else:
            assert any(row.get("role") == "tool" and SENTINEL in row.get("content", "")
                       for row in kwargs["messages"])
            yield "工具执行后的真实协议桩回答"
    monkeypatch.setattr(main, "stream_chat_completion", provider)
    async def run():
        async with scenario(monkeypatch, tmp_path, pause=False) as (sessions, calls, _, _, _):
            output = await asyncio.wait_for(
                collect(main.sse_events("fixture", PRINCIPAL)), 20,
            )
            await asyncio.gather(*list(generation.PRODUCERS))
            assert any('"status": "waiting_approval"' in row for row in output)
            assert calls == [] and provider_calls == [1]
            async with sessions() as session:
                result = await main.chat_get_approval("fixture", PRINCIPAL, session)
            summary = result["approval"]
            await approve(summary)
            result = await main.chat_continue_approved(ticket(summary), PRINCIPAL)
            cursor = int(result["afterEventId"].rsplit("-", 1)[1])
            output = await asyncio.wait_for(collect(main.sse_events("fixture", PRINCIPAL, cursor)), 20)
            await asyncio.gather(*list(generation.PRODUCERS))
            current, call, message = await state(sessions)
            assert current.status == message.status == "completed"
            assert provider_calls == [1, 2] and calls == [{"value": 1}]
            assert sum("event: done\n" in row for row in output) == 1
            assert not any('"status": "waiting_approval"' in row for row in output)
            assert any("工具执行后的真实协议桩回答" in row for row in output)
            async with sessions() as session:
                assistants = list(await session.scalars(select(ChatMessage).where(ChatMessage.role == "assistant")))
                assert len(assistants) == 1
    asyncio.run(run())


async def collect(source):
    return [row async for row in source]
