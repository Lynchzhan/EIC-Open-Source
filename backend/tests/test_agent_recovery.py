"""P11 节点恢复、快照拒绝和 Worker fencing 回归。"""
import asyncio
from datetime import timedelta

import pytest
from sqlalchemy import update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from eic.core.agent_runtime import (
    AgentLeaseLost,
    AgentRuntimeStore,
    utcnow,
)
from eic.core.ids import initialize_allocator
from eic.models import AgentRunLease, Base, Tenant


def _make_runtime_db(tmp_path):
    return create_async_engine(
        "sqlite+aiosqlite:///" + str(tmp_path / "agent-recovery.db"),
        connect_args={"timeout": 30},
    )


async def _create_run(sessions):
    async with sessions() as session:
        session.add(Tenant(id=1, code=1, name="fixture"))
        await session.flush()
        await initialize_allocator(session)
        run = await AgentRuntimeStore().create_run(
            session,
            tenant_id=1,
            session_id=None,
            request_id="recovery-request",
            graph_version="p11-agent-graph-v1",
        )
        await session.commit()
        return run.run_id


def test_recovery_accepts_latest_safe_checkpoint_and_fences_duplicate_workers(tmp_path):
    async def run():
        engine = _make_runtime_db(tmp_path)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            run_id = await _create_run(sessions)
            store = AgentRuntimeStore()
            async with sessions() as session:
                await store.append_checkpoint(
                    session,
                    run_id=run_id,
                    node_name="retrieve",
                    state={"providerStarted": False, "providerOutcome": "not_started"},
                )
                decision = await store.decide_recovery(
                    session,
                    run_id=run_id,
                    expected_graph_version="p11-agent-graph-v1",
                )
                assert decision.action == "safe_replay"
                assert decision.reason == "latest_safe_checkpoint"
                token = await store.claim_worker(session, run_id=run_id)
                assert token
                assert await store.claim_worker(session, run_id=run_id) is None
                with pytest.raises(AgentLeaseLost):
                    await store.renew_worker(session, run_id=run_id, worker_token="stale")
                await store.renew_worker(session, run_id=run_id, worker_token=token)
                assert await store.release_worker(session, run_id=run_id, worker_token=token)
                await session.commit()
        finally:
            await engine.dispose()

    asyncio.run(run())


@pytest.mark.parametrize(
    ("state", "resume_policy", "expected_action", "expected_reason"),
    [
        ({"providerStarted": True, "providerOutcome": "uncertain"}, "safe_replay", "manual_review", "provider_outcome_uncertain"),
        ({"providerStarted": False, "providerOutcome": "not_started"}, "manual_review", "manual_review", "checkpoint_requires_review"),
        ({"providerStarted": False, "providerOutcome": "not_started"}, "terminal", "terminal", "checkpoint_terminal"),
    ],
)
def test_recovery_does_not_replay_uncertain_or_non_replayable_checkpoints(
    tmp_path, state, resume_policy, expected_action, expected_reason
):
    async def run():
        engine = _make_runtime_db(tmp_path)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            run_id = await _create_run(sessions)
            async with sessions() as session:
                store = AgentRuntimeStore()
                await store.append_checkpoint(
                    session,
                    run_id=run_id,
                    node_name="provider",
                    state=state,
                    resume_policy=resume_policy,
                )
                decision = await store.decide_recovery(
                    session,
                    run_id=run_id,
                    expected_graph_version="p11-agent-graph-v1",
                )
                assert decision.action == expected_action
                assert decision.reason == expected_reason
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_recovery_rejects_old_graph_schema_and_tampered_checkpoint(tmp_path):
    async def run():
        engine = _make_runtime_db(tmp_path)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            run_id = await _create_run(sessions)
            store = AgentRuntimeStore()
            async with sessions() as session:
                checkpoint = await store.append_checkpoint(
                    session,
                    run_id=run_id,
                    node_name="rewrite_query",
                    state={"providerStarted": False},
                    state_schema_version=99,
                )
                decision = await store.decide_recovery(
                    session,
                    run_id=run_id,
                    expected_graph_version="p11-agent-graph-v1",
                )
                assert decision.action == "manual_review"
                assert decision.reason == "checkpoint_schema_unsupported"
                checkpoint.state_schema_version = 1
                checkpoint.state_hash = "0" * 64
                await session.commit()
            async with sessions() as session:
                decision = await store.decide_recovery(
                    session,
                    run_id=run_id,
                    expected_graph_version="p11-agent-graph-v1",
                )
                assert decision.action == "manual_review"
                assert decision.reason == "checkpoint_integrity_failed"
                await session.execute(
                    update(AgentRunLease)
                    .where(AgentRunLease.run_id == run_id)
                    .values(expires_at=utcnow() - timedelta(seconds=1))
                )
                await session.commit()
        finally:
            await engine.dispose()

    asyncio.run(run())
