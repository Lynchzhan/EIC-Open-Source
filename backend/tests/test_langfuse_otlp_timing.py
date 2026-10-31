"""实测耗时、瞬时事件与链接映射的纯本地契约验证。"""
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from eic.core.langfuse_otlp import build_completed_spans, otlp_trace_id
from eic.core.langfuse_contract import build_ingestion_events


def row(**values):
    return dict(tenant_id=1, trace_id='fixture-trace', span_id='a' * 32,
                parent_span_id='b' * 32, event_key='fixture-event', occurred_at=datetime.now(UTC),
                category='agent_run', source='knowledge_pipeline', stage='document_parse',
                status='completed', duration_ms=12, **values)


def test_measured_stage_has_real_duration_and_parent():
    value = row()
    span = build_completed_spans(value, environment='test', namespace='fixture')[0]['body']
    assert int(span['endTimeUnixNano']) - int(span['startTimeUnixNano']) == 12000000
    assert span['parentSpanId'] != span['spanId']
    legacy = build_ingestion_events(value, environment='test', namespace='fixture')
    assert span['traceId'] == otlp_trace_id(legacy[0]['body']['id'])


@pytest.mark.parametrize('status,code', [('started', 0), ('cancelled', 0), ('failed', 2)])
def test_instant_event_does_not_invent_duration_or_success(status, code):
    value = row()
    value.update(source='agent_runs', stage='terminal', status=status, duration_ms=None, parent_span_id=None)
    span = build_completed_spans(value, environment='test', namespace='fixture')[0]['body']
    assert span['startTimeUnixNano'] == span['endTimeUnixNano']
    assert span['status']['code'] == code
    assert 'parentSpanId' not in span
    assert any(a['key'] == 'langfuse.observation.type' and a['value']['stringValue'] == 'event' for a in span['attributes'])


def test_unknown_pipeline_stage_is_filtered():
    value = row()
    value['stage'] = 'document-content-must-not-export'
    assert build_completed_spans(value, environment='test', namespace='fixture') == []


def test_trace_link_uses_actual_otlp_identity(monkeypatch):
    from eic.core import langfuse_query
    monkeypatch.setenv('EIC_LANGFUSE_TRANSPORT', 'otlp_v4')
    monkeypatch.setattr(langfuse_query, 'get_settings', lambda: SimpleNamespace(
        langfuse_enabled=True, langfuse_ui_base_url='https://us.cloud.langfuse.com',
        langfuse_project_id='fixture', environment='test', langfuse_namespace='fixture'))
    value = row()
    value['project'] = 'eic'
    actual = langfuse_query.trace_details({'admin': True, 'principal': SimpleNamespace(tenant_id=1)}, [SimpleNamespace(**value)])
    span = build_completed_spans(value, environment='test', namespace='fixture')[0]['body']
    assert actual['url'].endswith('/traces/' + span['traceId'])


def test_superseded_ingestion_is_separate_instant_receipt_under_failed_run():
    value = row()
    value.update(source='knowledge_ingestion_runs', stage='ingestion', status='failed',
                 run_id='ingestion:42', parent_span_id=None)
    failed = build_completed_spans(value, environment='test', namespace='fixture')[0]['body']
    value.update(status='superseded', event_key='superseded-event')
    receipt = build_completed_spans(value, environment='test', namespace='fixture')[0]['body']
    assert receipt['spanId'] != failed['spanId']
    assert receipt['parentSpanId'] == failed['spanId']
    assert receipt['startTimeUnixNano'] == receipt['endTimeUnixNano']
    assert receipt['name'] == 'eic.ingestion_state'
    attrs = {item['key']: item['value'] for item in receipt['attributes']}
    assert attrs['langfuse.observation.type'] == {'stringValue': 'event'}
    assert attrs['langfuse.observation.metadata.status'] == {'stringValue': 'superseded'}
    assert 'langfuse.observation.metadata.duration_ms' not in attrs
    assert failed['status']['code'] == 2


@pytest.mark.parametrize('overrides', [
    {'trace_id': 'separate-trace'}, {'request_id': 'separate-request'},
    {'trace_id': 'separate-trace', 'request_id': 'separate-request'},
])
def test_explicit_scope_does_not_inherit_an_unrelated_parent_or_owner(overrides):
    from eic.core import observability as obs
    token = obs.CONTEXT.set({'tenant_id': 1, 'trace_id': 'old-trace',
        'request_id': 'old-request', 'span_id': 'a' * 32, 'owner_key': 'b' * 64})
    try:
        value = obs.observation('agent_run', 'memory_index', 'completed',
            tenant_id=1, source='memory_ops', **overrides)
        assert 'parent_span_id' not in value
        assert 'owner_key' not in value
    finally:
        obs.CONTEXT.reset(token)


@pytest.mark.parametrize('status', ['completed', 'uncertain', 'failed', 'cancelled'])
def test_context_job_terminal_root_matches_the_provider_parent(monkeypatch, status):
    from eic.core import observability as obs
    from eic.core.langfuse_context import turn_context
    from eic.models import AgentRun
    from datetime import timedelta
    rows = []
    monkeypatch.setattr(obs.collector, 'running', True)
    monkeypatch.setattr(obs, 'stage', lambda session, value: rows.append(value))
    scope = turn_context(tenant_id=1, request_id='context:42')
    stamp = datetime.now(UTC)
    run = AgentRun(run_id='context:fixture', tenant_id=1, session_id=101,
        request_id='context:42', trace_id=scope['trace_id'], graph_version='fixture',
        status=status, current_node='model_decision', event_seq=1,
        run_metadata={'contextJobId': 42}, started_at=stamp,
        finished_at=stamp + timedelta(seconds=1), error_code='')
    token = obs.CONTEXT.set(scope)
    try:
        obs._business_events(SimpleNamespace(new=[run], dirty=[]), None)
        provider = obs.observation('model_call', 'model', 'success', source='provider_stream',
            span_id='c' * 32, occurred_at=stamp, duration_ms=100)
    finally:
        obs.CONTEXT.reset(token)
    root = build_completed_spans(rows[0], environment='test', namespace='fixture')[0]['body']
    child = build_completed_spans(provider, environment='test', namespace='fixture')[0]['body']
    assert root['traceId'] == child['traceId']
    assert root['spanId'] == child['parentSpanId']
    assert 'parentSpanId' not in root
    assert root['startTimeUnixNano'] != root['endTimeUnixNano']
    assert root['status']['code'] == (1 if status == 'completed' else 2 if status == 'failed' else 0)


