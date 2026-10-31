import asyncio
from datetime import datetime, timezone
from pathlib import Path
import sys
import time

import pytest
from sqlalchemy import select

sys.path.insert(0, str(Path(__file__).parents[2] / "scripts"))
from harness.metrics import agent_metrics
from harness.observations import agent_observations, node_metrics, verify_runtime_trace
from eic.ai.agent_graph import execute_tool_calling_agent
from eic.core.agent_runtime import AgentRuntimeStore
from eic.models import AgentCheckpoint, AgentEvent, AgentToolCall
from test_id_generation_safety import fixture_db
from test_p12_tool_calling import calculator_center


def test_real_tool_loop_is_measured_without_fake_token_usage():
    async def run():
        decisions = iter([
            {"tool_calls": [{"id": "c1", "name": "calculator", "arguments": {"a": 2, "b": 3}}]},
            {"content": "5"},
        ])
        started = time.perf_counter()
        state = await execute_tool_calling_agent("计算", model_decider=lambda _: next(decisions),
                                                tool_center=calculator_center(), allowed_tools=["calculator"])
        observations = agent_observations(state, expected_answer="5", expected_tools={"calculator"},
                                          elapsed_ms=(time.perf_counter() - started) * 1000)
        summary = agent_metrics(observations)
        assert summary["metrics"]["taskSuccessRate"] == 1
        assert summary["metrics"]["toolSuccessRate"] == 1
        assert summary["sampleCounts"]["toolSuccessRate"] == 1
        assert summary["metrics"]["providerCost"] is None
        assert summary["metrics"]["p50FirstTokenMs"] is None
        assert node_metrics(state["trace_events"])["model_decision"]["count"] == 2
        state["tool_results"][0].update(status="rejected", errorCode="tool_not_allowed")
        assert agent_metrics(agent_observations(
            state, expected_answer="5", expected_tools=set(), elapsed_ms=1,
        ))["metrics"]["toolArgumentValidity"] is None
        state["agent_step_count"] = state["tool_budget"]["maxSteps"] + 1
        assert agent_observations(state, expected_answer="5", expected_tools={"calculator"},
                                  elapsed_ms=1)[0]["maxStepViolationRate"] is True
    asyncio.run(run())


def test_no_trace_evidence_is_not_valid():
    from types import SimpleNamespace
    run = SimpleNamespace(run_id="r1", trace_id="t1", graph_version="v1", event_seq=0,
                          checkpoint_seq=0, status="completed", finished_at=datetime.now(timezone.utc))
    assert verify_runtime_trace(run, [], [], [])["valid"] is False


def test_persisted_trace_detects_gap_tamper_and_cross_run(monkeypatch, tmp_path):
    async def run():
        async with fixture_db(monkeypatch, tmp_path) as sessions:
            async with sessions() as session:
                store = AgentRuntimeStore()
                run = await store.create_run(session, tenant_id=1, session_id=None,
                                             request_id="p15-fixture", graph_version="fixture")
                await store.append_event(session, run_id=run.run_id, node_name="context",
                                         event_type="node_completed", payload={"api_key": "PRIVATE"})
                await store.append_checkpoint(session, run_id=run.run_id, node_name="context",
                                              state={"route": "model", "authorization": "PRIVATE"})
                await store.append_tool_call(session, run_id=run.run_id, call_id="c1", tool_name="calculator",
                                             arguments={"a": 1}, result={"value": 1}, status="completed",
                                             started_at=datetime.now(timezone.utc), finished_at=datetime.now(timezone.utc))
                await store.finish_run(session, run_id=run.run_id, status="completed")
                await session.commit()
                events = list(await session.scalars(select(AgentEvent)))
                checkpoints = list(await session.scalars(select(AgentCheckpoint)))
                tools = list(await session.scalars(select(AgentToolCall)))
                summary = verify_runtime_trace(run, events, checkpoints, tools)
                assert summary["valid"] is True and "PRIVATE" not in str(summary)
                assert verify_runtime_trace(run, [], checkpoints, tools)["valid"] is False
                events[0].payload = {"modified": True}
                assert "event_digest_mismatch" in verify_runtime_trace(run, events, checkpoints, tools)["violations"]
                tools[0].run_id = "wrong"
                assert "run_binding_mismatch" in verify_runtime_trace(run, events, checkpoints, tools)["violations"]
                checkpoints[0].state_json = {"modified": True}
                assert "checkpoint_digest_mismatch" in verify_runtime_trace(run, events, checkpoints, tools)["violations"]
    asyncio.run(run())
