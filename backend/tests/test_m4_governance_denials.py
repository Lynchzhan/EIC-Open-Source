"""D2 离线治理拒绝契约；SQLite/脚本桩结果不代表 live 验收。

既有测试仅复用数据库和审批场景构造，不调用旧测试函数。
失败保留为当前候选缺陷；缺失的共享执行/取消接口显式标为 BLOCKED。
"""
import asyncio
import inspect
import json
import os
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import jwt
import pytest
from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import SQLAlchemyError
from test_id_generation_safety import claim, fixture_db
from test_p2_approval import PRINCIPAL, approve, scenario, state, ticket

from eic import main
from eic.ai.agent_graph import AgentState, build_agent_graph, execute_tool_calling_agent
from eic.ai.checkpointer import DatabaseCheckpointer
from eic.ai.tool_center import ToolCenter, ToolDefinition, ToolExecutionError
from eic.core import agent_worker, generation
from eic.core.agent_runtime import AgentLeaseLost, AgentRuntimeStore, utcnow
from eic.core.dependency_controls import DependencyDenied, admit, release
from eic.core.security import encrypt_secret
from auth_fixture import signed_token
from eic.models import (
    AdmissionLease,
    AdmissionWindow,
    AgentGraphCheckpoint,
    AgentRun,
    AgentRunLease,
    ChatEvent,
    ChatMessage,
    Permission,
    Resource,
    Role,
    RolePermission,
    Tenant,
    ToolAudit,
    User,
    UserRole,
)

PROVIDER_MOCK_REQUESTS = []
MCP_MOCK_REQUESTS = []
SECRET = "D2_SYNTHETIC_SECRET_NEVER_EXPORT"
LOCAL_EFFECTS = []
NETWORK_ATTEMPTS = []


@pytest.fixture(autouse=True)
def offline_boundary(monkeypatch, tmp_path, record_property):
    """共享 guard 拦外网；额外禁止 HTTP 发送，保留 Windows asyncio 回环 IPC。"""
    # 外置 harness 可指定收证根；正式隔离入口使用 pytest 独立临时目录。
    guard_result = os.environ.get("EIC_NETWORK_GUARD_RESULT")
    if guard_result:
        evidence = Path(guard_result).resolve().parent
        assert tmp_path.resolve().is_relative_to(evidence)
    assert tmp_path.is_dir()
    record_property("evidenceTier", "isolated_contract_sqlite_or_scripted_mock")
    record_property("liveStatus", "unverified")
    attempts = []

    def denied(*args, **kwargs):
        attempts.append("http_send")
        NETWORK_ATTEMPTS.append("http_send")
        raise AssertionError("D2 forbids HTTP sends, including loopback")

    async def async_denied(*args, **kwargs):
        return denied(*args, **kwargs)

    monkeypatch.setattr(httpx.Client, "send", denied)
    monkeypatch.setattr(httpx.AsyncClient, "send", async_denied)
    yield
    assert attempts == [], "拒绝契约发生了意外网络尝试"


async def grant(session, user_id=11, tenant_id=1, role_id=701, codes=("tool.execute",)):
    session.add(Role(id=role_id, tenant_id=tenant_id, name=f"D2-role-{role_id}"))
    await session.flush()
    for code in codes:
        if await session.get(Permission, code) is None:
            session.add(Permission(id=code, code=code, name=code))
            await session.flush()
        session.add(RolePermission(role_id=role_id, permission_id=code))
    session.add(UserRole(user_id=user_id, role_id=role_id))
    await session.flush()


def center_for(monkeypatch):
    effects = []

    def write(arguments):
        effects.append(dict(arguments))
        LOCAL_EFFECTS.append(dict(arguments))
        return {"ok": True}

    center = ToolCenter()
    center.register(ToolDefinition("d2_write", "D2 合成工具", write))
    monkeypatch.setattr(main, "tool_center", center)
    return center, effects


async def allow_tool(session, tenant_id=1):
    session.add(Resource(id=601, tenant_id=tenant_id, owner_id=11,
                         resource_type="skills", data={"allowedTools": ["d2_write"], "enabled": True}))
    await session.commit()