@pytest.mark.parametrize('status', ['completed', 'uncertain'])
def test_memory_pipeline_root_is_measured_and_matches_its_children(status):
    from eic.core import observability as obs
    from eic.core.langfuse_context import turn_context
    scope = turn_context(tenant_id=1, request_id='memory-index:42')
    token = obs.CONTEXT.set(scope)
    try:
        value = obs.observation('agent_run', 'memory_index', status,
            source='memory_pipeline', span_id=scope['span_id'], duration_ms=123)
        provider = obs.observation('model_call', 'embedding', 'success', source='provider_response',
            span_id='c' * 32, duration_ms=100)
    finally:
        obs.CONTEXT.reset(token)
    root = build_completed_spans(value, environment='test', namespace='fixture')[0]['body']
    child = build_completed_spans(provider, environment='test', namespace='fixture')[0]['body']
    assert root['spanId'] == child['parentSpanId'] and root['traceId'] == child['traceId']
    assert int(root['endTimeUnixNano']) - int(root['startTimeUnixNano']) == 123000000
    assert root['status']['code'] == (1 if status == 'completed' else 0)


def test_memory_pipeline_unknown_stage_is_filtered():
    value = row()
    value.update(source='memory_pipeline', stage='private_content')
    assert build_completed_spans(value, environment='test', namespace='fixture') == []


@pytest.mark.parametrize('outcome', ['success', 'exception', 'cancelled', 'caller_cancelled'])
async def test_context_job_actual_graph_persists_one_trace_with_a_real_root(monkeypatch, tmp_path, outcome):
    import asyncio
    from langgraph.errors import NodeCancelledError
    from datetime import timedelta
    from sqlalchemy import select
    from eic.core import context_jobs, observability as obs
    from eic.core.memory_lifecycle import record
    from eic.models import AIModel, AgentRun, ContextJob, ObservabilityEvent, OutboxEvent
    from test_fv2_observability_collection import collecting
    async def no_export(*args, **kwargs):
        return None
    monkeypatch.setattr(obs.langfuse_exporter, 'start', no_export)
    async with collecting(monkeypatch, tmp_path) as (sessions, collector):
        async with sessions() as session:
            model = AIModel(id=801, tenant_id=1, model_type='chat', provider='openai_compatible',
                model_name='fixture', endpoint='http://fixture.invalid/v1', enabled=True,
                max_input_tokens=4096, max_output_tokens=256)
            session.add(model)
            session.add(ContextJob(id=901, tenant_id=1, owner_id=11, session_id=101,
                kind='chat-compaction', job_key='context-fixture', source_hash='a' * 64, payload={}))
            stamp = datetime.now(UTC)
            session.add(OutboxEvent(id=902, tenant_id=1, event_key='context-fixture', kind='chat-compaction',
                entity_id=901, available_at=stamp, next_attempt_at=stamp, consume_status='running',
                consume_token='fixture-token', consume_until=stamp + timedelta(minutes=5)))
            await session.commit()
        async def checked_source(session, job):
            return await session.get(AIModel, 801)
        dispatched = asyncio.Event()
        @obs.observe_model_stream
        async def fake_stream(model, content, **kwargs):
            # 合成适配器显式发出调用收据；不会建立真实上游连接。
            kwargs['usage_sink'].update(_invoked=True, total_tokens=5, input_tokens=3, output_tokens=2)
            dispatched.set()
            if outcome == 'caller_cancelled':
                await asyncio.Event().wait()
            await asyncio.sleep(0.01)
            if outcome == 'exception':
                raise RuntimeError('fixture_provider_failure')
            if outcome == 'cancelled':
                raise asyncio.CancelledError()
            yield '{"ok":true}'
        monkeypatch.setattr(context_jobs, 'stream_chat_completion', fake_stream)
        monkeypatch.setattr(context_jobs, 'reliable_stream', lambda factory, *args, **kwargs: factory())
        ctx = {'event_id': 902, 'consume_token': 'fixture-token'}
        async def invoke():
            return await context_jobs.run_json_job(
                sessions, 901, 1, ctx, validate_source=checked_source,
                system='fixture', content='fixture',
                schema={'type': 'object', 'properties': {'ok': {'type': 'boolean'}}, 'required': ['ok']})
        if outcome == 'success':
            assert await invoke() == {'ok': True}
            # 此处已恢复不相关的调用方上下文，必须仍指向任务根。
            async with sessions() as session:
                job = await context_jobs.guard_context_job_commit(session, 901, 1, ctx)
                job.status = 'completed'
                record(session, job, 'context_compaction', 'completed', resultCount=1)
                await session.commit()
        elif outcome == 'caller_cancelled':
            task = asyncio.create_task(invoke())
            try:
                await asyncio.wait_for(dispatched.wait(), timeout=5)
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        else:
            with pytest.raises(RuntimeError if outcome == 'exception' else NodeCancelledError):
                await invoke()
        await collector.flush()
        async with sessions() as session:
            job = await session.get(ContextJob, 901)
            run = await session.get(AgentRun, job.run_id)
            assert run.trace_id == obs.turn_trace(1, 'context:901')
            assert run.status == ('completed' if outcome == 'success' else 'uncertain')
            assert run.finished_at > run.started_at
            rows = list(await session.scalars(select(ObservabilityEvent).where(ObservabilityEvent.request_id == 'context:901')))
        spans = [event['body'] for value in rows for event in build_completed_spans(value, environment='test', namespace='fixture')]
        identities = {(span['traceId'], span['spanId']) for span in spans}
        assert len({span['traceId'] for span in spans}) == 1
        assert any(span['name'] == 'eic.model_call' for span in spans)
        assert all(not span.get('parentSpanId') or (span['traceId'], span['parentSpanId']) in identities for span in spans)
        roots = [span for span in spans if span['name'] == 'eic.agent_run'
                 and int(span['endTimeUnixNano']) > int(span['startTimeUnixNano'])]
        assert len(roots) == 1
        assert roots[0]['status']['code'] == (1 if outcome == 'success' else 0)
        if outcome == 'success':
            receipt = next(span for span in spans if span['name'] == 'eic.context_compaction')
            assert receipt['parentSpanId'] == roots[0]['spanId']


