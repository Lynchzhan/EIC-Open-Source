"""真实SQLite竞争回归；模型数据为合成内容，不产生网络请求。"""
import asyncio
import json
import sqlite3

import pytest

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from sqlalchemy.exc import OperationalError

from eic.core import observability as obs
from eic.models import AIModel, ChatMessage, ObservabilityEvent
from test_id_generation_safety import fixture_db
from test_candidate_langfuse_delivery import exporter as durable_exporter


def test_retrieval_stage_survives_short_sqlite_writer_lock(monkeypatch, tmp_path):
    async def run():
        async with fixture_db(monkeypatch, tmp_path, is_super_admin=True) as sessions:
            engine = create_async_engine('sqlite+aiosqlite:///' + (tmp_path / 'fixture.db').as_posix(),
                                         poolclass=NullPool, connect_args={'timeout': 0.02})
            collector = obs.Collector()
            monkeypatch.setattr(obs, 'collector', collector)
            await collector.start(async_sessionmaker(engine, expire_on_commit=False))
            token = obs.CONTEXT.set({'tenant_id': 1, 'request_id': 'fixture'})
            try:
                async with sessions() as writer:
                    await writer.execute(text('BEGIN IMMEDIATE'))
                    with obs.observe_retrieval_stage('dense_recall', tenant_id=1):
                        pass
                    await asyncio.sleep(0.65)
                    await writer.rollback()
                await collector.flush()
                async with sessions() as session:
                    rows = list(await session.scalars(select(ObservabilityEvent).where(
                        ObservabilityEvent.stage == 'dense_recall')))
                assert len(rows) == 1, dict(collector.counters)
            finally:
                obs.CONTEXT.reset(token)
                await collector.stop()
                await engine.dispose()
    asyncio.run(run())


def test_long_synthetic_stream_with_real_collector(monkeypatch, tmp_path):
    from eic import main
    from eic.core import generation

    async def run():
        async with fixture_db(monkeypatch, tmp_path, is_super_admin=True) as sessions:
            async with sessions() as session:
                session.add(AIModel(id=201, tenant_id=1, model_type='chat', provider='openai',
                                    model_name='fixture', endpoint='http://fixture.invalid', enabled=True))
                await session.commit()
            engine = create_async_engine('sqlite+aiosqlite:///' + (tmp_path / 'fixture.db').as_posix(),
                                         poolclass=NullPool, connect_args={'timeout': 0.1})
            collector = obs.Collector()
            monkeypatch.setattr(obs, 'collector', collector)
            await collector.start(async_sessionmaker(engine, expire_on_commit=False))
            calls = []

            async def provider(model, question, **kwargs):
                calls.append(model.id)
                for _ in range(240):
                    yield '片段'
                    await asyncio.sleep(0.005)

            monkeypatch.setattr(main, 'stream_chat_completion', provider)
            try:
                chunks = await asyncio.wait_for(collect(main.sse_events(
                    'fixture', main.Principal(11, 1, 'fixture', True))), 40)
                terminals = [json.loads(chunk.split('data: ', 1)[1])
                             for chunk in chunks if 'event: done\n' in chunk]
                assert len(terminals) == 1 and terminals[0]['status'] == 'completed'
                assert calls == [201]
                async with sessions() as session:
                    assert (await session.get(ChatMessage, 301)).status == 'completed'
            finally:
                await generation.stop_producers()
                await collector.stop()
                await engine.dispose()
    asyncio.run(run())


async def collect(source):
    return [chunk async for chunk in source]


def test_observation_retry_is_bounded_and_does_not_retry_other_errors(monkeypatch):
    async def run():
        collector = obs.Collector()
        attempts = []

        async def locked(row):
            attempts.append(row)
            error = sqlite3.OperationalError('database is locked')
            error.sqlite_errorcode = sqlite3.SQLITE_BUSY
            raise OperationalError('private sql', {}, error)

        monkeypatch.setattr(collector, '_write', locked)
        with pytest.raises(TimeoutError):
            async with asyncio.timeout(0.08):
                await collector._write_with_retry({})
        assert 1 < len(attempts) < 10

        async def invalid(row):
            raise OperationalError('private sql', {}, sqlite3.OperationalError('no such table'))

        monkeypatch.setattr(collector, '_write', invalid)
        before = collector.counters['writeRetries']
        with pytest.raises(OperationalError):
            await collector._write_with_retry({})
        assert collector.counters['writeRetries'] == before
    asyncio.run(run())


def test_default_execution_deadline_respects_deployment_and_explicit_policy():
    from eic.ai.runtime_policy import execution_timeout_ms
    assert execution_timeout_ms(None, 180) == 180_000
    assert execution_timeout_ms({'maxSeconds': 10}, 180) == 10_000
    assert execution_timeout_ms({'maxSeconds': 300}, 180) == 300_000


def test_sqlite_attempt_timeout_retries_without_replaying_business(monkeypatch):
    async def run():
        collector = obs.Collector()
        collector.sqlite = True
        attempts = []

        async def write(row):
            attempts.append(row['event_key'])
            if len(attempts) == 1:
                raise TimeoutError

        monkeypatch.setattr(collector, '_write', write)
        await collector._write_with_retry({'event_key': 'same-event'})
        assert attempts == ['same-event', 'same-event']
        assert collector.counters['writeTimeoutRetries'] == 1
    asyncio.run(run())


def test_duplicate_after_uncertain_commit_exports_persisted_row(monkeypatch, tmp_path):
    async def run():
        async with fixture_db(monkeypatch, tmp_path) as sessions:
            collector = obs.Collector()
            collector.sessions = sessions
            sender = durable_exporter(monkeypatch)
            monkeypatch.setattr(obs, 'langfuse_exporter', sender)
            row = obs.observation('model_call', 'embedding', 'success', tenant_id=1,
                                  event_key='stable', summary={'modelName': 'actual-model'})
            await collector._write(row)
            await collector._write({**row, 'summary': {'modelName': 'wrong-model'}})
            assert collector.counters['written'] == 1
            assert collector.counters['duplicates'] == 1
            from eic.core.langfuse_delivery import dispatch_one
            assert await dispatch_one(sessions, sender)
            assert not await dispatch_one(sessions, sender)
            sender.deliver_persisted.assert_awaited_once()
            assert sender.deliver_persisted.call_args.args[0]['summary'] == {'modelName': 'actual-model'}
    asyncio.run(run())