async def make_run(sessions):
    async with sessions() as session:
        run = await AgentRuntimeStore().create_run(
            session, tenant_id=1, session_id=101, request_id="fixture",
            graph_version=main.AGENT_GRAPH_VERSION,
        )
        token = await agent_worker.claim(session, run.run_id)
        await session.commit()
        return run.run_id, token


def graph_input(run_id):
    return {"user_message": "D2 fixture", "rewritten_query": "D2 fixture",
            "model_available": False, "allowed_tools": [], "retrieved_documents": [],
            "tool_calls": [], "tool_results": [], "route": "no_model", "reasoning": "",
            "agent_run_id": run_id, "trace_id": "d2", "graph_version": main.AGENT_GRAPH_VERSION,
            "current_node": "start", "trace_events": []}


@pytest.mark.parametrize("attack", ["bad-signature", "tenant-rebind", "inactive-user", "inactive-tenant", "unapproved"])
def test_signed_identity_rejects_forgery_or_revocation(monkeypatch, tmp_path, attack):
    async def run():
        async with fixture_db(monkeypatch, tmp_path) as sessions, sessions() as session:
            token = signed_token(11, 2 if attack == "tenant-rebind" else 1, "forged-admin-name", "unused")
            if attack == "bad-signature":
                token = jwt.encode({"sub": "11", "tenant_id": 1, "is_super_admin": True},
                                   "d2-wrong-key-that-is-long-enough-for-hmac", algorithm="HS256")
            elif attack == "inactive-user":
                (await session.get(User, 11)).is_active = False
            elif attack == "inactive-tenant":
                (await session.get(Tenant, 1)).active = False
            elif attack == "unapproved":
                (await session.get(User, 11)).approval_status = "pending"
            await session.commit()
            with pytest.raises(main.HTTPException) as error:
                await main.current_principal("Bearer " + token, session, None)
            assert error.value.status_code == 401
    asyncio.run(run())


def test_signed_name_does_not_escalate_database_identity(monkeypatch, tmp_path):
    async def run():
        async with fixture_db(monkeypatch, tmp_path) as sessions, sessions() as session:
            identity = await main.current_principal(
                "Bearer " + signed_token(11, 1, "admin", "unused"), session, None)
            assert identity.username == "fixture" and not identity.is_super_admin
            assert not await main.has_permission("tool.execute", identity, session)
    asyncio.run(run())


@pytest.mark.parametrize("boundary", ["cross-tenant-rbac", "missing-rbac", "foreign-skill"])
def test_direct_tool_rejects_before_handler_and_success_audit(monkeypatch, tmp_path, boundary):
    async def run():
        _, effects = center_for(monkeypatch)
        async with fixture_db(monkeypatch, tmp_path) as sessions, sessions() as session:
            if boundary != "missing-rbac":
                await grant(session, tenant_id=2 if boundary == "cross-tenant-rbac" else 1)
            await allow_tool(session, tenant_id=2 if boundary == "foreign-skill" else 1)
            with pytest.raises(main.HTTPException) as error:
                await main.execute_tool({"name": "d2_write", "arguments": {},
                                         "tenantId": 2, "isSuperAdmin": True},
                                        main.Principal(11, 1, "fixture", False), session)
            assert error.value.status_code == 403
            assert effects == []
            assert await session.scalar(select(func.count()).select_from(ToolAudit).where(
                ToolAudit.outcome == "success")) == 0
    asyncio.run(run())