@pytest.mark.parametrize('context', [None, 'unrelated'])
@pytest.mark.parametrize('kind,stage', [
    ('chat-compaction', 'context_compaction'),
    ('memory-extraction', 'memory_candidates'),
    ('memory-index', 'memory_index'),
])
def test_job_record_has_explicit_parent_outside_its_context(monkeypatch, context, kind, stage):
    from eic.core import observability as obs
    from eic.core.memory_lifecycle import record
    from eic.models import ContextJob
    rows = []
    monkeypatch.setattr(obs, 'stage', lambda session, value: rows.append(value))
    token = obs.CONTEXT.set(None if context is None else {
        'tenant_id': 1, 'request_id': 'unrelated', 'trace_id': 'unrelated', 'span_id': 'f' * 32})
    try:
        record(None, ContextJob(id=42, tenant_id=1, owner_id=11, session_id=101, kind=kind),
               stage, 'completed')
    finally:
        obs.CONTEXT.reset(token)
    request = ('memory-index' if kind == 'memory-index' else 'context') + ':42'
    assert rows[0]['parent_span_id'] == obs.turn_span(1, request)
    assert rows[0]['trace_id'] == obs.turn_trace(1, request)
    assert rows[0]['span_id'] != rows[0]['parent_span_id']


@pytest.mark.parametrize('trace_mode', ['current', 'legacy'])
@pytest.mark.parametrize('state', ['provider_done', 'provider_started', 'completed'])
@pytest.mark.parametrize('bad_binding', [
    None, 'session', 'request', 'trace', 'graph', 'metadata', 'binding', 'model', 'status', 'missing',
])
async def test_context_job_recovery_converges_bound_run_without_dispatch(monkeypatch, tmp_path, state, bad_binding, trace_mode):
    import json
    import uuid
    from datetime import timedelta
    from sqlalchemy import select
    from eic.ai.agent_graph import AGENT_GRAPH_VERSION
    from eic.core import context_jobs, observability as obs
    from eic.core.security import _fernet
    from eic.models import AIModel, AgentRun, ContextJob, ObservabilityEvent, OutboxEvent
    from test_fv2_observability_collection import collecting

    # 即使宿主环境启用了导出，这些离线用例也不能启动 exporter。
    async def no_export(*args, **kwargs):
        return None
    monkeypatch.setattr(obs.langfuse_exporter, 'start', no_export)
    def no_dispatch(*args, **kwargs):
        pytest.fail('recovery must not dispatch a provider or graph')
    monkeypatch.setattr(context_jobs, 'stream_chat_completion', no_dispatch)
    monkeypatch.setattr(context_jobs, 'execute_shared', no_dispatch)
    async with collecting(monkeypatch, tmp_path) as (sessions, collector):
        async def checked_source(session, job):
            return await session.get(AIModel, 801)
        schema = {'type': 'object', 'properties': {'ok': {'type': 'boolean'}}, 'required': ['ok']}
        ctx = {'event_id': 902, 'consume_token': 'fixture-token'}
        stamp = datetime.now(UTC)
        async with sessions() as session:
            model = AIModel(id=801, tenant_id=1, model_type='chat', provider='openai_compatible',
                model_name='fixture', endpoint='http://fixture.invalid/v1', enabled=True,
                max_input_tokens=4096, max_output_tokens=256)
            job = ContextJob(id=901, tenant_id=1, owner_id=11, session_id=101,
                kind='chat-compaction', job_key='recovery-fixture', source_hash='a' * 64,
                payload={}, status=state, run_id='context:recovery', lease_token='expired',
                lease_until=stamp - timedelta(minutes=1))
            session.add_all([model, job, OutboxEvent(id=902, tenant_id=1,
                event_key='recovery-fixture', kind='chat-compaction', entity_id=901,
                available_at=stamp, next_attempt_at=stamp, consume_status='running',
                consume_token='fixture-token', consume_until=stamp + timedelta(minutes=5))])
            await session.commit()
        async with sessions() as session:
            runtime = context_jobs._Runtime(sessions, 901, 1, ctx, checked_source,
                                            context_jobs._hash(['fixture', 'fixture', schema]))
            job, _, _ = await runtime.checked(session, owned=False)
            metadata = {'contextJobId': 901, 'binding': runtime.identity, 'model': runtime.model_hash}
            if state in {'provider_done', 'completed'}:
                job.result_encrypted = _fernet().encrypt(json.dumps({
                    'binding': [runtime.identity, runtime.model_hash], 'result': {'ok': True}}
                ).encode()).decode('ascii')
            original_trace = (obs.turn_trace(1, 'context:901') if trace_mode == 'current' else
                              uuid.uuid5(uuid.NAMESPACE_URL, job.run_id).hex)
            original_start = stamp - timedelta(minutes=2)
            values = dict(run_id=job.run_id, tenant_id=1, session_id=101,
                request_id='context:901', trace_id=original_trace,
                graph_version=AGENT_GRAPH_VERSION, status='running', run_metadata=metadata,
                started_at=original_start)
            if bad_binding:
                field, value = {
                    'missing': ('run_id', 'context:unrelated'), 'session': ('session_id', None),
                    'request': ('request_id', 'context:other'), 'trace': ('trace_id', 'other'),
                    'graph': ('graph_version', 'other'), 'metadata': ('run_metadata', {}),
                    'binding': ('run_metadata', {**metadata, 'binding': 'wrong'}),
                    'model': ('run_metadata', {**metadata, 'model': 'wrong'}),
                    'status': ('status', 'failed')}[bad_binding]
                values[field] = value
            session.add(AgentRun(**values))
            await session.commit()
        before = datetime.now(UTC)
        async def invoke():
            return await context_jobs.run_json_job(sessions, 901, 1, ctx,
                system='fixture', content='fixture', schema=schema, validate_source=checked_source)
        if bad_binding:
            with pytest.raises(context_jobs.ContextJobLost, match='context_job_recovery_binding_invalid'):
                await invoke()
        elif state == 'provider_started':
            with pytest.raises(context_jobs.ContextJobLost, match='context_job_provider_outcome_unknown'):
                await invoke()
        else:
            assert await invoke() == {'ok': True}
        await collector.flush()
        async with sessions() as session:
            job = await session.get(ContextJob, 901)
            run = await session.get(AgentRun, 'context:unrelated' if bad_binding == 'missing' else 'context:recovery')
            expected_metadata = ({} if bad_binding == 'metadata' else
                {**metadata, bad_binding: 'wrong'} if bad_binding in {'binding', 'model'} else metadata)
            assert run.run_metadata == expected_metadata
            if bad_binding:
                assert job.status == state and run.status == ('failed' if bad_binding == 'status' else 'running')
                assert run.finished_at is None
                assert '_context_job_receipt' not in ctx
                return
            expected = 'uncertain' if state == 'provider_started' else 'completed'
            assert run.status == expected
            assert context_jobs._utc(run.finished_at) >= before
            assert run.error_code == f'context_job_recovered_{expected}'
            assert run.trace_id == original_trace
            assert context_jobs._utc(run.started_at) == original_start
            assert job.usage['_observation']['trace_id'] == original_trace
            if state == 'provider_done':
                bound_job = await context_jobs.guard_context_job_commit(session, 901, 1, ctx)
                assert bound_job.run_id == run.run_id
            rows = list(await session.scalars(select(ObservabilityEvent).where(
                ObservabilityEvent.run_id == run.run_id, ObservabilityEvent.status == expected)))
        value = next(value for value in rows if value.category == 'agent_run')
        legacy = build_ingestion_events(value, environment='test', namespace='fixture')[1]['body']
        assert legacy['startTime'] == original_start.isoformat(timespec='microseconds').replace('+00:00', 'Z')
        assert legacy['startTime'] != legacy['endTime']
        assert legacy['metadata']['completion_time_source'] == 'recovery_convergence'
        assert legacy['metadata']['recovery_time'] == legacy['endTime']
        assert legacy['metadata']['duration_ms'] >= 120000
        assert legacy['metadata']['includes_recovery_wait'] is True
        assert legacy['metadata']['original_execution_end_unknown'] is True
        if trace_mode == 'legacy':
            assert legacy['metadata']['legacy_trace_preserved'] is True
            assert legacy['metadata']['historical_parent_graph_verified'] is False
        span = build_completed_spans(value, environment='test', namespace='fixture')[0]['body']
        assert span['status']['code'] == (1 if expected == 'completed' else 0)
        assert not span.get('parentSpanId')
        from eic.core.langfuse_context import turn_context
        token = obs.CONTEXT.set(turn_context(tenant_id=1, request_id='context:901', trace_id=original_trace))
        try:
            provider = obs.observation('model_call', 'model', 'success',
                source='provider_stream', span_id='c' * 32, occurred_at=stamp, duration_ms=1)
        finally:
            obs.CONTEXT.reset(token)
        child = build_completed_spans(provider, environment='test', namespace='fixture')[0]['body']
        if trace_mode == 'current':
            assert (child['traceId'], child['parentSpanId']) == (span['traceId'], span['spanId'])
        else:
            # 核对 approved 旧根身份；不以合成现代 provider 节点证明历史父图。
            old_shape = {column.name: getattr(value, column.name) for column in ObservabilityEvent.__table__.columns}
            old_shape['span_id'] = None
            old_span = build_completed_spans(old_shape, environment='test', namespace='fixture')[0]['body']
            assert old_span['spanId'] == span['spanId']
        # 重派发不能改写第一次收敛时间或重复调用模型。
        finished = run.finished_at
        if state != 'provider_started':
            async with sessions() as session:
                job = await session.get(ContextJob, 901)
                job.lease_until = stamp - timedelta(minutes=1)
                await session.commit()
            assert await invoke() == {'ok': True}
        else:
            with pytest.raises(context_jobs.ContextJobLost, match='context_job_terminal_no_replay'):
                await invoke()
        async with sessions() as session:
            run = await session.get(AgentRun, 'context:recovery')
            assert run.finished_at == finished and run.run_metadata == metadata
            assert context_jobs._utc(run.started_at) == original_start and run.trace_id == original_trace
            if state != 'provider_started':
                from eic.core.memory_lifecycle import record
                job = await session.get(ContextJob, 901)
                record(session, job, 'context_compaction', 'completed')
                await session.commit()
        if state != 'provider_started':
            await collector.flush()
            async with sessions() as session:
                receipt = await session.scalar(select(ObservabilityEvent).where(
                    ObservabilityEvent.source == 'memory_ops', ObservabilityEvent.stage == 'context_compaction'))
            assert receipt.trace_id == original_trace
            receipt_span = build_completed_spans(receipt, environment='test', namespace='fixture')[0]['body']
            assert (receipt_span['traceId'], receipt_span['parentSpanId']) == (span['traceId'], span['spanId'])


