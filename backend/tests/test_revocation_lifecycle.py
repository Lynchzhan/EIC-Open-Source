"""撤权后订阅者立即停止读取；独立 producer 安全终态且不执行工具。"""
import asyncio

import pytest
from sqlalchemy import select

from eic import main
from eic.core import generation
from eic.models import AgentToolCall, ChatEvent, ChatMessage
from test_p12_cloud_regressions import calculator_center, fixture_db, seed_chat, tool_call
from eic.ai.tool_center import ToolDefinition
from eic.models import User


@pytest.mark.parametrize('wait_for_producer', [False, True])
def test_revocation_lifecycle(monkeypatch, tmp_path, wait_for_producer):
    executions = []
    center = calculator_center()
    center._tools['calculator'] = ToolDefinition('calculator', '计算器',
        lambda _: executions.append(1) or {'value': 3})
    monkeypatch.setattr(main, 'tool_center', center)

    async def run():
        async with fixture_db(monkeypatch, tmp_path, is_super_admin=True) as sessions:
            await seed_chat(sessions)

            async def provider(model, question, **kwargs):
                async with sessions() as session:
                    (await session.get(User, 11)).is_active = False
                    await session.commit()
                kwargs['tool_call_sink'].append(tool_call(1))
                yield ''

            monkeypatch.setattr(main, 'stream_chat_completion', provider)
            events = [x async for x in main.sse_events('fixture', main.Principal(11, 1, 'fixture', True))]
            if wait_for_producer:
                async with asyncio.timeout(10):
                    await asyncio.gather(*list(generation.PRODUCERS))
            async with sessions() as session:
                stored = await session.get(ChatMessage, 301)
                tool_rows = list(await session.scalars(select(AgentToolCall)))
                done = list(await session.scalars(select(ChatEvent).where(ChatEvent.event_type == 'done')))
                print({'waitForProducer': wait_for_producer, 'status': stored.status,
                    'toolCalls': len(tool_rows), 'done': len(done), 'subscriberChunks': len(events)})
                assert not executions and not tool_rows
                if wait_for_producer:
                    assert stored.status == 'failed'
                    assert len(done) == 1

    asyncio.run(run())


def test_revoked_subscriber_exits_before_provider_cleanup(monkeypatch, tmp_path):
    """固定竞态：订阅者必须立即失去读取权，生产者随后自行收口。"""
    async def run():
        async with fixture_db(monkeypatch, tmp_path, is_super_admin=True) as sessions:
            await seed_chat(sessions)
            release = asyncio.Event()

            async def provider(model, question, **kwargs):
                async with sessions() as session:
                    (await session.get(User, 11)).is_active = False
                    await session.commit()
                await release.wait()
                kwargs['tool_call_sink'].append(tool_call(1))
                yield ''

            monkeypatch.setattr(main, 'stream_chat_completion', provider)
            try:
                async with asyncio.timeout(5):
                    chunks = [x async for x in main.sse_events('fixture', main.Principal(11, 1, 'fixture', True))]
                assert not any('event: aimsg' in c for c in chunks)
                async with sessions() as session:
                    assert (await session.get(ChatMessage, 301)).status == 'generating'
            finally:
                release.set()
                async with asyncio.timeout(10):
                    await asyncio.gather(*list(generation.PRODUCERS))
            async with sessions() as session:
                assert (await session.get(ChatMessage, 301)).status == 'failed'
                assert not list(await session.scalars(select(AgentToolCall)))
                assert len(list(await session.scalars(select(ChatEvent).where(ChatEvent.event_type == 'done')))) == 1

    asyncio.run(run())