@pytest.mark.parametrize("operation", ["read-model", "call-mcp", "overwrite-mcp"])
def test_foreign_credentials_rejected_before_decryption_or_transport(monkeypatch, tmp_path, operation):
    async def run():
        from eic.models import AIModel
        async with fixture_db(monkeypatch, tmp_path, is_super_admin=True) as sessions, sessions() as session:
            session.add(Tenant(id=2, code=2, name="foreign"))
            session.add(AIModel(id=501, tenant_id=2, model_name="foreign", model_type="chat",
                                endpoint="https://provider.invalid", secret_encrypted=encrypt_secret(SECRET)))
            session.add(Resource(id=602, tenant_id=2, owner_id=22, resource_type="mcps",
                                 data={"endpoint": "https://mcp.invalid", "transport": "http",
                                       "headers_encrypted": encrypt_secret(SECRET)}))
            await session.commit()
            decrypt = main.decrypt_secret
            used = []
            def observed_decrypt(value):
                used.append(value)
                return decrypt(value)
            monkeypatch.setattr(main, "decrypt_secret", observed_decrypt)
            with pytest.raises((main.HTTPException, main.RuntimeReferenceUnavailable)):
                if operation == "read-model":
                    await main.require_runtime_model(session, 1, 501, "chat")
                elif operation == "call-mcp":
                    await main.mcp_call({"mcpId": 602, "name": "write", "arguments": {}}, PRINCIPAL, session)
                else:
                    await main.mcp_add_edit({"id": 602, "endpoint": "https://attacker.invalid",
                                             "headers": {"Authorization": SECRET}}, PRINCIPAL, session)
            assert used == []
            assert (await session.get(Resource, 602)).tenant_id == 2
    asyncio.run(run())


def test_budget_atomic_reservation_duplicate_release_and_denial_rollback(monkeypatch, tmp_path):
    settings = SimpleNamespace(admission_global_concurrency=1, admission_tenant_concurrency=1,
                               admission_global_rate=10, admission_tenant_rate=10,
                               admission_global_units=8, admission_tenant_units=8)
    async def run():
        async with fixture_db(monkeypatch, tmp_path) as sessions:
            ready = asyncio.Event()
            waiting = 0
            async def reserve():
                nonlocal waiting
                waiting += 1
                if waiting == 2:
                    ready.set()
                await ready.wait()
                try:
                    return await admit(sessions, 1, 4, settings)
                except DependencyDenied as error:
                    return error
            results = await asyncio.gather(reserve(), reserve())
            leases = [x for x in results if isinstance(x, str)]
            denied = [x for x in results if isinstance(x, DependencyDenied)]
            assert len(leases) == len(denied) == 1
            assert denied[0].code == "concurrency_budget_exceeded"
            await release(sessions, leases[0])
            await release(sessions, leases[0])
            with pytest.raises(DependencyDenied) as error:
                await admit(sessions, 1, 5, settings)
            assert error.value.code == "cost_budget_exceeded"
            async with sessions() as session:
                for key in ("global", "tenant:1"):
                    window = await session.get(AdmissionWindow, key)
                    assert (window.requests, window.reserved_units) == (1, 4)
                rows = list(await session.scalars(select(AdmissionLease)))
                assert len(rows) == 1 and rows[0].finished
    asyncio.run(run())


def test_audit_store_failure_prevents_first_side_effect(monkeypatch, tmp_path):
    """审计故障 fail-closed 是待确认风险契约；失败不改源码或降级为 xfail。"""
    async def run():
        _, effects = center_for(monkeypatch)
        async with fixture_db(monkeypatch, tmp_path, is_super_admin=True) as sessions:
            async with sessions() as session:
                await allow_tool(session)
                monkeypatch.setattr(session, "commit", AsyncMock(side_effect=SQLAlchemyError("D2 audit unavailable")))
                with pytest.raises(main.HTTPException) as error:
                    await main.execute_tool({"name": "d2_write", "arguments": {}, "idempotencyKey": "audit-fault"},
                                            PRINCIPAL, session)
                assert error.value.status_code == 503
                await session.rollback()
            async with sessions() as session:
                audits = list(await session.scalars(select(ToolAudit)))
            assert audits == []
            assert effects == [], "审计写入失败前已执行本地副作用；策略须由协调者确认并修复"
    asyncio.run(run())