async def test_context_job_recovery_rejects_cross_tenant_run():
    from unittest.mock import AsyncMock
    from eic.ai.agent_graph import AGENT_GRAPH_VERSION
    from eic.core import context_jobs, observability as obs
    job = SimpleNamespace(id=42, tenant_id=1, session_id=101, run_id='context:fixture')
    runtime = SimpleNamespace(identity='binding', model_hash='model')
    run = SimpleNamespace(tenant_id=2, session_id=101, request_id='context:42',
        trace_id=obs.turn_trace(1, 'context:42'), graph_version=AGENT_GRAPH_VERSION,
        run_metadata={'contextJobId': 42, 'binding': 'binding', 'model': 'model'},
        status='running', finished_at=None)
    session = SimpleNamespace(get=AsyncMock(return_value=run))
    with pytest.raises(context_jobs.ContextJobLost, match='context_job_recovery_binding_invalid'):
        await context_jobs._recover_run(session, runtime, job, 'completed')
    assert run.status == 'running' and run.finished_at is None


def test_uncertain_without_measured_duration_remains_an_instant_event():
    value = row()
    value.update(source='agent_runs', stage='model_decision', status='uncertain', duration_ms=None)
    body = build_ingestion_events(value, environment='test', namespace='fixture')[1]['body']
    assert 'endTime' not in body
    assert 'duration_ms' not in body['metadata']
    span = build_completed_spans(value, environment='test', namespace='fixture')[0]['body']
    assert span['startTimeUnixNano'] == span['endTimeUnixNano']
    assert span['status']['code'] == 0


