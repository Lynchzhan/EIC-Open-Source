import asyncio
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from eic.core.agent_runtime import AgentRuntimeStore, digest_payload
from eic.core.ids import initialize_allocator
from eic.models import AgentCheckpoint, AgentEvent, AgentRun, AgentToolCall, Base, Tenant


def test_agent_runtime_persists_trace_checkpoint_and_idempotent_terminal_state(tmp_path):
    async def run():
        engine = create_async_engine("sqlite+aiosqlite:///" + str(tmp_path / "agent-runtime.db"))
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            async with sessions() as session:
                session.add(Tenant(id=1, code=1, name="fixture"))
                await session.flush()
                await initialize_allocator(session)
                store = AgentRuntimeStore()
                agent_run = await store.create_run(
                    session,
                    tenant_id=1,
                    session_id=None,
                    request_id="request-1",
                    graph_version="p10-test",
                    metadata={"authorization": "secret-value", "source": "test"},
                )
                await store.append_event(
                    session,
                    run_id=agent_run.run_id,
                    node_name="prepare_context",
                    event_type="node_completed",
                    payload={"allowedToolCount": 1, "api_key": "secret-value"},
                )
                await store.append_checkpoint(
                    session,
                    run_id=agent_run.run_id,
                    node_name="prepare_context",
                    state={"route": "model", "authorization": "secret-value"},
                )
                await store.append_tool_call(
                    session,
                    run_id=agent_run.run_id,
                    call_id="call-1",
                    tool_name="calculator",
                    arguments={"a": 1, "b": 2},
                    result={"value": 3},
                    status="completed",
                    started_at=datetime.now(UTC),
                    finished_at=datetime.now(UTC),
                )
                await store.finish_for_request(
                    session,
                    tenant_id=1,
                    request_id="request-1",
                    status="completed",
                    result={"answerDigest": digest_payload("answer")},
                )
                await session.commit()

            async with sessions() as session:
                stored_run = await session.get(AgentRun, agent_run.run_id)
                events = list(await session.scalars(select(AgentEvent).where(AgentEvent.run_id == agent_run.run_id)))
                checkpoints = list(await session.scalars(select(AgentCheckpoint).where(AgentCheckpoint.run_id == agent_run.run_id)))
                tools = list(await session.scalars(select(AgentToolCall).where(AgentToolCall.run_id == agent_run.run_id)))
                assert stored_run is not None and stored_run.status == "completed"
                assert stored_run.finished_at is not None
                assert len(events) == 1 and events[0].payload["api_key"] == "[REDACTED]"
                assert len(checkpoints) == 1 and checkpoints[0].state_json["authorization"] == "[REDACTED]"
                assert checkpoints[0].state_hash == digest_payload(checkpoints[0].state_json)
                assert len(tools) == 1 and tools[0].output_digest == digest_payload({"value": 3})
                finished_at = stored_run.finished_at
                await AgentRuntimeStore().finish_run(
                    session,
                    run_id=agent_run.run_id,
                    status="failed",
                    error_code="late-error",
                )
                await session.commit()
                unchanged = await session.get(AgentRun, agent_run.run_id)
                assert unchanged.status == "completed" and unchanged.finished_at == finished_at
        finally:
            await engine.dispose()

    asyncio.run(run())