def test_idempotency_rebinding_cannot_execute_second_effect(monkeypatch, tmp_path):
    async def run():
        _, effects = center_for(monkeypatch)
        async with fixture_db(monkeypatch, tmp_path, is_super_admin=True) as sessions, sessions() as session:
            await allow_tool(session)
            payload = {"name": "d2_write", "arguments": {"value": 1}, "idempotencyKey": "same-key"}
            first = await main.execute_tool(payload, PRINCIPAL, session)
            replay = await main.execute_tool(payload, PRINCIPAL, session)
            assert not first["replayed"] and replay["replayed"]
            with pytest.raises(main.HTTPException) as error:
                await main.execute_tool({**payload, "arguments": {"value": 2}}, PRINCIPAL, session)
            assert error.value.detail["errorCode"] == "duplicate_tool_call_id"
            assert effects == [{"value": 1}]
    asyncio.run(run())


def test_unknown_effect_cannot_be_replayed_after_late_handler_success():
    """用事件屏障制造取消，不依赖 sleep 或调度速度。"""
    import threading
    entered, finish, completed = threading.Event(), threading.Event(), threading.Event()
    calls = []
    def handler(arguments):
        calls.append(arguments)
        entered.set()
        assert finish.wait(5)
        completed.set()
        return {"ok": True}
    center = ToolCenter()
    center.register(ToolDefinition("d2_unknown", "D2 合成副作用", handler))
    async def run():
        pending = asyncio.create_task(center.execute_audited_async(
            "d2_unknown", {}, {"d2_unknown"}, idempotency_key="unknown"))
        try:
            assert await asyncio.to_thread(entered.wait, 5)
            pending.cancel()
            with pytest.raises(asyncio.CancelledError):
                await pending
        finally:
            finish.set()
        assert await asyncio.to_thread(completed.wait, 5)
        with pytest.raises(ToolExecutionError) as error:
            await center.execute_audited_async("d2_unknown", {}, {"d2_unknown"}, idempotency_key="unknown")
        assert error.value.effect_status == "unknown" and not error.value.retryable
        assert calls == [{}]
    asyncio.run(run())


@pytest.mark.parametrize("mismatch", ["tenant", "owner", "run", "version"])
def test_checkpoint_service_rejects_wrong_tenant_owner_run_version(monkeypatch, tmp_path, mismatch):
    async def run():
        async with fixture_db(monkeypatch, tmp_path, is_super_admin=True) as sessions:
            run_id, _ = await make_run(sessions)
            async with sessions() as session:
                identity = PRINCIPAL
                if mismatch == "tenant":
                    identity = main.Principal(22, 2, "foreign", True)
                elif mismatch == "owner":
                    identity = main.Principal(22, 1, "other", True)
                elif mismatch == "version":
                    (await session.get(AgentRun, run_id)).graph_version = "D2-stale-version"
                    await session.commit()
                with pytest.raises(main.HTTPException) as error:
                    await main.require_approval_run(session, identity, "fixture",
                                                    "wrong-run" if mismatch == "run" else run_id)
                assert error.value.status_code == (409 if mismatch == "version" else 404)
    asyncio.run(run())


@pytest.mark.parametrize("mutation", ["missing-owner", "stale-owner", "cancelled-run"])
def test_graph_write_fences_missing_stale_and_cancelled_workers(monkeypatch, tmp_path, mutation):
    async def run():
        async with fixture_db(monkeypatch, tmp_path) as sessions:
            generation_token = await claim(sessions)
            run_id, worker_token = await make_run(sessions)
            async with sessions() as session:
                if mutation == "stale-owner":
                    await session.execute(update(AgentRunLease).where(AgentRunLease.run_id == run_id).values(worker_token="replacement"))
                elif mutation == "cancelled-run":
                    (await session.get(AgentRun, run_id)).status = "cancelled"
                await session.commit()
            g = generation.OWNER.set((301, generation_token))
            w = agent_worker.OWNER.set(None if mutation == "missing-owner" else (run_id, worker_token))
            try:
                with pytest.raises((AgentLeaseLost, generation.LeaseLost)):
                    await DatabaseCheckpointer(sessions).aput(
                        {"configurable": {"thread_id": run_id}},
                        {"id": "d2-denied", "channel_values": {}}, {}, {})
            finally:
                agent_worker.OWNER.reset(w)
                generation.OWNER.reset(g)
            async with sessions() as session:
                assert await session.scalar(select(func.count()).select_from(AgentGraphCheckpoint)) == 0
    asyncio.run(run())