@asynccontextmanager
async def _memory_index_observation_fixture(monkeypatch, tmp_path, *, state='pending', known_start=False):
    import hashlib
    from datetime import timedelta
    from eic import main
    from eic.core import memory_index, observability as obs
    from eic.core.security import _fernet
    from eic.models import AIModel, AgentMemory, ContextJob, OutboxEvent
    from memory_fixture import enable_memory
    from test_fv2_observability_collection import collecting

    async def no_export(*args, **kwargs):
        return None
    monkeypatch.setattr(obs.langfuse_exporter, 'start', no_export)
    async with collecting(monkeypatch, tmp_path) as (sessions, collector):
        async with sessions() as session:
            await enable_memory(session)
            model = AIModel(id=801, tenant_id=1, model_type='embedding', enabled=True,
                provider='openai_compatible', model_name='fixture-embedding',
                endpoint='http://fixture.invalid/v1', embedding_dimensions=3)
            memory = AgentMemory(id=901, tenant_id=1, owner_id=11, agent_id=7,
                session_id=101, content='fixture memory', status='active', version=1)
            session.add_all([model, memory])
            await session.commit()
            config = await main.runtime_app_config(session, 1, 7)
            stamp = datetime.now(UTC)
            session.add(ContextJob(id=902, tenant_id=1, owner_id=11, session_id=101,
                kind='memory-index', job_key='memory-index:fixture', status=state,
                usage={'input_tokens': 17, **({'_observation': {
                    'started_at': (stamp - timedelta(minutes=2)).isoformat()}} if known_start else {})},
                source_hash=hashlib.sha256(memory.content.encode('utf-8')).hexdigest(),
                payload={'memoryId': 901, 'version': 1, 'modelId': 801,
                         'fingerprint': memory_index.fingerprint(model),
                         'runtimePolicy': config.get('runtimePolicy')},
                result_encrypted=(_fernet().encrypt(b'[1.0,0.0,0.0]').decode('ascii')
                                  if state == 'provider_done' else None)))
            session.add(OutboxEvent(id=903, tenant_id=1, event_key='memory-index:fixture',
                kind='memory-index', entity_id=902, available_at=stamp, next_attempt_at=stamp,
                consume_status='running', consume_token='fixture-index-token',
                consume_until=stamp + timedelta(minutes=5)))
            await session.commit()
        yield sessions, collector, {'event_id': 903, 'consume_token': 'fixture-index-token'}


