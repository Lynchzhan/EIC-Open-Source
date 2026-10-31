"""C2 隔离调用链与拒绝边界；所有模型、工具及 HTTP 仅为测试桩。"""
import asyncio
from pathlib import Path

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy import select

from eic.ai import shared_executor as shared
from eic.ai.agent_graph import AGENT_GRAPH_VERSION, AgentBudget, ModelDecision, ToolCallRequest
from eic.ai.providers import ModelProviderError
from eic.ai.tool_center import _digest

PROVIDER_MOCK_REQUESTS = []
MCP_MOCK_REQUESTS = []


def services_for(decisions, *, calls=None, approval=False, unknown=False, guard=None):
    decisions = iter(decisions)
    calls = [] if calls is None else calls
    async def model(state):
        PROVIDER_MOCK_REQUESTS.append({'run': state['agent_run_id'], 'step': state['agent_step_count']})
        value = next(decisions)
        if isinstance(value, Exception):
            raise value
        return value
    async def validate(request):
        if request.name != 'fixture':
            return {'errorCode': 'tool_not_allowed'}
        return {'requiresApproval': approval}
    async def tool(request, remaining_ms):
        calls.append(request.call_id)
        return {'callId': request.call_id, 'toolName': request.name, 'status': 'failed' if unknown else 'completed',
                'result': {'ok': True}, 'effectStatus': 'unknown' if unknown else 'applied',
                **({'errorCode': 'tool_timeout'} if unknown else {})}
    return shared.ExecutionServices(model=model, execute_tool=tool, validate_tool=validate, guard=guard)


def decision(call_id='one', name='fixture', arguments=None):
    return ModelDecision('tool_calls', tool_calls=(ToolCallRequest(call_id, name, arguments or {}),))


@pytest.mark.asyncio
async def test_graph_owns_model_tool_loop_and_same_entrypoint():
    calls = []
    services = services_for([decision(), ModelDecision('answer', 'done')], calls=calls)
    graph = shared.build_execution_graph(InMemorySaver())
    result = await shared.execute_shared('question', run_id='loop', services=services, graph=graph)
    assert result['answer'] == 'done' and calls == ['one']
    assert result['agent_step_count'] == 2 and result['tool_call_count'] == 1
    assert result['graph_version'] == AGENT_GRAPH_VERSION
    nodes = {key for key in graph.get_graph().nodes if not key.startswith('__')}
    assert nodes == {'check_execution', 'model_decision', 'validate_tool', 'approval', 'execute_tool', 'record_tool_result'}
    assert not {'tenant_id', 'principal', 'session', 'credentials', 'allowed_tools'} & set(result)


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['denied', 'unknown', 'duplicate', 'steps', 'tools'])
async def test_rejection_and_uncertain_boundaries(kind):
    calls = []
    decisions = [decision(name='forbidden'), ModelDecision('answer', 'refused')] if kind == 'denied' else [
        decision(), decision(arguments={'changed': True}), ModelDecision('answer', 'unused')]
    services = services_for(decisions, calls=calls, unknown=kind == 'unknown')
    if kind == 'steps':
        services.budget = AgentBudget(max_steps=1)
    if kind == 'tools':
        services.budget = AgentBudget(max_tool_calls=1)
    result = await shared.execute_shared('question', run_id=kind, services=services)
    if kind == 'denied':
        assert calls == [] and result['tool_results'][0]['errorCode'] == 'tool_not_allowed'
    else:
        assert calls == ['one']
        assert result['stop_reason'] == {'unknown': 'tool_timeout', 'duplicate': 'duplicate_tool_call_id',
            'steps': 'max_steps_exceeded', 'tools': 'max_tool_calls_exceeded'}[kind]


