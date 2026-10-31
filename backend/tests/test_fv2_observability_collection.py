"""确定性协议、提交边界、权限关联、失败和真实未知值负控。"""
import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
import json
import time

import httpx
import pytest
from sqlalchemy import select

from eic.ai.providers import ModelProviderError, stream_chat_completion
from eic.core import observability as obs
from eic.models import AIModel, ObservabilityEvent, ToolAudit
from test_id_generation_safety import fixture_db


@asynccontextmanager
async def collecting(monkeypatch, tmp_path):
    async with fixture_db(monkeypatch, tmp_path, is_super_admin=True) as sessions:
        collector = obs.Collector()
        monkeypatch.setattr(obs, "collector", collector)
        await collector.start(sessions)
        token = obs.CONTEXT.set({"tenant_id": 1, "owner_key": obs.actor_key(1, 11),
                                 "request_id": "fixture", "chat_id": "101"})
        try:
            yield sessions, collector
        finally:
            obs.CONTEXT.reset(token)
            await collector.stop()


def model(provider="openai_compatible"):
    return AIModel(id=201, tenant_id=1, model_type="chat", provider=provider,
                   model_name="fixture", endpoint="http://fixture.invalid/v1",
                   max_input_tokens=4096, max_output_tokens=512)


@pytest.mark.parametrize("usage,expected", [
    ({}, {}),
    ({"usage": None}, {}),
    ({"usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}},
     {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0, "usage_source": "provider_usage"}),
    ({"usage": {"prompt_tokens": True, "completion_tokens": -1, "total_tokens": 1.5}}, {}),
    ({"usage": {"prompt_tokens": 2**64}}, {}),
    ({"usage": {"prompt_tokens": 8, "prompt_tokens_details": {"cached_tokens": 9}}},
     {"input_tokens": 8, "usage_source": "provider_usage"}),
    ({"usage": {"prompt_tokens": 8, "prompt_tokens_details": {"cached_tokens": 0}}},
     {"input_tokens": 8, "cached_tokens": 0, "cache_outcome": "miss", "usage_source": "provider_usage"}),
])
def test_usage_values_are_not_estimated_or_coerced(usage, expected):
    assert obs.usage_fields(usage) == expected


@pytest.mark.parametrize("finish", ["stop", "tool_calls"])
def test_usage_only_chunk_after_finish_is_collected_without_text(monkeypatch, tmp_path, finish):
    async def run():
        async with collecting(monkeypatch, tmp_path) as (sessions, collector):
            chunks = [
                {"choices": [{"delta": {"content": "答案"}}]},
                {"choices": [{"delta": {}, "finish_reason": finish}]},
                {"choices": [], "usage": {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10,
                                         "prompt_tokens_details": {"cached_tokens": 4}}},
            ]
            body = "".join("data: " + json.dumps(chunk, ensure_ascii=False) + "\n\n" for chunk in chunks) + "data: [DONE]\n\n"
            transport = httpx.MockTransport(lambda _: httpx.Response(200, content=body))
            sink = {}
            async with httpx.AsyncClient(transport=transport) as client:
                output = [x async for x in stream_chat_completion(model(), "PRIVATE_PROMPT", client=client,
                                                                 usage_sink=sink, tools=[])]
            await collector.flush()
            assert output == ["答案"]
            async with sessions() as session:
                rows = list(await session.scalars(select(ObservabilityEvent)))
            assert len(rows) == 1 and rows[0].total_tokens == 10 and rows[0].cached_tokens == 4
            assert rows[0].request_id == "fixture" and rows[0].owner_key == obs.actor_key(1, 11)
            assert rows[0].usage_source == "provider_usage" and rows[0].saved_tokens is None
            assert "PRIVATE_PROMPT" not in str(rows[0].summary) and "_invoked" not in sink
    asyncio.run(run())


def test_retry_is_a_new_physical_invocation_and_unknown_usage_stays_null(monkeypatch, tmp_path):
    async def run():
        async with collecting(monkeypatch, tmp_path) as (sessions, collector):
            calls = iter([429, 200])
            transport = httpx.MockTransport(lambda _: httpx.Response(
                next(calls), content='data: {"choices":[{"delta":{"content":"答案"}}]}\n\ndata: [DONE]\n\n'))
            async with httpx.AsyncClient(transport=transport) as client:
                with pytest.raises(ModelProviderError):
                    _ = [x async for x in stream_chat_completion(model(), "private", client=client)]
                assert [x async for x in stream_chat_completion(model(), "private", client=client)] == ["答案"]
            await collector.flush()
            async with sessions() as session:
                rows = list(await session.scalars(select(ObservabilityEvent).where(ObservabilityEvent.category == "model_call")))
            assert len(rows) == 2 and rows[0].span_id != rows[1].span_id
            assert {r.status for r in rows} == {"error", "success"}
            assert all(r.total_tokens is None and r.usage_source == "not_provided" for r in rows)
    asyncio.run(run())


def test_ollama_reports_partial_usage_without_fabricated_total(monkeypatch, tmp_path):
    async def run():
        async with collecting(monkeypatch, tmp_path) as (sessions, collector):
            transport = httpx.MockTransport(lambda _: httpx.Response(
                200, content='{"message":{"content":"答案"},"done":true,"prompt_eval_count":8,"eval_count":2}\n'))
            async with httpx.AsyncClient(transport=transport) as client:
                assert [x async for x in stream_chat_completion(model("ollama"), "private", client=client)] == ["答案"]
            await collector.flush()
            async with sessions() as session:
                row = await session.scalar(select(ObservabilityEvent))
            assert row.input_tokens == 8 and row.output_tokens == 2 and row.total_tokens is None
            assert row.usage_source == "provider_ollama"
    asyncio.run(run())


def test_commit_rollback_and_duplicates_do_not_fake_events(monkeypatch, tmp_path):
    async def run():
        async with collecting(monkeypatch, tmp_path) as (sessions, collector):
            row = obs.observation("request", "test", "success", event_key="stable")
            async with sessions() as session:
                obs.stage(session, row)
                await session.rollback()
            await collector.flush()
            assert collector.counters["enqueued"] == 0
            async with sessions() as session:
                obs.stage(session, row)
                await session.commit()
            collector.submit(row)
            await collector.flush()
            async with sessions() as session:
                rows = list(await session.scalars(select(ObservabilityEvent)))
            assert len(rows) == 1 and collector.counters["duplicates"] == 1
    asyncio.run(run())


def test_real_business_records_emit_only_after_commit(monkeypatch, tmp_path):
    from eic.core.agent_runtime import AgentRuntimeStore
    async def run():
        async with collecting(monkeypatch, tmp_path) as (sessions, collector):
            async with sessions() as session:
                store = AgentRuntimeStore()
                run = await store.create_run(session, tenant_id=1, session_id=101, request_id="fixture",
                                             graph_version="fixture")
                await store.append_event(session, run_id=run.run_id, node_name="approval",
                                         event_type="approval_required", status="waiting_approval",
                                         payload={"password": "PRIVATE", "text": "PRIVATE"})
                session.add(ToolAudit(id=801, tenant_id=1, user_id=11, tool_name="calculator",
                                      outcome="success", input_digest="digest", duration_ms=12,
                                      error_message="PRIVATE"))
                await session.flush()
                assert collector.counters["enqueued"] == 0
                await session.commit()
            await collector.flush()
            async with sessions() as session:
                rows = list(await session.scalars(select(ObservabilityEvent)))
            assert {r.category for r in rows} == {"agent_run", "approval", "tool_call"}
            assert all("PRIVATE" not in str(r.summary) for r in rows)
            assert next(r for r in rows if r.category == "tool_call").owner_key == obs.actor_key(1, 11)
    asyncio.run(run())


def test_queue_full_and_write_failure_are_bounded(monkeypatch, tmp_path):
    async def run():
        async with collecting(monkeypatch, tmp_path) as (sessions, collector):
            monkeypatch.setattr(collector, "queue", asyncio.Queue(maxsize=1))
            row = obs.observation("request", "test", "success")
            assert collector.submit(row)
            assert not collector.submit(row)
            assert collector.counters["dropped"] == 1
            # consumer 尚未调度，队列与任务仍使用当前字段；失败独立于业务提交。
            async def failure(_):
                raise RuntimeError("PRIVATE_SQL_CONNECTION")
            monkeypatch.setattr(collector, "_write", failure)
            await collector.flush()
            assert collector.counters["writeFailures"] == 1
            async with sessions() as session:
                session.add(ToolAudit(id=802, tenant_id=1, user_id=11, tool_name="calculator",
                                      outcome="success", input_digest="digest", duration_ms=1))
                await session.commit()
            await collector.flush()
            async with sessions() as session:
                assert await session.get(ToolAudit, 802) is not None
            assert "PRIVATE" not in str(collector.health())
    asyncio.run(run())


def test_writer_timeout_and_shutdown_are_bounded(monkeypatch, tmp_path):
    async def run():
        async with collecting(monkeypatch, tmp_path) as (_, collector):
            monkeypatch.setattr(obs.get_settings(), "observability_write_timeout", 0.05)
            monkeypatch.setattr(obs.get_settings(), "observability_lock_retry_timeout", 0.15)
            async def slow(_):
                await asyncio.sleep(10)
            monkeypatch.setattr(collector, "_write", slow)
            collector.submit(obs.observation("request", "test", "success"))
            started = time.monotonic()
            await collector.flush()
            assert time.monotonic() - started < 1
            assert collector.counters["writeFailures"] == 1
            assert collector.counters["writeTimeoutRetries"] >= 1
    asyncio.run(run())


def test_redaction_owner_namespace_and_retention_only_new_table(monkeypatch, tmp_path):
    async def run():
        async with collecting(monkeypatch, tmp_path) as (sessions, collector):
            assert obs.actor_key(1, 11) != obs.actor_key(2, 11)
            row = obs.observation("request", "test", "success", event_key="old",
                                  summary={"content": "PRIVATE", "authorization": "PRIVATE",
                                           "route": "http://PRIVATE?key=PRIVATE", "httpStatus": 200},
                                  occurred_at=datetime.now(UTC) - timedelta(days=31))
            assert row["summary"] == {"httpStatus": 200}
            # 单独直接写入旧观测记录，再运行新表保留策略。
            async with sessions() as session:
                session.add(ObservabilityEvent(**row, collected_at=datetime.now(UTC)))
                session.add(ToolAudit(id=803, tenant_id=1, user_id=11, tool_name="calculator",
                                      outcome="success", input_digest="digest",
                                      created_at=datetime.now(UTC) - timedelta(days=100)))
                await session.commit()
            await collector.flush()
            await collector.prune()
            async with sessions() as session:
                assert await session.get(ObservabilityEvent, "old") is None
                assert await session.get(ToolAudit, 803) is not None
            cross = obs.observation("model_call", "model", "success", tenant_id=2)
            assert cross.get("owner_key") is None and cross.get("request_id") is None
    asyncio.run(run())


def test_asgi_middleware_passes_sse_bytes_unchanged(monkeypatch):
    async def run():
        collector = obs.Collector()
        collector.queue, collector.running = asyncio.Queue(maxsize=10), True
        monkeypatch.setattr(obs, "collector", collector)
        sent = []
        body = b"event: aimsg\ndata: PRIVATE\n\n"
        async def app(scope, receive, send):
            obs.CONTEXT.get().update(tenant_id=1, owner_key=obs.actor_key(1, 11))
            scope["route"] = type("Route", (), {"path": "/api/Chat/{id}"})()
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": body, "more_body": False})
        async def send(message):
            sent.append(message)
        await obs.ObservationMiddleware(app)({"type": "http", "path": "/api/Chat/123", "method": "GET"},
                                             None, send)
        assert sent[-1]["body"] == body and collector.queue.qsize() == 1
        row = collector.queue.get_nowait()
        assert "PRIVATE" not in str(row) and row["summary"]["route"] == "/api/Chat/{id}"
        assert obs.CONTEXT.get() is None
    asyncio.run(run())


def test_savepoint_rollback_preserves_outer_but_not_inner_events(monkeypatch, tmp_path):
    async def run():
        async with collecting(monkeypatch, tmp_path) as (sessions, collector):
            async with sessions() as session:
                obs.stage(session, obs.observation("request", "test", "success", event_key="outer"))
                nested = await session.begin_nested()
                obs.stage(session, obs.observation("request", "test", "success", event_key="inner"))
                await nested.rollback()
                await session.commit()
            await collector.flush()
            async with sessions() as session:
                assert list(await session.scalars(select(ObservabilityEvent.event_key))) == ["outer"]
    asyncio.run(run())


def test_savepoint_commit_waits_for_outer_commit_or_rollback(monkeypatch, tmp_path):
    async def run():
        async with collecting(monkeypatch, tmp_path) as (sessions, collector):
            async with sessions() as session:
                await session.begin()
                nested = await session.begin_nested()
                obs.stage(session, obs.observation("request", "test", "success", event_key="inner"))
                await nested.commit()
                assert collector.counters["enqueued"] == 0
                await session.rollback()
            async with sessions() as session:
                await session.begin()
                nested = await session.begin_nested()
                obs.stage(session, obs.observation("request", "test", "success", event_key="committed"))
                await nested.commit()
                await session.commit()
            await collector.flush()
            async with sessions() as session:
                assert list(await session.scalars(select(ObservabilityEvent.event_key))) == ["committed"]
    asyncio.run(run())


def test_model_consumer_close_releases_source_and_records_cancel(monkeypatch, tmp_path):
    async def run():
        async with collecting(monkeypatch, tmp_path) as (sessions, collector):
            closed = []
            @obs.observe_model_stream
            async def source(model, *, usage_sink):
                usage_sink["_invoked"] = True
                try:
                    yield "answer"
                    await asyncio.sleep(60)
                finally:
                    closed.append(True)
            stream = source(model(), usage_sink=None)
            assert await anext(stream) == "answer"
            await stream.aclose()
            assert closed == [True]
            await collector.flush()
            async with sessions() as session:
                row = await session.scalar(select(ObservabilityEvent))
                assert row.status == "cancelled" and row.total_tokens is None
    asyncio.run(run())


def test_shutdown_counts_queued_and_inflight_rows(monkeypatch, tmp_path):
    async def run():
        async with collecting(monkeypatch, tmp_path) as (_, collector):
            monkeypatch.setattr(obs.get_settings(), "observability_write_timeout", 5)
            entered = asyncio.Event()
            async def hanging(_):
                entered.set()
                await asyncio.sleep(60)
            monkeypatch.setattr(collector, "_write", hanging)
            collector.submit(obs.observation("request", "test", "success"))
            collector.submit(obs.observation("request", "test", "success"))
            await entered.wait()
            started = time.monotonic()
            await collector.stop()
            assert time.monotonic() - started < 3
            assert collector.counters["shutdownDropped"] == 2
            assert collector.queue.qsize() == 0
            await collector.flush()
    asyncio.run(run())


def test_real_sqlite_lock_drops_observation_without_rolling_back_business(monkeypatch, tmp_path):
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    async def run():
        async with collecting(monkeypatch, tmp_path) as (sessions, collector):
            fast_engine = create_async_engine(
                "sqlite+aiosqlite:///" + (tmp_path / "fixture.db").as_posix(),
                connect_args={"timeout": 0.05})
            monkeypatch.setattr(collector, "sessions", async_sessionmaker(fast_engine, expire_on_commit=False))
            try:
                async with sessions() as session:
                    await session.execute(text("BEGIN IMMEDIATE"))
                    session.add(ToolAudit(id=804, tenant_id=1, user_id=11, tool_name="calculator",
                                          outcome="success", input_digest="digest", duration_ms=1))
                    await session.flush()
                    collector.submit(obs.observation("request", "test", "success", event_key="locked"))
                    await collector.flush()
                    assert collector.counters["writeFailures"] == 1
                    await session.commit()
                await collector.flush()
                async with sessions() as session:
                    assert await session.get(ToolAudit, 804) is not None
                    assert await session.get(ObservabilityEvent, "locked") is None
            finally:
                await fast_engine.dispose()
    asyncio.run(run())
