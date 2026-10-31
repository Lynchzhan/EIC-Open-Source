"""非 Locked：入口和共享图总预算隔离契约。"""
import asyncio
import time
import pytest
from langgraph.checkpoint.memory import InMemorySaver
from eic.ai import agent_graph, shared_executor as shared
from eic.ai.agent_graph import AgentBudget
from eic.ai.tool_center import ToolCenter

def services(**kwargs):
    async def model(state):
        return {'content': 'ok'}
    async def tool(*args):
        raise AssertionError('unexpected tool dispatch')
    return shared.ExecutionServices(model=model, execute_tool=tool,
        validate_tool=tool, budget=AgentBudget(max_total_ms=20), **kwargs)

def test_plan_consumes_public_entry_budget(monkeypatch):
    original = agent_graph.plan_chat_execution
    def slow(*args, **kwargs):
        time.sleep(.04)
        return original(*args, **kwargs)
    monkeypatch.setattr(agent_graph, 'plan_chat_execution', slow)
    async def forbidden(state):
        raise AssertionError('expired setup dispatched model')
    result = asyncio.run(agent_graph.execute_tool_calling_agent('q',
        model_decider=forbidden, tool_center=ToolCenter(), budget=AgentBudget(max_total_ms=20)))
    assert result['stop_reason'] == 'total_budget_exceeded'

def test_synchronous_build_exhaustion_stops_before_checkpoint(monkeypatch):
    original = shared.build_execution_graph
    def slow(*args, **kwargs):
        time.sleep(.04)
        return original(*args, **kwargs)
    monkeypatch.setattr(shared, 'build_execution_graph', slow)
    result = asyncio.run(shared.execute_shared('q', run_id='r', services=services()))
    assert result['stop_reason'] == 'total_budget_exceeded'
    assert result['provider_outcome'] == 'not_started'

def test_checkpoint_read_shares_deadline():
    class SlowSaver(InMemorySaver):
        async def aget_tuple(self, config):
            await asyncio.sleep(.1)
            raise AssertionError('checkpoint read outlived deadline')
    result = asyncio.run(shared.execute_shared('q', run_id='r', services=services(),
        graph=shared.build_execution_graph(SlowSaver())))
    assert result['stop_reason'] == 'total_budget_exceeded'

def test_guard_expiration_prevents_model_dispatch():
    async def guard():
        await asyncio.sleep(.05)
    result = asyncio.run(shared.execute_shared('q', run_id='r', services=services(guard=guard)))
    assert result['stop_reason'] == 'total_budget_exceeded'
    assert result['provider_outcome'] == 'not_started'

def test_graph_instances_have_independent_checkpoint_state():
    first, second = shared.build_execution_graph(), shared.build_execution_graph()
    assert first is not second
    assert first.checkpointer is not second.checkpointer
    async def run():
        s = services()
        s.budget = AgentBudget(max_total_ms=1000)
        await shared.execute_shared('private', run_id='same-id', services=s, graph=first)
        assert await second.checkpointer.aget_tuple({'configurable': {'thread_id': 'same-id'}}) is None
    asyncio.run(run())

def test_external_cancellation_is_not_budget_success():
    async def run():
        async def guard():
            await asyncio.sleep(10)
        task = asyncio.create_task(shared.execute_shared('q', run_id='r', services=services(guard=guard)))
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    asyncio.run(run())

def test_expired_tool_dispatch_remains_unknown_in_checkpoint():
    async def run():
        effects = []
        async def model(state):
            return {'tool_calls': [{'call_id': 'one', 'name': 'tool', 'arguments': {}}]}
        async def validate(request):
            return {}
        async def tool(request, remaining):
            effects.append(request.call_id)
            await asyncio.sleep(10)
        graph = shared.build_execution_graph()
        svc = shared.ExecutionServices(model=model, validate_tool=validate, execute_tool=tool,
            budget=AgentBudget(max_total_ms=100))
        result = await shared.execute_shared('q', run_id='uncertain', services=svc, graph=graph)
        assert effects == ['one']
        assert result['effect_status'] == 'unknown'
        checkpoint = await graph.checkpointer.aget_tuple({'configurable': {'thread_id': 'uncertain'}})
        assert checkpoint.checkpoint['channel_values']['effect_status'] == 'unknown'
    asyncio.run(run())