def test_raw_checkpoint_read_does_not_trust_caller_tenant_config(monkeypatch, tmp_path):
    async def run():
        async with fixture_db(monkeypatch, tmp_path) as sessions:
            run_id, _ = await make_run(sessions)
            saver = DatabaseCheckpointer(sessions)
            key = [run_id, "", "d2-read"]
            kind, encrypted = saver._encode(key, {"checkpoint": {"id": key[2], "channel_values": {"business": "tenant-one"}}, "metadata": {}})
            async with sessions() as session:
                session.add(AgentGraphCheckpoint(run_id=run_id, namespace="", checkpoint_id=key[2],
                                                graph_version=main.AGENT_GRAPH_VERSION, schema_version=1,
                                                payload_type=kind, payload_encrypted=encrypted))
                await session.commit()
            assert await saver.aget_tuple({"configurable": {"thread_id": "nonexistent"}}) is None
            try:
                restored = await saver.aget_tuple({"configurable": {"thread_id": run_id, "tenant_id": 2}})
            except (PermissionError, AgentLeaseLost, ValueError):
                return
            assert restored is None, "直接 saver 读取未绑定可信租户；服务入口检查不等于 saver 授权"
    asyncio.run(run())


@pytest.mark.parametrize("field", ["api_key", "access_token", "principal", "permissions", "db_session", "worker_token"])
def test_graph_schema_excludes_trusted_capabilities(field):
    assert field not in AgentState.__annotations__


@pytest.mark.parametrize("channel", ["channel_values", "pending_write"])
def test_encrypted_checkpoint_restore_excludes_nested_credentials(monkeypatch, tmp_path, channel):
    async def run():
        async with fixture_db(monkeypatch, tmp_path) as sessions:
            run_id, worker_token = await make_run(sessions)
            saver = DatabaseCheckpointer(sessions)
            owner = agent_worker.OWNER.set((run_id, worker_token))
            try:
                config = await saver.aput({"configurable": {"thread_id": run_id}},
                    {"id": "d2-secret", "channel_values": {"tool_results": [{"safe": "keep", "api_key": SECRET}]}
                     if channel == "channel_values" else {}}, {}, {})
                if channel == "pending_write":
                    await saver.aput_writes(config, [("tool_results", [{"access_token": SECRET}])], "d2-task")
                restored = await saver.aget_tuple(config)
            finally:
                agent_worker.OWNER.reset(owner)
            text = json.dumps({"state": restored.checkpoint, "writes": restored.pending_writes}, default=str)
            assert SECRET not in text, "加密只保护落盘；恢复后敏感值仍进入 graph state"
    asyncio.run(run())


@pytest.mark.parametrize("key", ["api_key", "access_token"])
def test_log_recursive_snake_case_secret_exclusion(key):
    result = main.redact_for_log({"safe": "keep", "nested": [{key: SECRET}]})
    assert result["safe"] == "keep"
    assert SECRET not in json.dumps(result)


def test_graph_rejects_unauthorized_tool_without_effect(monkeypatch):
    center, effects = center_for(monkeypatch)
    decisions = iter([{"kind": "tool_calls", "tool_calls": [{"call_id": "forged", "name": "d2_write", "arguments": {}}]},
                      {"kind": "answer", "content": "denied"}])
    def decide(state):
        PROVIDER_MOCK_REQUESTS.append("scripted_model_decision")
        return next(decisions)
    result = asyncio.run(execute_tool_calling_agent("D2", model_decider=decide, tool_center=center, allowed_tools=[]))
    assert result["tool_results"][0]["errorCode"] == "tool_not_allowed"
    assert result["tool_calls"] == [] and effects == []


