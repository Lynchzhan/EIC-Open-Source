"""C3 worker durable graph recovery; offline, read-only knowledge-check only."""
import asyncio
import json
import os
import signal
import subprocess
import sys
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from eic.models import Base, KnowledgeBase, Resource, ScheduledTask, TaskGraphCheckpoint, TaskGraphLease, TaskRun, Tenant, User
from eic.core.ids import initialize_allocator
from eic.core import task_graph_runtime as runtime


@asynccontextmanager
async def db(tmp_path):
    engine = create_async_engine('sqlite+aiosqlite:///' + (tmp_path / 'c3.db').as_posix(), connect_args={'timeout': 30})
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with sessions() as s:
        s.add_all([Tenant(id=1, code=1, name='c3'), User(id=11, tenant_id=1, username='c3', password_hash='x', is_super_admin=True),
                   Resource(id=100, tenant_id=1, owner_id=11, name='kb', resource_type='knowledge_bases', data={'enabled': True}),
                   KnowledgeBase(id=100, tenant_id=1, name='kb'),
                   ScheduledTask(id=200, tenant_id=1, owner_id=11, name='task', cron='* * * * *',
                                 payload={'handlerType': 'knowledge-check', 'knowledge_base_id': 100})])
        await s.flush(); await initialize_allocator(s)
        s.add(TaskRun(id=300, tenant_id=1, task_id=200, schedule_revision=0,
                      input_snapshot={'handlerType': 'knowledge-check', 'knowledge_base_id': 100}, status='pending'))
        await s.commit()
    try:
        yield sessions
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_two_workers_one_atomic_claim_and_expired_fencing(tmp_path, monkeypatch):
    async with db(tmp_path) as sessions:
        monkeypatch.setattr(runtime, 'get_settings', lambda: type('S', (), {'task_execution_enabled': True})())
        a, b = runtime.TaskRuntime(sessions, 200, 1, 300), runtime.TaskRuntime(sessions, 200, 1, 300)
        assert sorted(await asyncio.gather(a.claim(), b.claim())) == ['already_claimed', 'claimed']
        async with sessions() as s:
            lease = await s.get(TaskGraphLease, 300)
            lease.expires_at = datetime.now(UTC) - timedelta(seconds=1)
            await s.commit()
        c = runtime.TaskRuntime(sessions, 200, 1, 300)
        assert await c.claim() == 'claimed' and c.version == 2
        with pytest.raises(runtime.TaskLeaseLost):
            await a.guard()


@pytest.mark.asyncio
async def test_revoked_schedule_and_unknown_checkpoint_fail_closed(tmp_path, monkeypatch):
    async with db(tmp_path) as sessions:
        monkeypatch.setattr(runtime, 'get_settings', lambda: type('S', (), {'task_execution_enabled': True})())
        worker = runtime.TaskRuntime(sessions, 200, 1, 300)
        assert await worker.claim() == 'claimed'
        from langgraph.checkpoint.memory import InMemorySaver
        from eic.ai.shared_executor import build_execution_graph, ExecutionState
        from eic.ai.agent_graph import AGENT_GRAPH_VERSION
        graph = build_execution_graph(InMemorySaver())
        state = ExecutionState(user_message='x', agent_run_id='task:1:300', graph_version=AGENT_GRAPH_VERSION,
            stop_reason='', agent_step_count=0, tool_call_count=0, tool_calls=[], tool_results=[], tool_errors=[],
            pending_calls=[], completed_calls={}, trace_events=[], provider_outcome='known', effect_status='unknown')
        saver = runtime.TaskGraphCheckpointer(worker)
        config = await saver.aput({'configurable': {'thread_id': 'task:1:300'}}, {'id': 'cp-unknown', 'channel_values': state}, {}, {})
        with pytest.raises(ValueError, match='task_checkpoint_outcome_unknown'):
            await saver.aget_tuple(config)
        async with sessions() as s:
            await s.execute(update(ScheduledTask).where(ScheduledTask.id == 200).values(schedule_revision=1))
            await s.commit()
        with pytest.raises(runtime.TaskLeaseLost):
            await worker.guard()


