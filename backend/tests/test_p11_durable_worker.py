"""真实编译图、加密持久快照和生产 Worker 接管的独立回归。"""
import asyncio
from datetime import timedelta
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.exc import OperationalError

from eic.ai.agent_graph import (
    AGENT_GRAPH_VERSION, CHAT_AGENT_GRAPH, build_agent_graph, plan_chat_execution_durable,
)
from eic.ai.checkpointer import DatabaseCheckpointer
from eic.core import agent_worker, generation
from eic.core.agent_runtime import AgentLeaseLost, AgentRuntimeStore, utcnow
from eic.models import (
    AgentGraphCheckpoint, AgentGraphWrite, AgentRun, AgentRunLease, ChatMessage,
    ChatEvent, GenerationLease, Tenant,
)
from test_id_generation_safety import claim, fixture_db


async def make_run(sessions):
    async with sessions() as session:
        store = AgentRuntimeStore()
        run = await store.create_run(
            session, tenant_id=1, session_id=101, request_id="fixture",
            graph_version=AGENT_GRAPH_VERSION,
        )
        token = await agent_worker.claim(session, run.run_id)
        await session.commit()
    return run.run_id, token


def input_state(run_id):
    return {
        "user_message": "PRIVATE_QUESTION", "rewritten_query": "PRIVATE_QUESTION",
        "model_available": True, "allowed_tools": ["calculator", "calculator"],
        "retrieved_documents": [], "tool_calls": [], "tool_results": [],
        "route": "no_model", "reasoning": "", "agent_run_id": run_id,
        "trace_id": "trace", "graph_version": AGENT_GRAPH_VERSION,
        "current_node": "start", "trace_events": [],
    }


def test_actual_graph_uses_persistent_database_saver():
    assert isinstance(CHAT_AGENT_GRAPH.checkpointer, DatabaseCheckpointer)


def test_restart_resumes_real_graph_without_repeating_completed_nodes(monkeypatch, tmp_path):
    async def run():
        async with fixture_db(monkeypatch, tmp_path) as sessions:
            run_id, token = await make_run(sessions)
            config = {"configurable": {"thread_id": run_id}}
            owner = agent_worker.OWNER.set((run_id, token))
            try:
                first = build_agent_graph(DatabaseCheckpointer(sessions))
                await first.ainvoke(input_state(run_id), config, interrupt_after=["prepare_context"],
                                    durability="sync")
                snapshot = await first.aget_state(config)
                assert snapshot.next == ("rewrite_query",)
                # 全新编译图及 Saver，不复用进程内 graph 状态。
                second = build_agent_graph(DatabaseCheckpointer(sessions))
                result = await plan_chat_execution_durable(
                    "PRIVATE_QUESTION", model_available=True, allowed_tools=["calculator"],
                    agent_run_id=run_id, trace_id="trace", graph=second,
                )
                assert [event["node"] for event in result["trace_events"]] == [
                    "prepare_context", "rewrite_query", "prepare_retrieval", "choose_route",
                ]
                assert result["route"] == "model"
                assert result["allowed_tools"] == ["calculator"]
                assert (await second.aget_state(config)).next == ()
                listed = [item async for item in second.checkpointer.alist(config, limit=2)]
                assert len(listed) == 2
            finally:
                agent_worker.OWNER.reset(owner)
            async with sessions() as session:
                rows = list(await session.scalars(select(AgentGraphCheckpoint)))
                writes = list(await session.scalars(select(AgentGraphWrite)))
                assert len(rows) >= 6 and writes
                assert all("PRIVATE_QUESTION" not in row.payload_encrypted for row in rows + writes)
    asyncio.run(run())