@pytest.mark.parametrize('outcome', ['success', 'exception', 'cancelled', 'caller_cancelled'])
async def test_process_index_actual_lifecycle_has_closed_parent_graph(monkeypatch, tmp_path, outcome):
    import asyncio
    from sqlalchemy import func, select
    from eic.ai.providers import ModelProviderError
    from eic.ai.runtime_policy import ACTIVE
    from eic.core import memory_index, observability as obs
    from eic.models import AgentMemory, ContextJob, MemoryVector, ObservabilityEvent

    calls = []
    dispatched = asyncio.Event()
    @obs.observe_model_call('embedding')
    async def fake_provider(model, texts, **kwargs):
        calls.append((model.id, list(texts)))
        kwargs['usage_sink'].update(_invoked=True, total_tokens=3, input_tokens=3,
                                    usage_source='provider_usage')
        dispatched.set()
        if outcome == 'caller_cancelled':
            await asyncio.Event().wait()
        await asyncio.sleep(0.01)
        if outcome == 'exception':
            raise ModelProviderError('fixture embedding failure', reason='provider_error', retryable=False)
        if outcome == 'cancelled':
            raise asyncio.CancelledError()
        return [[1.0, 0.0, 0.0]]
    # 仅替换 provider；process_index、_checked、SQL 提交和观测装饰器均真实执行。
    monkeypatch.setattr(memory_index, 'embed_texts', fake_provider)
    async with _memory_index_observation_fixture(monkeypatch, tmp_path) as (sessions, collector, ctx):
        async def invoke():
            prior_scope, prior_account = obs.CONTEXT.get(), ACTIVE.get()
            try:
                return await memory_index.process_index(ctx, 902, 1, sessions=sessions)
            finally:
                assert obs.CONTEXT.get() == prior_scope
                assert ACTIVE.get() is prior_account
        if outcome == 'success':
            assert await invoke() == {'status': 'completed', 'memoryId': '901'}
        elif outcome == 'caller_cancelled':
            task = asyncio.create_task(invoke())
            try:
                await asyncio.wait_for(dispatched.wait(), timeout=5)
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        else:
            with pytest.raises(ModelProviderError if outcome == 'exception' else asyncio.CancelledError):
                await invoke()
        await collector.flush()
        async with sessions() as session:
            job = await session.get(ContextJob, 902)
            memory = await session.get(AgentMemory, 901)
            assert job.status == ('completed' if outcome == 'success' else 'uncertain')
            assert memory.index_status == ('ready' if outcome == 'success' else 'uncertain')
            assert await session.scalar(select(func.count()).select_from(MemoryVector)) == (1 if outcome == 'success' else 0)
            values = list(await session.scalars(select(ObservabilityEvent).where(
                ObservabilityEvent.request_id == 'memory-index:902')))
        roots = [value for value in values if value.source == 'memory_pipeline']
        providers = [value for value in values if value.source == 'provider_response']
        receipts = [value for value in values if value.source == 'memory_ops']
        assert len(roots) == len(providers) == 1
        root = roots[0]
        assert root.status == ('completed' if outcome == 'success' else 'uncertain')
        assert root.duration_ms > 0 and not root.parent_span_id
        assert providers[0].status == ('success' if outcome == 'success' else 'error' if outcome == 'exception' else 'cancelled')
        assert providers[0].parent_span_id == root.span_id
        assert len(receipts) == (1 if outcome == 'success' else 0)
        if receipts:
            assert receipts[0].parent_span_id == root.span_id and receipts[0].span_id != root.span_id
        spans = [event['body'] for value in values for event in build_completed_spans(
            value, environment='test', namespace='fixture')]
        identities = {(span['traceId'], span['spanId']) for span in spans}
        assert len({span['traceId'] for span in spans}) == 1
        assert all(not span.get('parentSpanId') or
                   (span['traceId'], span['parentSpanId']) in identities for span in spans)
        root_span = build_completed_spans(root, environment='test', namespace='fixture')[0]['body']
        assert int(root_span['endTimeUnixNano']) > int(root_span['startTimeUnixNano'])
        assert root_span['status']['code'] == (1 if outcome == 'success' else 0)
        # 同一持久终态重投不再外呼、不重复发根或收据。
        expected = ({'status': 'completed'} if outcome == 'success' else
                    {'status': 'uncertain', 'automaticRetry': False})
        assert await invoke() == expected
        await collector.flush()
        async with sessions() as session:
            after = list(await session.scalars(select(ObservabilityEvent).where(
                ObservabilityEvent.request_id == 'memory-index:902')))
        assert {value.event_key for value in after} == {value.event_key for value in values}
        assert len(calls) == 1


@pytest.mark.parametrize('known_start', [True, False])
async def test_process_index_provider_done_recovery_records_under_actual_root(monkeypatch, tmp_path, known_start):
    from sqlalchemy import select
    from eic.core import memory_index
    from eic.models import ContextJob, MemoryVector, ObservabilityEvent
    def no_dispatch(*args, **kwargs):
        pytest.fail('provider_done recovery must not call embedding')
    monkeypatch.setattr(memory_index, 'embed_texts', no_dispatch)
    async with _memory_index_observation_fixture(monkeypatch, tmp_path, state='provider_done', known_start=known_start) as (sessions, collector, ctx):
        assert await memory_index.process_index(ctx, 902, 1, sessions=sessions) == {
            'status': 'completed', 'memoryId': '901'}
        await collector.flush()
        async with sessions() as session:
            assert (await session.get(ContextJob, 902)).status == 'completed'
            vector = await session.scalar(select(MemoryVector))
            assert vector.vector == [1.0, 0.0, 0.0]
            values = list(await session.scalars(select(ObservabilityEvent).where(
                ObservabilityEvent.request_id == 'memory-index:902')))
        root = next(value for value in values if value.source == 'memory_pipeline')
        assert root.status == 'completed'
        assert not any(value.source == 'provider_response' for value in values)
        parent = build_completed_spans(root, environment='test', namespace='fixture')[0]['body']
        legacy = build_ingestion_events(root, environment='test', namespace='fixture')[1]['body']
        if not known_start:
            assert root.stage == 'memory_index_recovery' and root.duration_ms is None
            assert not any(value.source == 'memory_ops' for value in values)
            assert legacy['metadata']['start_time_missing'] is True
            assert legacy['metadata']['parent_graph_complete'] is False
            assert parent['startTimeUnixNano'] == parent['endTimeUnixNano']
            return
        receipt = next(value for value in values if value.source == 'memory_ops')
        assert root.duration_ms >= 120000
        assert receipt.parent_span_id == root.span_id
        assert legacy['metadata']['includes_recovery_wait'] is True
        child = build_completed_spans(receipt, environment='test', namespace='fixture')[0]['body']
        assert (child['traceId'], child['parentSpanId']) == (parent['traceId'], parent['spanId'])