@pytest.mark.asyncio
async def test_typed_checkpoint_roundtrip_and_identity_version_rejection(tmp_path, monkeypatch):
    async with db(tmp_path) as sessions:
        monkeypatch.setattr(runtime, 'get_settings', lambda: type('S', (), {'task_execution_enabled': True})())
        worker = runtime.TaskRuntime(sessions, 200, 1, 300)
        assert await worker.claim() == 'claimed'
        saver = runtime.TaskGraphCheckpointer(worker)
        typed = datetime(2026, 10, 15, tzinfo=UTC)
        cp = {'id': 'typed-cp', 'channel_values': {'typed': typed, 'effect_status': 'none'}}
        config = await saver.aput(saver.config(), cp, {'step': 0}, {})
        await saver.aput_writes(config, [('fixture', typed)], 'fixture-task')
        restored = await saver.aget_tuple(config)
        assert restored.checkpoint['channel_values']['typed'] == typed
        assert restored.pending_writes == [('fixture-task', 'fixture', typed)]
        with pytest.raises(ValueError, match='thread_mismatch'):
            await saver.aget_tuple({'configurable': {'thread_id': 'task:2:300'}})
        async with sessions() as s:
            row = await s.get(TaskGraphCheckpoint, (300, '', 'typed-cp'))
            row.schema_version = 2
            await s.commit()
        with pytest.raises(ValueError, match='identity_or_version'):
            await saver.aget_tuple(config)


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['permission', 'cancel', 'input', 'owner', 'disabled'])
async def test_every_node_refreshes_authorization(tmp_path, monkeypatch, change):
    async with db(tmp_path) as sessions:
        monkeypatch.setattr(runtime, 'get_settings', lambda: type('S', (), {'task_execution_enabled': True})())
        worker = runtime.TaskRuntime(sessions, 200, 1, 300)
        assert await worker.claim() == 'claimed'
        async with sessions() as s:
            if change == 'permission':
                (await s.get(User, 11)).is_active = False
            elif change == 'cancel':
                (await s.get(TaskRun, 300)).status = 'cancelled'
            elif change == 'input':
                (await s.get(TaskRun, 300)).input_snapshot = {'handlerType': 'knowledge-check', 'knowledge_base_id': 101}
            elif change == 'owner':
                (await s.get(ScheduledTask, 200)).owner_id = 12
            else:
                (await s.get(ScheduledTask, 200)).enabled = False
            await s.commit()
        with pytest.raises(runtime.TaskLeaseLost):
            await worker.guard()


def child_guard():
    import atexit
    sys.path.insert(0, os.environ['C3_GUARD_ROOT'])
    import offline_network_guard as guard
    import b4_run_guard as transport
    assert guard.socket.socket.connect is guard._guard_connect
    assert guard.httpx.HTTPTransport.handle_request is transport.deny_sync
    def report():
        path = os.environ.get('C3_CHILD_NETWORK')
        if path:
            from pathlib import Path
            Path(path).write_text(json.dumps({'guardLoaded': True, 'external': dict(guard._network_attempts),
                'realHttp': transport.real_http_attempts, 'exporterDisabled': os.environ['OTEL_SDK_DISABLED']}), encoding='utf-8')
    atexit.register(report)
    report()


def test_0030_upgrade_schema_and_downgrade(tmp_path):
    import importlib.util
    from pathlib import Path
    from sqlalchemy import create_engine, inspect
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    path = Path(__file__).parents[1]/'migrations/versions/0030_task_graph_recovery.py'
    spec = importlib.util.spec_from_file_location('c3_migration', path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    assert migration.down_revision == '0029_tool_execution_intents'
    engine = create_engine('sqlite:///'+(tmp_path/'migration.db').as_posix())
    with engine.begin() as conn:
        migration.op = Operations(MigrationContext.configure(conn))
        migration.upgrade()
        for name in ('task_graph_leases', 'task_graph_checkpoints'):
            actual = {c['name'] for c in inspect(conn).get_columns(name)}
            assert actual == set(Base.metadata.tables[name].columns.keys())
        migration.downgrade()
        assert not {'task_graph_leases', 'task_graph_checkpoints'} & set(inspect(conn).get_table_names())
    engine.dispose()


async def child_worker(db_path, marker, mode):
    from pathlib import Path
    from eic import workers
    from eic.ai import shared_executor as shared
    from eic.core import tasks
    engine = create_async_engine('sqlite+aiosqlite:///'+Path(db_path).as_posix(), connect_args={'timeout': 30})
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    workers.SessionLocal = sessions
    workers.get_settings().task_execution_enabled = True
    original = shared.record_tool_result
    if mode in {'kill', 'unknown'}:
        async def blocked(state):
            Path(marker).write_text('checkpoint-persisted', encoding='utf-8')
            await asyncio.Event().wait()
        shared.record_tool_result = blocked
    if mode == 'unknown':
        original_execute = shared.execute_tool
        async def uncertain(state):
            value = await original_execute(state)
            value['effect_status'] = 'unknown'
            value['active_result']['effectStatus'] = 'unknown'
            return value
        shared.execute_tool = uncertain
    calls = Path(marker + '.calls')
    original_handler = tasks.execute_handler
    async def handler(*args):
        with calls.open('a', encoding='utf-8') as out:
            out.write('handler\n')
        if mode == 'legacy':
            Path(marker).write_text('legacy-uncommitted-running', encoding='utf-8')
            await asyncio.Event().wait()
        return await original_handler(*args)
    tasks.execute_handler = handler
    if mode == 'legacy':
        # 显式合成旧式未提交事务，不声称运行已删除的历史源码。
        async with sessions() as session:
            run = await session.get(TaskRun, 300)
            run.status = 'running'
            await session.flush()
            await handler()
        raise AssertionError('legacy fixture must be killed before commit')
    result = await workers.run_scheduled_task({}, 200, 1, 300)
    Path(marker+'.result').write_text(json.dumps(result), encoding='utf-8')
    await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['kill', 'unknown', 'legacy'])
