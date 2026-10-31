"""提交取消与最终工具派发之间的真实服务 guard 契约。"""
import asyncio

import pytest
from test_id_generation_safety import claim, fixture_db

from eic import main
from eic.ai.agent_graph import ModelDecision, ToolCallRequest
from eic.ai.shared_executor import ExecutionServices, execute_shared
from eic.core import generation


def test_committed_cancel_fences_shared_executor_before_dispatch(monkeypatch, tmp_path):
    async def run():
        ready, release = asyncio.Event(), asyncio.Event()
        effects = []
        async with fixture_db(monkeypatch, tmp_path, is_super_admin=True) as sessions:
            await claim(sessions)
            principal = main.Principal(11, 1, 'fixture', True)

            async def model(state):
                return ModelDecision('tool_calls', tool_calls=(ToolCallRequest('one', 'fixture', {}),))

            async def validate(request):
                ready.set()
                await release.wait()
                return {'requiresApproval': False}

            async def guard():
                if await main.is_chat_cancelled('fixture', 1):
                    raise generation.LeaseLost('Generation cancelled')

            async def execute(request, remaining_ms):
                effects.append(request.call_id)
                return {'callId': request.call_id, 'toolName': request.name, 'status': 'completed',
                        'effectStatus': 'applied', 'result': {}}

            services = ExecutionServices(model=model, validate_tool=validate, execute_tool=execute, guard=guard)
            pending = asyncio.create_task(execute_shared('fixture', run_id='fixture-cancel', services=services))
            try:
                await asyncio.wait_for(ready.wait(), 5)
                async with sessions() as session:
                    await main.chat_history_cancel('fixture', principal, session)
                release.set()
                with pytest.raises(generation.LeaseLost):
                    await pending
                assert effects == []
            finally:
                release.set()
                if not pending.done():
                    pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
    asyncio.run(run())