def test_transient_checkpoint_read_error_keeps_request_recoverable(monkeypatch, tmp_path):
    from eic import main
    from eic.ai import shared_executor as shared
    from test_m3_shared_executor import services_for, decision

    async def run():
        async with fixture_db(monkeypatch, tmp_path, is_super_admin=True) as sessions:
            await claim(sessions)
            run_id, token = await make_run(sessions)
            graph = shared.build_execution_graph(shared.ExecutorCheckpointer(sessions))
            effects = []
            async def crash_before_record(state):
                raise RuntimeError('synthetic-process-loss')
            monkeypatch.setattr(shared, 'record_tool_result', crash_before_record)
            owner = agent_worker.OWNER.set((run_id, token))
            try:
                with pytest.raises(RuntimeError, match='synthetic-process-loss'):
                    await shared.execute_shared('fixture', run_id=run_id,
                        services=services_for([decision()], calls=effects), graph=graph)
            finally:
                agent_worker.OWNER.reset(owner)
            async with sessions() as session:
                await session.execute(update(GenerationLease).values(
                    expires_at=utcnow() - timedelta(seconds=1),
                ))
                await session.execute(update(AgentRunLease).values(
                    expires_at=utcnow() - timedelta(seconds=1),
                ))
                await session.commit()
            assert effects == ['one']
            original = main.ExecutorCheckpointer.aget_tuple
            monkeypatch.setattr(main.ExecutorCheckpointer, "aget_tuple", AsyncMock(
                side_effect=OperationalError("fixture", {}, RuntimeError("transient")),
            ))
            with pytest.raises(OperationalError):
                await main.recover_agent_request(301, sessions=sessions)
            async with sessions() as session:
                assert (await session.get(AgentRun, run_id)).status == "running"
                assert (await session.get(ChatMessage, 301)).status == "generating"
                assert (await session.get(AgentRunLease, run_id)).attempt == 1
            monkeypatch.setattr(main.ExecutorCheckpointer, "aget_tuple", original)
            resumed = AsyncMock()
            monkeypatch.setattr(main, "_run_chat_producer", resumed)
            def wrong_global_sessions():
                raise AssertionError('explicit session factory must be used')
            monkeypatch.setattr(main, 'SessionLocal', wrong_global_sessions)
            assert await main.recover_agent_request(301, sessions=sessions)
            await asyncio.sleep(0)
            assert resumed.await_count == 1
            assert effects == ['one']
    asyncio.run(run())


@pytest.mark.parametrize("revoked", ["permission", "tenant"])
def test_revoked_authorization_closes_expired_request_without_replay(monkeypatch, tmp_path, revoked):
    from eic import main

    async def run():
        async with fixture_db(monkeypatch, tmp_path, is_super_admin=True) as sessions:
            await claim(sessions)
            run_id, token = await make_run(sessions)
            graph = build_agent_graph(DatabaseCheckpointer(sessions))
            owner = agent_worker.OWNER.set((run_id, token))
            try:
                await graph.ainvoke(input_state(run_id), {"configurable": {"thread_id": run_id}},
                                    interrupt_after=["prepare_context"], durability="sync")
            finally:
                agent_worker.OWNER.reset(owner)
            async with sessions() as session:
                await session.execute(update(GenerationLease).values(
                    expires_at=utcnow() - timedelta(seconds=1),
                ))
                await session.execute(update(AgentRunLease).values(
                    expires_at=utcnow() - timedelta(seconds=1),
                ))
                if revoked == "tenant":
                    (await session.get(Tenant, 1)).active = False
                await session.commit()
            if revoked == "permission":
                from eic.core import authorization
                monkeypatch.setattr(authorization, "has_permission", AsyncMock(return_value=False))
            monkeypatch.setattr(main, "CHAT_AGENT_GRAPH", graph)
            resumed = AsyncMock()
            monkeypatch.setattr(main, "_run_chat_producer", resumed)
            assert await generation.recover_expired() == 1
            assert await generation.recover_expired() == 0
            assert resumed.await_count == 0
            async with sessions() as session:
                assert (await session.get(AgentRun, run_id)).status == "failed"
                assert (await session.get(ChatMessage, 301)).status == "failed"
                assert await session.scalar(select(func.count()).select_from(ChatEvent).where(
                    ChatEvent.event_type == "done",
                )) == 1
    asyncio.run(run())