@pytest.mark.parametrize("revoked", ["tool.execute", "ai.chat.write", "tenant"])
def test_approved_resume_rechecks_current_rbac_and_tenant(monkeypatch, tmp_path, revoked):
    async def run():
        async with scenario(monkeypatch, tmp_path) as (sessions, calls, summary, _, _):
            await approve(summary)
            async with sessions() as session:
                (await session.get(User, 11)).is_super_admin = False
                await grant(session, codes=("tool.execute", "ai.chat.write"))
                if revoked == "tenant":
                    (await session.get(Tenant, 1)).active = False
                else:
                    await session.execute(delete(RolePermission).where(RolePermission.permission_id == revoked))
                await session.commit()
            with pytest.raises(main.HTTPException) as error:
                await main.chat_continue_approved(ticket(summary), PRINCIPAL)
            assert error.value.status_code in {403, 409}
            assert calls == []
            _, call, _ = await state(sessions)
            assert call.effect_status == "none"
    asyncio.run(run())


@pytest.mark.parametrize("boundary", ["foreign-tenant", "non-owner", "missing-rbac"])
def test_cancel_authorization_preserves_active_lease(monkeypatch, tmp_path, boundary):
    async def run():
        async with fixture_db(monkeypatch, tmp_path) as sessions:
            await claim(sessions)
            # 模拟经过真实身份认证的主体，确保测到所有权边界而非不存在的用户。
            async with sessions() as session:
                if boundary == "missing-rbac":
                    await session.execute(delete(RolePermission).where(RolePermission.role_id == 599))
                else:
                    if boundary == "foreign-tenant":
                        session.add(Tenant(id=2, code=2, name="foreign"))
                        await session.flush()
                    session.add(User(id=22, tenant_id=2 if boundary == "foreign-tenant" else 1,
                        username="other", password_hash="unused", is_super_admin=True))
                await session.commit()
            identity = main.Principal(22, 2 if boundary == "foreign-tenant" else 1, "other", True)
            if boundary == "missing-rbac":
                identity = main.Principal(11, 1, "fixture", False)
            async with sessions() as session:
                with pytest.raises(main.HTTPException) as error:
                    await main.chat_history_cancel("fixture", identity, session)
                assert error.value.status_code == (403 if boundary == "missing-rbac" else 404)
                await session.rollback()
            async with sessions() as session:
                assert (await session.get(ChatMessage, 301)).status == "generating"
                assert await session.scalar(select(func.count()).select_from(ChatEvent)) == 0
    asyncio.run(run())


def test_cancel_during_approved_unknown_effect_requires_review(monkeypatch, tmp_path):
    async def run():
        async with scenario(monkeypatch, tmp_path) as (sessions, calls, _, _, _), sessions() as session:
            message = await session.get(ChatMessage, 301)
            message.status = "approval_executing"
            await session.commit()
            with pytest.raises(main.HTTPException) as error:
                await main.chat_history_cancel("fixture", PRINCIPAL, session)
            assert error.value.status_code == 409
            assert error.value.detail["code"] == "tool_outcome_uncertain"
            assert message.status == "approval_executing" and calls == []
    asyncio.run(run())


def test_cancel_fences_graph_and_recovery_after_checkpoint(monkeypatch, tmp_path):
    async def run():
        async with fixture_db(monkeypatch, tmp_path, is_super_admin=True) as sessions:
            generation_token = await claim(sessions)
            run_id, worker_token = await make_run(sessions)
            saver = DatabaseCheckpointer(sessions)
            graph = build_agent_graph(saver)
            owner = agent_worker.OWNER.set((run_id, worker_token))
            try:
                config = {"configurable": {"thread_id": run_id}}
                await graph.ainvoke(graph_input(run_id), config, interrupt_after=["prepare_context"], durability="sync")
            finally:
                agent_worker.OWNER.reset(owner)
            async with sessions() as session:
                await main.chat_history_cancel("fixture", PRINCIPAL, session)
            g = generation.OWNER.set((301, generation_token))
            w = agent_worker.OWNER.set((run_id, worker_token))
            try:
                with pytest.raises((AgentLeaseLost, generation.LeaseLost)):
                    await saver.aput_writes(config | {"configurable": {**config["configurable"], "checkpoint_id": "late"}},
                                            [("route", "tool")], "old-worker")
            finally:
                generation.OWNER.reset(g)
                agent_worker.OWNER.reset(w)
            resumed = AsyncMock()
            monkeypatch.setattr(main, "_run_chat_producer", resumed)
            assert await main.recover_agent_request(301) is False
            assert resumed.await_count == 0
            async with sessions() as session:
                assert (await session.get(AgentRun, run_id)).status == "cancelled"
    asyncio.run(run())