@pytest.mark.asyncio
async def test_interrupt_survives_new_graph_and_requires_service_proof():
    saver, calls = InMemorySaver(), []
    first = services_for([decision()], calls=calls, approval=True)
    graph = shared.build_execution_graph(saver)
    result = await shared.execute_shared('question', run_id='approval', services=first, graph=graph)
    assert result['stop_reason'] == 'approval_required' and calls == []
    config = {'configurable': {'thread_id': 'approval'}}
    assert any(task.interrupts for task in (await graph.aget_state(config)).tasks)
    resumed = services_for([ModelDecision('answer', 'approved')], calls=calls, approval=True)
    with pytest.raises(ValueError, match='approval_resume_service_required'):
        await shared.execute_shared('question', run_id='approval', services=resumed,
            graph=shared.build_execution_graph(saver), resume=True)
    async def proof(request, value):
        assert value == {'receipt': 'eic-consumed'}
        return {'callId': request.call_id, 'toolName': request.name, 'status': 'completed',
                'inputDigest': _digest(request.arguments), 'effectStatus': 'applied', 'result': {'ok': True}}
    resumed.validate_resume = proof
    final = await shared.execute_shared('question', run_id='approval', services=resumed,
        graph=shared.build_execution_graph(saver), resume={'receipt': 'eic-consumed'})
    assert final['answer'] == 'approved' and calls == []
    assert final['tool_call_count'] == 1
    with pytest.raises(ValueError, match='approval_interrupt_missing'):
        await shared.execute_shared('question', run_id='approval', services=resumed,
            graph=shared.build_execution_graph(saver), resume=True)


@pytest.mark.asyncio
async def test_forged_resume_result_is_rejected():
    saver = InMemorySaver()
    service = services_for([decision()], approval=True)
    await shared.execute_shared('q', run_id='forged', services=service, graph=shared.build_execution_graph(saver))
    async def forged(request, value):
        return {'callId': request.call_id, 'toolName': request.name, 'inputDigest': 'wrong',
                'status': 'completed', 'effectStatus': 'applied'}
    service.validate_resume = forged
    with pytest.raises(ValueError, match='approval_resume_result_mismatch'):
        await shared.execute_shared('q', run_id='forged', services=service,
            graph=shared.build_execution_graph(saver), resume=True)


@pytest.mark.asyncio
@pytest.mark.parametrize('reason', ['provider_outcome_uncertain', 'provider_parameters_rejected', 'provider_rate_limited'])
async def test_a1_error_reason_and_retryability_are_preserved(reason):
    error = ModelProviderError('fixture', reason=reason, retryable=reason == 'provider_rate_limited')
    service = services_for([error])
    with pytest.raises(ModelProviderError) as raised:
        await shared.execute_shared('q', run_id=reason, services=service)
    assert raised.value is error


@pytest.mark.asyncio
async def test_cancel_stops_before_tool_and_closes_event_bridge():
    closed = asyncio.Event()
    async def model(state):
        try:
            await service.emit('first')
            await asyncio.Event().wait()
        finally:
            closed.set()
    service = services_for([])
    service.model = model
    stream = shared.stream_shared('q', run_id='close', services=service)
    assert await anext(stream) == 'first'
    await stream.aclose()
    assert closed.is_set()
    async def revoked():
        raise PermissionError('lease_revoked')
    service = services_for([decision()], guard=revoked)
    with pytest.raises(PermissionError, match='lease_revoked'):
        await shared.execute_shared('q', run_id='revoked', services=service)


@pytest.mark.asyncio
async def test_event_bridge_waits_for_cleanup_through_repeated_native_cancellation():
    started, closing, release, closed = [asyncio.Event() for _ in range(4)]
    async def model(state):
        try:
            started.set()
            await asyncio.Event().wait()
        finally:
            closing.set()
            await release.wait()
            closed.set()
    service = services_for([])
    service.model = model
    async def consume():
        async for _ in shared.stream_shared('q', run_id='repeated-cancel', services=service):
            pass
    consumer = asyncio.create_task(consume())
    try:
        await asyncio.wait_for(started.wait(), 5)
        consumer.cancel()
        await asyncio.wait_for(closing.wait(), 5)
        consumer.cancel()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert not consumer.done() and not closed.is_set()
    finally:
        release.set()
        outcome = await asyncio.gather(consumer, return_exceptions=True)
    assert isinstance(outcome[0], asyncio.CancelledError)
    assert closed.is_set() and service.event_sink is None


