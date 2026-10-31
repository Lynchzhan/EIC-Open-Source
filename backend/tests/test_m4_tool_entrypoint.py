"""真正服务入口的审计故障、重启重放与身份冲突验证。"""
import asyncio

import pytest
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from test_id_generation_safety import fixture_db

from eic import main
from eic.ai.tool_center import ToolCenter, ToolDefinition
from eic.models import Resource, ToolAudit, ToolExecutionIntent


@pytest.mark.parametrize('mode', ['late_commit', 'restart_replay', 'different_tool', 'first_commit'])
def test_service_intent_and_audit_survive_fresh_tool_center(monkeypatch, tmp_path, mode):
    effects = []

    def center():
        tools = ToolCenter()
        for name in ('one', 'two'):
            tools.register(ToolDefinition(name, 'fixture', lambda args: effects.append(args) or {'ok': True}))
        monkeypatch.setattr(main, 'tool_center', tools)

    async def run():
        center()
        async with fixture_db(monkeypatch, tmp_path, is_super_admin=True) as sessions:
            async with sessions() as session:
                session.add(Resource(id=601, tenant_id=1, owner_id=11, resource_type='skills',
                                     data={'enabled': True, 'allowedTools': ['one', 'two']}))
                await session.commit()
            payload = {'name': 'one', 'arguments': {'value': 1}, 'idempotencyKey': 'durable-key'}
            principal = main.Principal(11, 1, 'fixture', True)
            async with sessions() as session:
                if mode in {'late_commit', 'first_commit'}:
                    commit = session.commit
                    count = 0

                    async def fault():
                        nonlocal count
                        count += 1
                        if count == (1 if mode == 'first_commit' else 2):
                            raise SQLAlchemyError('synthetic-audit-store-fault')
                        await commit()

                    monkeypatch.setattr(session, 'commit', fault)
                    with pytest.raises(main.HTTPException) as persistence_error:
                        await main.execute_tool(payload, principal, session)
                    assert persistence_error.value.status_code == 503
                    await session.rollback()
                else:
                    first = await main.execute_tool(payload, principal, session)
                    assert first['replayed'] is False
            center()
            async with sessions() as session:
                rows = list(await session.scalars(select(ToolExecutionIntent)))
                audits = list(await session.scalars(select(ToolAudit)))
                if mode == 'first_commit':
                    assert effects == [] and rows == [] and audits == []
                    return
                assert len(effects) == 1
                assert len(rows) == 1 and len(audits) == 1
                if mode == 'restart_replay':
                    replay = await main.execute_tool(payload, principal, session)
                    assert replay['replayed'] is True and replay['result'] == {'ok': True}
                else:
                    with pytest.raises(main.HTTPException) as raised:
                        await main.execute_tool({**payload, **({'name': 'two'} if mode == 'different_tool' else {})}, principal, session)
                    assert raised.value.detail['errorCode'] == ('duplicate_tool_call_id' if mode == 'different_tool' else 'tool_outcome_uncertain')
                assert len(effects) == 1
    asyncio.run(run())