def test_reclaimed_worker_cannot_write_release_or_renew_new_lease(monkeypatch, tmp_path):
    async def run():
        async with fixture_db(monkeypatch, tmp_path) as sessions:
            run_id, stale = await make_run(sessions)
            async with sessions() as session:
                await session.execute(update(AgentRunLease).values(expires_at=utcnow() - timedelta(seconds=2)))
                await session.commit()
            async with sessions() as session:
                fresh = await agent_worker.claim(session, run_id)
                await session.commit()
            assert fresh != stale
            async with sessions() as session:
                with pytest.raises(AgentLeaseLost):
                    await AgentRuntimeStore().renew_worker(session, run_id=run_id, worker_token=stale)
                assert not await AgentRuntimeStore().release_worker(session, run_id=run_id, worker_token=stale)
                await session.commit()
            owner = agent_worker.OWNER.set((run_id, stale))
            try:
                with pytest.raises(AgentLeaseLost):
                    await DatabaseCheckpointer(sessions).aput_writes(
                        {"configurable": {"thread_id": run_id, "checkpoint_id": "late"}}, [("route", "tool")], "stale")
            finally:
                agent_worker.OWNER.reset(owner)
            async with sessions() as session:
                lease = await session.get(AgentRunLease, run_id)
                assert lease.worker_token == fresh and lease.state == "active"
    asyncio.run(run())


def test_unknown_provider_recovery_never_dispatches_producer(monkeypatch, tmp_path):
    async def run():
        async with fixture_db(monkeypatch, tmp_path, is_super_admin=True) as sessions:
            await claim(sessions)
            run_id, _ = await make_run(sessions)
            async with sessions() as session:
                await AgentRuntimeStore().append_checkpoint(session, run_id=run_id, node_name="provider",
                    state={"providerStarted": True, "providerOutcome": "uncertain"})
                from eic.models import GenerationLease
                await session.execute(update(GenerationLease).values(expires_at=utcnow() - timedelta(seconds=2)))
                await session.execute(update(AgentRunLease).values(expires_at=utcnow() - timedelta(seconds=2)))
                await session.commit()
            resumed = AsyncMock()
            monkeypatch.setattr(main, "_run_chat_producer", resumed)
            assert await main.recover_agent_request(301) is False
            assert await main.recover_agent_request(301) is False
            assert resumed.await_count == 0
            async with sessions() as session:
                assert (await session.get(AgentRun, run_id)).status == "manual_review"
    asyncio.run(run())


def test_shared_executor_cancellation_port_contract():
    from eic.ai import shared_executor as shared
    from test_m3_shared_executor import services_for, decision
    async def run():
        calls = []
        async def revoked():
            raise PermissionError('lease_revoked')
        services = services_for([decision()], calls=calls, guard=revoked)
        assert isinstance(services, shared.ExecutionServices)
        with pytest.raises(PermissionError, match='lease_revoked'):
            await shared.execute_shared('fixture', run_id='cancel-contract', services=services)
        assert calls == []
        closed = asyncio.Event()
        async def waiting_model(state):
            try:
                await services.emit('started')
                await asyncio.Event().wait()
            finally:
                closed.set()
        services.guard = None
        services.model = waiting_model
        stream = shared.stream_shared('fixture', run_id='cancel-stream-contract', services=services)
        assert await anext(stream) == 'started'
        await stream.aclose()
        assert closed.is_set() and calls == []
    asyncio.run(run())
