"""离线候选断言；SQLite真实事务，队列替身，不运行Provider或真实Redis。"""
import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from eic.core import outbox, queue
from eic.core.ids import initialize_allocator
from eic.models import Base, Tenant, User, AgentMemory, OutboxEvent, IdAllocator


@asynccontextmanager
async def fixture_db(monkeypatch, tmp_path, *, broker=False):
    engine = create_async_engine('sqlite+aiosqlite:///' + (tmp_path / 'candidate013.db').as_posix())
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    clock = [datetime(2030, 1, 1, tzinfo=UTC)]
    settings = SimpleNamespace(environment='production' if broker else 'cloud-lite',
        database_url='postgresql+asyncpg://unused/fixture' if broker else 'sqlite+aiosqlite:///fixture',
        task_execution_enabled=False)
    async def now(session):
        return clock[0]
    monkeypatch.setattr(outbox, 'SessionLocal', sessions)
    monkeypatch.setattr(outbox, 'get_settings', lambda: settings)
    monkeypatch.setattr(outbox, 'now', now)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with sessions() as session:
        session.add(Tenant(id=1, code=1, name='candidate013'))
        await session.flush()
        session.add(User(id=11, tenant_id=1, username='fixture', password_hash='no-login',
                         is_super_admin=True, is_active=True))
        await session.flush()
        for memory_id in (1001, 1002):
            session.add(AgentMemory(id=memory_id, tenant_id=1, owner_id=11, content='synthetic',
                memory_key='candidate013-' + str(memory_id), memory_type='long_term', status='pending'))
        await session.flush()
        await initialize_allocator(session)
        await session.commit()
    try:
        yield sessions, clock
    finally:
        await engine.dispose()


async def state(sessions, event_id):
    async with sessions() as session:
        event = await session.get(OutboxEvent, event_id)
        allocator = await session.get(IdAllocator, 'global')
        return (event.status, event.consume_status, event.attempts, event.owner_token,
                event.job_id, event.result, event.available_at, event.next_attempt_at, allocator.value)


@pytest.mark.parametrize('status', ['pending', 'sent', 'sending'])
def test_future_inline_scan_and_direct_cas_do_not_dispatch(monkeypatch, tmp_path, status):
    async def run():
        async with fixture_db(monkeypatch, tmp_path) as (sessions, clock):
            consumer = AsyncMock(side_effect=AssertionError('future consumer invoked'))
            broker = AsyncMock(side_effect=AssertionError('inline used broker'))
            monkeypatch.setattr(outbox, 'consume_event', consumer)
            monkeypatch.setattr(queue, 'enqueue_outbox', broker)
            async with sessions() as session:
                event_id = await outbox.stage(session, 'memory-write', 1001, 1,
                    available_at=clock[0] + timedelta(hours=1))
                event = await session.get(OutboxEvent, event_id)
                event.status = status
                if status == 'sending':
                    event.lease_until = clock[0] - timedelta(seconds=1)
                    event.owner_token = 'expired-owner'
                await session.commit()
            before = await state(sessions, event_id)
            for _ in range(3):
                assert await outbox.dispatch_pending(kind='memory-write') == 0
                await outbox.dispatch_one(event_id)
                assert await state(sessions, event_id) == before
            consumer.assert_not_awaited()
            broker.assert_not_awaited()
    asyncio.run(run())


def test_due_memory_write_completes_and_future_becomes_due_once(monkeypatch, tmp_path):
    async def run():
        async with fixture_db(monkeypatch, tmp_path) as (sessions, clock):
            broker = AsyncMock(side_effect=AssertionError('memory-write used broker'))
            monkeypatch.setattr(queue, 'enqueue_outbox', broker)
            async with sessions() as session:
                future = await outbox.stage(session, 'memory-write', 1002, 1,
                    available_at=clock[0] + timedelta(hours=1))
                first = await outbox.stage(session, 'memory-write', 1001, 1)
                await session.commit()
            # 低ID未来任务不能占用limit，阻塞已到期任务。
            assert await outbox.dispatch_pending(limit=1, kind='memory-write') == 1
            async with sessions() as session:
                immediate = await session.get(OutboxEvent, first)
                later = await session.get(OutboxEvent, future)
                assert immediate.consume_status == 'completed' and immediate.attempts == 1
                assert (await session.get(AgentMemory, 1001)).status == 'active'
                assert later.status == 'pending' and later.attempts == 0
            clock[0] += timedelta(hours=1)  # 等于available_at即允许；无需真实等待。
            assert await outbox.dispatch_pending(kind='memory-write') == 1
            async with sessions() as session:
                later = await session.get(OutboxEvent, future)
                assert later.consume_status == 'completed' and later.attempts == 1
                assert (await session.get(AgentMemory, 1002)).status == 'active'
            settled = await state(sessions, future)
            assert await outbox.dispatch_pending(kind='memory-write') == 0
            assert await state(sessions, future) == settled
            broker.assert_not_awaited()
    asyncio.run(run())