@pytest.mark.parametrize('known_start', [True, False])
async def test_process_index_provider_started_recovery_is_unknown_and_never_dispatches(monkeypatch, tmp_path, known_start):
    from sqlalchemy import select
    from eic.core import memory_index, observability as obs
    from eic.models import AgentMemory, ContextJob, ObservabilityEvent
    def no_dispatch(*args, **kwargs):
        pytest.fail('unknown provider outcome must not replay embedding')
    monkeypatch.setattr(memory_index, 'embed_texts', no_dispatch)
    async with _memory_index_observation_fixture(monkeypatch, tmp_path, state='provider_started', known_start=known_start) as (sessions, collector, ctx):
        expected = {'status': 'uncertain', 'automaticRetry': False}
        assert await memory_index.process_index(ctx, 902, 1, sessions=sessions) == expected
        # 不等 collector.flush，终态根或缺测收据已和业务同时落盘。
        async with sessions() as session:
            job = await session.get(ContextJob, 902)
            assert job.status == (await session.get(AgentMemory, 901)).index_status == 'uncertain'
            saved_usage = dict(job.usage)
            values = list(await session.scalars(select(ObservabilityEvent).where(
                ObservabilityEvent.source == 'memory_pipeline')))
        assert len(values) == 1
        root = values[0]
        legacy = build_ingestion_events(root, environment='test', namespace='fixture')[1]['body']
        span = build_completed_spans(root, environment='test', namespace='fixture')[0]['body']
        assert span['status']['code'] == 0
        assert root.status == 'uncertain' and saved_usage['input_tokens'] == 17
        if known_start:
            assert root.span_id == obs.turn_span(1, 'memory-index:902')
            assert root.duration_ms >= 120000
            assert legacy['metadata']['includes_recovery_wait'] is True
        else:
            assert root.span_id != obs.turn_span(1, 'memory-index:902')
            assert root.duration_ms is None and root.stage == 'memory_index_recovery'
            assert legacy['metadata']['start_time_missing'] is True
            assert legacy['metadata']['parent_graph_complete'] is False
        assert await memory_index.process_index(ctx, 902, 1, sessions=sessions) == expected
        await collector.flush()
        async with sessions() as session:
            assert (await session.get(ContextJob, 902)).usage == saved_usage
            assert len(list(await session.scalars(select(ObservabilityEvent).where(
                ObservabilityEvent.source == 'memory_pipeline')))) == 1


@pytest.mark.parametrize('interruption', ['cancel_after_provider_done', 'terminal_commit_failure'])
async def test_process_index_interrupted_local_commit_recovers_original_task_lifecycle(monkeypatch, tmp_path, interruption):
    import asyncio
    from sqlalchemy import func, select
    from eic.core import memory_index, observability as obs
    from eic.models import ContextJob, MemoryVector, ObservabilityEvent
    calls = []
    @obs.observe_model_call('embedding')
    async def fake_provider(model, texts, **kwargs):
        calls.append(model.id)
        kwargs['usage_sink'].update(_invoked=True, input_tokens=3, total_tokens=3)
        # 外呼前，真实 start 已经和 provider_started 一起提交。
        async with sessions() as session:
            job = await session.get(ContextJob, 902)
            assert job.status == 'provider_started'
            assert memory_index._observation_start(job) is not None
        return [[1.0, 0.0, 0.0]]
    monkeypatch.setattr(memory_index, 'embed_texts', fake_provider)
    async with _memory_index_observation_fixture(monkeypatch, tmp_path) as (sessions, collector, ctx):
        fired = False
        @asynccontextmanager
        async def interrupted_sessions():
            async with sessions() as session:
                original_commit = session.commit
                async def commit():
                    nonlocal fired
                    job = await session.get(ContextJob, 902)
                    if not fired and job is not None:
                        if interruption == 'cancel_after_provider_done' and job.status == 'provider_done':
                            await original_commit()
                            fired = True
                            raise asyncio.CancelledError()
                        if interruption == 'terminal_commit_failure' and job.status == 'completed':
                            fired = True
                            raise RuntimeError('fixture_terminal_commit_failure')
                    await original_commit()
                session.commit = commit
                yield session
        with pytest.raises(asyncio.CancelledError if interruption == 'cancel_after_provider_done' else RuntimeError):
            await memory_index.process_index(ctx, 902, 1, sessions=interrupted_sessions)
        assert fired
        await collector.flush()
        async with sessions() as session:
            job = await session.get(ContextJob, 902)
            assert job.status == 'provider_done' and job.result_encrypted
            start = job.usage['_observation']['started_at']
            payload = dict(job.payload)
            assert 'finished_at' not in job.usage['_observation']
            assert await session.scalar(select(func.count()).select_from(MemoryVector)) == 0
            assert await session.scalar(select(func.count()).select_from(ObservabilityEvent).where(
                ObservabilityEvent.source == 'memory_pipeline')) == 0
        await asyncio.sleep(0.01)
        assert await memory_index.process_index(ctx, 902, 1, sessions=sessions) == {
            'status': 'completed', 'memoryId': '901'}
        async with sessions() as session:
            job = await session.get(ContextJob, 902)
            assert job.usage['_observation']['started_at'] == start and job.payload == payload
            assert job.usage['input_tokens'] == 17
            # 根不依赖 collector 消费即可读取。
            roots = list(await session.scalars(select(ObservabilityEvent).where(
                ObservabilityEvent.source == 'memory_pipeline')))
            assert len(roots) == 1
        root = roots[0]
        legacy = build_ingestion_events(root, environment='test', namespace='fixture')[1]['body']
        assert legacy['startTime'] == start.replace('+00:00', 'Z')
        assert legacy['metadata']['includes_recovery_wait'] is True
        assert root.status == 'completed' and root.duration_ms >= 10
        await collector.flush()
        async with sessions() as session:
            values = list(await session.scalars(select(ObservabilityEvent).where(
                ObservabilityEvent.request_id == 'memory-index:902')))
        spans = [event['body'] for value in values for event in build_completed_spans(
            value, environment='test', namespace='fixture')]
        identities = {(span['traceId'], span['spanId']) for span in spans}
        assert all(not span.get('parentSpanId') or
            (span['traceId'], span['parentSpanId']) in identities for span in spans)
        assert len(calls) == 1