@pytest.mark.asyncio
async def test_checkpoint_binding_and_finished_replay():
    saver = InMemorySaver()
    first = services_for([ModelDecision('answer', 'saved')])
    await shared.execute_shared('q', run_id='binding', services=first, graph=shared.build_execution_graph(saver))
    replay = await shared.execute_shared('q', run_id='binding', services=services_for([]),
                                        graph=shared.build_execution_graph(saver))
    assert replay['answer'] == 'saved'
    with pytest.raises(ValueError, match='shared_checkpoint_binding_mismatch'):
        await shared.execute_shared('changed', run_id='binding', services=services_for([]),
                                    graph=shared.build_execution_graph(saver))


@pytest.mark.asyncio
@pytest.mark.parametrize('agent_trial', [False, True])
async def test_actual_chat_and_agent_trial_reach_shared_model_and_tool_nodes(monkeypatch, tmp_path, agent_trial):
    from test_id_generation_safety import fixture_db
    from test_p12_cloud_regressions import seed_chat, tool_call
    from test_p12_tool_calling import calculator_center

    from eic import main
    from eic.models import AgentGraphCheckpoint, AgentRun, ChatMessage
    monkeypatch.setattr(main, 'tool_center', calculator_center())
    nodes, requests = [], []
    original = shared.model_decision
    async def traced(state):
        nodes.append(('model_decision', state['graph_version']))
        return await original(state)
    monkeypatch.setattr(shared, 'model_decision', traced)
    async with fixture_db(monkeypatch, tmp_path, is_super_admin=True) as sessions:
        config = {'isSkill': True, 'skills': [{'aiSkillToolManagementId': 501, 'isSelect': True}]}
        await seed_chat(sessions, app_config=config if agent_trial else None)
        async def provider(model, question, **kwargs):
            requests.append(kwargs['messages'])
            PROVIDER_MOCK_REQUESTS.append({'entry': 'trial' if agent_trial else 'chat'})
            if len(requests) == 1:
                kwargs['tool_call_sink'].append(tool_call(1))
            else:
                assert any(m['role'] == 'tool' for m in kwargs['messages'])
                yield 'shared-answer'
        monkeypatch.setattr(main, 'stream_chat_completion', provider)
        output = ''.join([item async for item in main.sse_events('fixture', main.Principal(11, 1, 'fixture', True))])
        assert 'shared-answer' in output
        assert nodes == [('model_decision', AGENT_GRAPH_VERSION)] * 2
        async with sessions() as session:
            assert (await session.get(ChatMessage, 301)).status == 'completed'
            run = await session.scalar(select(AgentRun))
            assert run.graph_version == AGENT_GRAPH_VERSION
            assert list(await session.scalars(select(AgentGraphCheckpoint)))


@pytest.mark.asyncio
async def test_actual_worker_uses_same_nodes_without_authorizing_agent_tasks(monkeypatch, tmp_path):
    from test_transactional_outbox import fixture_db

    from eic import workers
    from eic.core import tasks
    from eic.models import ScheduledTask, TaskRun
    nodes = []
    original = shared.model_decision
    async def traced(state):
        nodes.append(('model_decision', state['graph_version']))
        return await original(state)
    monkeypatch.setattr(shared, 'model_decision', traced)
    async with fixture_db(monkeypatch, tmp_path) as (sessions, _):
        async with sessions() as session:
            task = await session.get(ScheduledTask, 200)
            run_id, _ = await tasks.stage_run(session, task)
            await session.commit()
        result = await workers.run_scheduled_task({}, 200, 1, run_id)
        assert result['status'] == 'success'
        assert nodes == [('model_decision', AGENT_GRAPH_VERSION)] * 2
        async with sessions() as session:
            assert (await session.get(TaskRun, run_id)).result['graphVersion'] == AGENT_GRAPH_VERSION
        for payload in [{'agentId': 401}, {'appId': 401}, {'handlerType': 'shell', 'knowledge_base_id': 100}]:
            with pytest.raises(ValueError):
                tasks.normalize_input(payload)