def test_cas_rechecks_available_at_after_scanner_selection(monkeypatch, tmp_path):
    async def run():
        async with fixture_db(monkeypatch, tmp_path) as (sessions, clock):
            consumer = AsyncMock(side_effect=AssertionError('rescheduled event consumed'))
            monkeypatch.setattr(outbox, 'consume_event', consumer)
            async with sessions() as session:
                event_id = await outbox.stage(session, 'memory-write', 1001, 1)
                await session.commit()
            real_dispatch = outbox.dispatch_one
            async def reschedule_then_dispatch(identity):
                async with sessions() as session:
                    event = await session.get(OutboxEvent, identity)
                    event.available_at = clock[0] + timedelta(hours=1)
                    await session.commit()
                return await real_dispatch(identity)
            monkeypatch.setattr(outbox, 'dispatch_one', reschedule_then_dispatch)
            assert await outbox.dispatch_pending(kind='memory-write') == 0
            async with sessions() as session:
                event = await session.get(OutboxEvent, event_id)
                assert event.status == 'pending' and event.attempts == 0
            consumer.assert_not_awaited()
    asyncio.run(run())


def test_broker_keeps_future_enqueue_and_defer_until(monkeypatch, tmp_path):
    async def run():
        async with fixture_db(monkeypatch, tmp_path, broker=True) as (sessions, clock):
            consumer = AsyncMock(side_effect=AssertionError('broker event ran inline'))
            broker = AsyncMock(return_value='stable-synthetic-job')
            monkeypatch.setattr(outbox, 'consume_event', consumer)
            monkeypatch.setattr(queue, 'enqueue_outbox', broker)
            due = clock[0] + timedelta(hours=1)
            async with sessions() as session:
                event_id = await outbox.stage(session, 'memory-index', 1001, 1, available_at=due)
                await session.commit()
            assert await outbox.dispatch_pending(kind='memory-index') == 1
            args, kwargs = broker.await_args
            assert args == (event_id, 1) and kwargs['available_at'] == due
            consumer.assert_not_awaited()
            async with sessions() as session:
                event = await session.get(OutboxEvent, event_id)
                assert event.status == 'sent' and event.attempts == 1
    asyncio.run(run())


@pytest.mark.parametrize('environment,url,kind,expected', [
    ('production', 'postgresql+asyncpg://unused/db', 'memory-write', True),
    ('production', 'postgresql+asyncpg://unused/db', 'memory-index', False),
    ('cloud-lite', 'postgresql+asyncpg://unused/db', 'memory-index', True),
    ('production', 'sqlite+aiosqlite:///unused', 'memory-index', True),
    ('test', 'postgresql+asyncpg://unused/db', 'conversation-label', True),
    ('production', 'sqlite+aiosqlite:///unused', 'scheduled-task', False),
    ('cloud-lite', 'sqlite+aiosqlite:///unused', 'knowledge-base', True),
    ('production', 'postgresql+asyncpg://unused/db', 'knowledge-base', False),
    ('production', 'postgresql+asyncpg://unused/db', 'memory-projection', False),
    ('cloud-lite', 'sqlite+aiosqlite:///unused', 'memory-projection', True),
])
def test_inline_classification_matches_lite_and_broker_contract(environment, url, kind, expected):
    assert (kind in outbox._inline_kinds(SimpleNamespace(environment=environment, database_url=url))) is expected