async def test_process_index_root_is_durable_with_collector_closed_and_reexport_is_idempotent(monkeypatch, tmp_path):
    from sqlalchemy import select
    from eic.core import langfuse_delivery, memory_index, observability as obs
    from eic.models import ContextJob, LangfuseDelivery, ObservabilityEvent
    async def fake_provider(*args, **kwargs):
        return [[1.0, 0.0, 0.0]]
    monkeypatch.setattr(memory_index, 'embed_texts', fake_provider)
    exported = []
    async def delivered(value):
        exported.append(value)
        return 'acknowledged'
    monkeypatch.setattr(obs.langfuse_exporter, 'deliver_persisted', delivered)
    async with _memory_index_observation_fixture(monkeypatch, tmp_path) as (sessions, collector, ctx):
        monkeypatch.setattr(collector, 'running', False)
        await memory_index.process_index(ctx, 902, 1, sessions=sessions)
        async with sessions() as session:
            assert (await session.get(ContextJob, 902)).status == 'completed'
            root = await session.scalar(select(ObservabilityEvent).where(ObservabilityEvent.source == 'memory_pipeline'))
            assert root is not None
            original = {column.name: getattr(root, column.name) for column in ObservabilityEvent.__table__.columns}
        assert not exported
        target = 'a' * 64
        # 只在 collector 已启动后启用合成目标，手动消费持久账本，不启动真实出口。
        monkeypatch.setattr(langfuse_delivery, 'destination', lambda _: target)
        monkeypatch.setattr(obs.langfuse_exporter, '_build', build_completed_spans)
        monkeypatch.setattr(obs.langfuse_exporter, '_environment', 'test', raising=False)
        monkeypatch.setattr(obs.langfuse_exporter, '_namespace', 'fixture', raising=False)
        monkeypatch.setattr(obs.langfuse_exporter, '_accepting', True)
        monkeypatch.setattr(collector, 'running', True)
        await memory_index.process_index(ctx, 902, 1, sessions=sessions)
        await collector.flush()
        for _ in range(20):
            if not await langfuse_delivery.dispatch_one(sessions, obs.langfuse_exporter):
                break
        else:
            pytest.fail('合成投递队列未能有界排空')
        async with sessions() as session:
            values = list(await session.scalars(select(ObservabilityEvent).where(ObservabilityEvent.source == 'memory_pipeline')))
            assert len(values) == 1
            assert {column.name: getattr(values[0], column.name) for column in ObservabilityEvent.__table__.columns} == original
            ledger = await session.get(LangfuseDelivery, (root.event_key, target))
            assert ledger.status == 'acknowledged' and ledger.attempts == 1
        assert sum(value['event_key'] == root.event_key for value in exported) == 1
        await memory_index.process_index(ctx, 902, 1, sessions=sessions)
        await collector.flush()
        assert not await langfuse_delivery.dispatch_one(sessions, obs.langfuse_exporter)
        assert sum(value['event_key'] == root.event_key for value in exported) == 1


async def test_context_job_completed_legacy_run_preserves_original_end_and_usage():
    import uuid
    from datetime import timedelta
    from unittest.mock import AsyncMock
    from eic.ai.agent_graph import AGENT_GRAPH_VERSION
    from eic.core import context_jobs
    from eic.models import AgentRun, ContextJob
    stamp = datetime.now(UTC)
    run_id = 'context:legacy-completed'
    trace = uuid.uuid5(uuid.NAMESPACE_URL, run_id).hex
    metadata = {'contextJobId': 42, 'binding': 'binding', 'model': 'model'}
    run = AgentRun(run_id=run_id, tenant_id=1, session_id=101, request_id='context:42',
        trace_id=trace, graph_version=AGENT_GRAPH_VERSION, run_metadata=metadata,
        status='completed', started_at=stamp - timedelta(seconds=5), finished_at=stamp, error_code='')
    job = ContextJob(id=42, tenant_id=1, session_id=101, run_id=run_id,
                     usage={'input_tokens': 3, 'total_tokens': 5})
    session = SimpleNamespace(get=AsyncMock(return_value=run))
    await context_jobs._recover_run(session, SimpleNamespace(identity='binding', model_hash='model'), job, 'completed')
    assert run.trace_id == trace and run.finished_at == stamp
    assert run.started_at == stamp - timedelta(seconds=5) and run.error_code == ''
    assert run.run_metadata == metadata
    assert job.usage == {'input_tokens': 3, 'total_tokens': 5, '_observation': {'trace_id': trace}}