@pytest.mark.parametrize("failure", ["stale_worker", "tamper", "old_schema", "cross_run"])
def test_checkpoint_rejects_stale_worker_and_invalid_state(monkeypatch, tmp_path, failure):
    async def run():
        async with fixture_db(monkeypatch, tmp_path) as sessions:
            run_id, token = await make_run(sessions)
            saver = DatabaseCheckpointer(sessions)
            graph = build_agent_graph(saver)
            config = {"configurable": {"thread_id": run_id}}
            owner = agent_worker.OWNER.set((run_id, token))
            try:
                await graph.ainvoke(input_state(run_id), config, interrupt_after=["prepare_context"],
                                    durability="sync")
                item = await saver.aget_tuple(config)
                async with sessions() as session:
                    row = await session.get(AgentGraphCheckpoint, (
                        run_id, "", item.checkpoint["id"],
                    ))
                    if failure == "tamper":
                        row.payload_encrypted = row.payload_encrypted[:-5] + "XXXXX"
                    elif failure == "old_schema":
                        row.schema_version = 99
                    elif failure == "cross_run":
                        older = await session.scalar(select(AgentGraphCheckpoint).where(
                            AgentGraphCheckpoint.checkpoint_id != row.checkpoint_id,
                        ))
                        row.payload_encrypted = older.payload_encrypted
                    else:
                        await session.execute(update(AgentRunLease).values(worker_token="replacement"))
                    await session.commit()
                if failure == "stale_worker":
                    with pytest.raises(AgentLeaseLost):
                        await saver.aput_writes(item.config, [("route", "model")], "stale")
                else:
                    with pytest.raises(Exception):
                        await saver.aget_tuple(config)
            finally:
                agent_worker.OWNER.reset(owner)
    asyncio.run(run())


@pytest.mark.parametrize("provider_started", [False, True])
def test_production_recovery_claims_once_and_never_resends_unknown_provider(
    monkeypatch, tmp_path, provider_started,
):
    from eic import main

    async def run():
        async with fixture_db(monkeypatch, tmp_path, is_super_admin=True) as sessions:
            await claim(sessions)
            run_id, token = await make_run(sessions)
            graph = build_agent_graph(DatabaseCheckpointer(sessions))
            owner = agent_worker.OWNER.set((run_id, token))
            try:
                await graph.ainvoke(input_state(run_id), {"configurable": {"thread_id": run_id}},
                                    interrupt_after=["prepare_context"], durability="sync")
            finally:
                agent_worker.OWNER.reset(owner)
            async with sessions() as session:
                if provider_started:
                    await AgentRuntimeStore().append_checkpoint(
                        session, run_id=run_id, node_name="provider_pending",
                        state={"providerStarted": True, "providerOutcome": "uncertain"},
                        resume_policy="manual_review",
                    )
                await session.execute(update(GenerationLease).values(
                    expires_at=utcnow() - timedelta(seconds=1),
                ))
                await session.execute(update(AgentRunLease).values(
                    expires_at=utcnow() - timedelta(seconds=1),
                ))
                await session.commit()
            monkeypatch.setattr(main, "CHAT_AGENT_GRAPH", graph)
            resumed = AsyncMock()
            monkeypatch.setattr(main, "_run_chat_producer", resumed)
            result = await main.recover_agent_request(301)
            await asyncio.sleep(0)
            assert result is (not provider_started)
            assert await main.recover_agent_request(301) is False
            assert resumed.await_count == (0 if provider_started else 1)
            async with sessions() as session:
                run = await session.get(AgentRun, run_id)
                assert run.status == ("manual_review" if provider_started else "running")
                assert await session.scalar(select(func.count()).select_from(AgentRun)) == 1
                assert (await session.get(ChatMessage, 301)).status == "generating"
                lease = await session.get(AgentRunLease, run_id)
                assert lease.attempt == (1 if provider_started else 2)
    asyncio.run(run())