def test_trial_frontend_call_chain_is_bound_to_chat():
    root = Path(__file__).parents[2]
    source = (root / 'frontend/src/pages/ai/MyAIChat.vue').read_text(encoding='utf-8')
    assert 'await createNewConversation(props.agentId)' in source
    assert 'startEmbeddedTrial' in source


@pytest.mark.asyncio
async def test_actual_chat_two_approvals_resume_pending_graph_calls(monkeypatch, tmp_path):
    from test_id_generation_safety import fixture_db
    from test_p2_approval import approve, ticket
    from test_p12_cloud_regressions import seed_chat, tool_call

    from eic import main
    from eic.ai.tool_center import ToolCenter, ToolDefinition
    from eic.core import agent_approval, generation
    from eic.models import AgentRun, AgentToolCall, ChatMessage, Resource
    calls, requests = [], []
    center = ToolCenter()
    def write(arguments):
        calls.append(arguments)
        return {'ok': True, 'secret': 'private-result'}
    center.register(ToolDefinition('write_fixture', '审批桩', write, requires_approval=True))
    monkeypatch.setattr(main, 'tool_center', center)
    principal = main.Principal(11, 1, 'fixture', True)
    async with fixture_db(monkeypatch, tmp_path, is_super_admin=True) as sessions:
        await seed_chat(sessions)
        async with sessions() as session:
            (await session.get(Resource, 501)).data = {'enabled': True, 'allowedTools': ['write_fixture']}
            await session.commit()
        async def provider(model, question, **kwargs):
            requests.append(kwargs['messages'])
            PROVIDER_MOCK_REQUESTS.append({'entry': 'two-approvals'})
            if len(requests) == 1:
                kwargs['tool_call_sink'].extend([
                    tool_call(1, 'write_fixture', {'value': 1}),
                    tool_call(2, 'write_fixture', {'value': 2})])
                yield ''
            else:
                assert [item['tool_call_id'] for item in kwargs['messages'] if item['role'] == 'tool'] == [
                    'call-1', 'call-2']
                yield 'approved-answer'
        monkeypatch.setattr(main, 'stream_chat_completion', provider)
        try:
            _ = [item async for item in main.sse_events('fixture', principal)]
            for index in (1, 2):
                async with sessions() as session:
                    run = await session.scalar(select(AgentRun))
                    cp = await agent_approval.pending(session, run)
                    assert cp is not None and cp.state_json['callId'] == f'call-{index}'
                    summary = agent_approval.summary(run, cp, can_decide=True)
                    envelope = agent_approval.unseal(cp.state_json['approvalEnvelope'])
                    assert envelope['graphResumeCallId'] == f'call-{index}'
                    assert envelope['remainingCalls'] == []
                assert len(calls) == index - 1 and len(requests) == 1
                await approve(summary, idempotencyKey=f'decide-{index}')
                current_ticket = ticket(summary, idempotencyKey=f'consume-{index}')
                assert (await main.chat_continue_approved(current_ticket, principal))['toolStatus'] == 'completed'
                assert (await main.chat_continue_approved(current_ticket, principal))['replayed']
                _ = [item async for item in main.sse_events('fixture', principal)]
            assert calls == [{'value': 1}, {'value': 2}] and len(requests) == 2
            async with sessions() as session:
                message = await session.get(ChatMessage, 301)
                assert message.status == 'completed'
                answer = await session.scalar(select(ChatMessage).where(ChatMessage.role == 'assistant'))
                assert answer.content == 'approved-answer'
                records = list(await session.scalars(select(AgentToolCall)))
                assert len(records) == 2 and all(row.effect_status == 'applied' for row in records)
        finally:
            await generation.stop_producers()