async def test_os_kill_checkpoint_recovers_known_read_only_without_replay(tmp_path, monkeypatch, mode):
    from pathlib import Path
    assert os.environ.get('C3_GUARD_ROOT'), 'Run under C3 offline harness'
    async with db(tmp_path) as sessions:
        marker = tmp_path/'marker'
        command = [sys.executable, '-X', 'utf8', '-B', str(Path(__file__).resolve()),
                   str(tmp_path/'c3.db'), str(marker), mode]
        env = {**os.environ, 'C3_CHILD_NETWORK': str(tmp_path/'child-first-network.json')}
        with (tmp_path/'child-first.stdout').open('w') as out, (tmp_path/'child-first.stderr').open('w') as err:
            child = subprocess.Popen(command, env=env, stdout=out, stderr=err)
            try:
                async with asyncio.timeout(30):
                    while not marker.exists():
                        assert child.poll() is None, (tmp_path/'child-first.stderr').read_text()
                        await asyncio.sleep(.05)
                async with sessions() as s:
                    assert (await s.get(TaskRun, 300)).status == ('pending' if mode == 'legacy' else 'running')
                    assert bool(list(await s.scalars(select(TaskGraphCheckpoint)))) == (mode != 'legacy')
                child.kill()
                await asyncio.to_thread(child.wait, 15)
            finally:
                if child.poll() is None:
                    child.kill(); child.wait()
        assert child.returncode != 0
        async with sessions() as s:
            lease = await s.get(TaskGraphLease, 300)
            if lease:
                lease.expires_at = datetime.now(UTC)-timedelta(seconds=60)
            assert (await s.get(TaskRun, 300)).status == ('pending' if mode == 'legacy' else 'running')
            await s.commit()
        env['C3_CHILD_NETWORK'] = str(tmp_path/'child-resume-network.json')
        result = await asyncio.to_thread(subprocess.run, [*command[:-1], 'resume'], env=env,
            capture_output=True, text=True, timeout=40)
        (tmp_path/'child-resume.stdout').write_text(result.stdout, encoding='utf-8')
        (tmp_path/'child-resume.stderr').write_text(result.stderr, encoding='utf-8')
        assert result.returncode == 0, result.stderr
        expected = 'uncertain' if mode == 'unknown' else 'success'
        assert json.loads(Path(str(marker)+'.result').read_text())['status'] == expected
        assert len(Path(str(marker)+'.calls').read_text().splitlines()) == (2 if mode == 'legacy' else 1)
        async with sessions() as s:
            run = await s.get(TaskRun, 300)
            assert run.status == expected
            if expected == 'success':
                assert run.result['report']['readOnly']
            assert (await s.get(TaskGraphLease, 300)).fencing_version == (1 if mode == 'legacy' else 2)
        for filename in ('child-first-network.json', 'child-resume-network.json'):
            guard = json.loads((tmp_path/filename).read_text())
            assert guard['guardLoaded'] and guard['external'] == {} and guard['realHttp'] == []


if __name__ == '__main__':
    child_guard()
    asyncio.run(child_worker(*sys.argv[1:]))