@pytest.mark.asyncio
@pytest.mark.parametrize('unknown', [False, True])
async def test_database_checkpoint_after_tool_recovers_without_reexecution(monkeypatch, tmp_path, unknown):
    from test_id_generation_safety import fixture_db
    from test_p11_durable_worker import make_run

    from eic.core import agent_runtime, agent_worker
    async with fixture_db(monkeypatch, tmp_path) as sessions:
        run_id, worker_token = await make_run(sessions)
        owner = agent_worker.OWNER.set((run_id, worker_token))
        calls = []
        try:
            first = services_for([decision(), ModelDecision('answer', 'done')], calls=calls, unknown=unknown)
            original_record = shared.record_tool_result
            async def crash_before_record(state):
                raise RuntimeError('simulated-process-loss')
            monkeypatch.setattr(shared, 'record_tool_result', crash_before_record)
            broken = shared.build_execution_graph(shared.ExecutorCheckpointer(sessions))
            with pytest.raises(RuntimeError, match='simulated-process-loss'):
                await shared.execute_shared('q', run_id=run_id, services=first, graph=broken)
            async with sessions() as session:
                cp = await agent_runtime.runtime_store.latest_checkpoint(session, run_id=run_id)
                recovery = await agent_runtime.runtime_store.decide_recovery(
                    session, run_id=run_id, expected_graph_version=AGENT_GRAPH_VERSION)
                assert cp.state_json['providerStarted'] is False
                assert cp.resume_policy == ('manual_review' if unknown else 'safe_replay')
                assert recovery.action == ('manual_review' if unknown else 'safe_replay')
            assert calls == ['one']
            if not unknown:
                monkeypatch.setattr(shared, 'record_tool_result', original_record)
                second = services_for([ModelDecision('answer', 'recovered')], calls=calls)
                result = await shared.execute_shared('q', run_id=run_id, services=second,
                    graph=shared.build_execution_graph(shared.ExecutorCheckpointer(sessions)))
                assert result['answer'] == 'recovered' and calls == ['one']
        finally:
            agent_worker.OWNER.reset(owner)


@pytest.mark.asyncio
async def test_service_result_commit_is_idempotent_and_conflicts_are_rejected(monkeypatch, tmp_path):
    from sqlalchemy import func
    from test_id_generation_safety import fixture_db
    from test_p11_durable_worker import make_run

    from eic import main
    from eic.ai.tool_center import ToolExecutionError
    from eic.models import AgentEvent, ToolAudit
    async with fixture_db(monkeypatch, tmp_path) as sessions:
        run_id, _ = await make_run(sessions)
        arguments = {'principal': main.Principal(11, 1, 'fixture', True), 'request_id': 'fixture',
            'agent_run_id': run_id, 'call_id': 'one', 'tool_name': 'fixture', 'arguments': {},
            'definition': None,
            'result': {'status': 'completed', 'effectStatus': 'applied', 'result': {'ok': True}}}
        await main.record_chat_tool_result(**arguments)
        await main.record_chat_tool_result(**arguments)
        with pytest.raises(ToolExecutionError, match='持久工具结果'):
            await main.record_chat_tool_result(**{**arguments, 'result': {
                'status': 'completed', 'effectStatus': 'applied', 'result': {'ok': False}}})
        async with sessions() as session:
            assert await session.scalar(select(func.count()).select_from(ToolAudit)) == 1
            assert await session.scalar(select(func.count()).select_from(AgentEvent).where(
                AgentEvent.event_type == 'tool_result')) == 1
