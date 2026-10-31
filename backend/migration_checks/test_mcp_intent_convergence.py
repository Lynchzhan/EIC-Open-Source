"""真实MCP入口、官方SDK与合成SQLite故障注入；不产生真实工具副作用。"""

import asyncio
import dataclasses
import json
from contextlib import asynccontextmanager

import httpx
import pytest
from sqlalchemy import event, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from eic.core.tool_intent import (
    ToolIntentConflict,
    ToolIntentConvergenceScope,
    converge_tool_intent_unknown,
)
from eic.models import Base, Resource, ToolAudit, ToolExecutionIntent


def db_error():
    return OperationalError('synthetic-write', {}, RuntimeError('synthetic-offline-db-failure'))


@pytest.mark.parametrize('fault', ['cancel', 'finish', 'new-session', 'committed-then-error'])
def test_service_fault_convergence_and_no_replay(fault, tmp_path, monkeypatch, caplog):
    from fastapi import HTTPException

    from eic import main
    from eic.ai.mcp_client import HttpMcpClient

    async def run():
        engine = create_async_engine('sqlite+aiosqlite:///' + (tmp_path / 'fixture.db').as_posix())
        async with engine.begin() as conn:
            await conn.run_sync(lambda sync: Base.metadata.create_all(sync, tables=[Resource.__table__, ToolAudit.__table__, ToolExecutionIntent.__table__]))
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        new_sessions = []
        @asynccontextmanager
        async def convergence_sessions():
            async with sessions() as fresh:
                new_sessions.append(fresh)
                if fault == 'new-session':
                    def fail_commit(sync):
                        # 只破坏故障后收敛事务，不提前破坏新增的握手准入审计。
                        if any(isinstance(row, ToolAudit) and row.tool_name == 'lookup' and row.outcome == 'unknown'
                               for row in fresh.dirty):
                            raise db_error()
                    event.listen(fresh.sync_session, 'before_commit', fail_commit)
                yield fresh
        monkeypatch.setattr(main, 'SessionLocal', convergence_sessions)
        async def allow(code, principal, session):
            assert principal.tenant_id == 601 and code == 'mcp.call'
        monkeypatch.setattr(main, 'ensure_permission', allow)
        counter = [900]
        async def identifier(session):
            counter[0] += 1
            return counter[0]
        monkeypatch.setattr(main, 'new_id', identifier)
        called, products = [], []
        entered = asyncio.Event()
        async def server(request):
            if request.method != 'POST':
                return httpx.Response(405)
            body = json.loads(request.content)
            method = body['method']
            if method.startswith('notifications/'):
                return httpx.Response(202)
            if method == 'initialize':
                result = {'protocolVersion': '2025-03-26', 'capabilities': {}, 'serverInfo': {'name': 'synthetic', 'version': '1'}}
            elif method == 'tools/list':
                result = {'tools': [{'name': 'lookup', 'inputSchema': {'type': 'object'}}]}
            else:
                called.append(body)
                # 从另一个session实证副作用前claim和audit已经提交。
                async with sessions() as verify:
                    assert (await verify.scalar(select(ToolExecutionIntent))).state == 'executing'
                    assert (await verify.scalar(select(ToolAudit).where(ToolAudit.tool_name == 'lookup'))).outcome == 'intent'
                entered.set()
                if fault in {'cancel', 'new-session'}:
                    await asyncio.sleep(60)
                result = {'content': [{'type': 'text', 'text': 'synthetic-result'}], 'isError': False}
            return httpx.Response(200, json={'jsonrpc': '2.0', 'id': body['id'], 'result': result})
        try:
            async with httpx.AsyncClient(transport=httpx.MockTransport(server)) as borrowed:
                def product(config, **kwargs):
                    result = HttpMcpClient(config, client=borrowed, **kwargs)
                    products.append(result)
                    return result
                monkeypatch.setattr(main, 'HttpMcpClient', product)
                async with sessions() as session:
                    session.add(Resource(id=1, tenant_id=601, resource_type='mcps', data={'transport': 'http', 'endpoint': 'https://mcp.invalid/rpc', 'timeout': 2}))
                    await session.commit()
                    real_commit = session.commit
                    async def failed_finish_commit():
                        if any(isinstance(x, ToolExecutionIntent) and x.state == 'succeeded' for x in session.dirty):
                            if fault == 'committed-then-error':
                                await real_commit()
                            raise db_error()
                        await real_commit()
                    if fault in {'finish', 'committed-then-error'}:
                        monkeypatch.setattr(session, 'commit', failed_finish_commit)
                    principal = main.Principal(602, 601, 'synthetic', False)
                    payload = {'mcpId': 1, 'name': 'lookup', 'arguments': {}, 'idempotencyKey': 'synthetic-convergence-key'}
                    if fault in {'cancel', 'new-session'}:
                        task = asyncio.create_task(main.mcp_call(payload, principal, session))
                        try:
                            await asyncio.wait_for(entered.wait(), 5)
                        finally:
                            task.cancel()
                        with pytest.raises(asyncio.CancelledError):
                            await task
                    else:
                        with pytest.raises(HTTPException) as failure:
                            await main.mcp_call(payload, principal, session)
                        assert failure.value.status_code == 503
                    assert products[0]._worker is None
                    assert new_sessions and all(x is not session for x in new_sessions)
                async with sessions() as verify:
                    intent = await verify.scalar(select(ToolExecutionIntent))
                    audit = await verify.scalar(select(ToolAudit).where(ToolAudit.tool_name == 'lookup'))
                    expected = 'executing' if fault == 'new-session' else 'succeeded' if fault == 'committed-then-error' else 'unknown'
                    assert intent.state == expected
                    assert audit.outcome == ('intent' if fault == 'new-session' else 'success' if fault == 'committed-then-error' else 'unknown')
                    if expected != 'succeeded':
                        assert intent.result_encrypted is None
                # 新请求、全新原session：未知/执行中拒绝；已提交成功只恢复密文结果。
                async with sessions() as repeat:
                    if fault == 'committed-then-error':
                        assert (await main.mcp_call(payload, principal, repeat))['content'][0]['text'] == 'synthetic-result'
                    else:
                        with pytest.raises(HTTPException) as replay:
                            await main.mcp_call(payload, principal, repeat)
                        assert replay.value.status_code == 409
                assert len(called) == 1 and all(x._worker is None for x in products)
                if fault == 'new-session':
                    assert 'convergence unavailable' in caplog.text
        finally:
            await engine.dispose()
    asyncio.run(run())


@pytest.mark.parametrize('field', ['tenant_id', 'user_id', 'tool_name', 'intent_id', 'input_digest', 'audit_id', 'audit_tool_name', 'audit_input_digest', 'succeeded'])
def test_convergence_scope_and_succeeded_guard(field, tmp_path):
    async def run():
        engine = create_async_engine('sqlite+aiosqlite:///' + (tmp_path / 'scope.db').as_posix())
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        async with engine.begin() as conn:
            await conn.run_sync(lambda sync: Base.metadata.create_all(sync, tables=[ToolAudit.__table__, ToolExecutionIntent.__table__]))
        try:
            async with sessions() as s:
                s.add(ToolExecutionIntent(id='synthetic-intent', tenant_id=601, user_id=602, tool_name='mcp:lookup', input_digest='a'*64, idempotency_key_digest='b'*64, state='succeeded' if field == 'succeeded' else 'executing', result_encrypted='preserve-existing' if field == 'succeeded' else None))
                s.add(ToolAudit(id=701, tenant_id=601, user_id=602, tool_name='lookup', tool_type='mcp', input_digest='c'*64, outcome='success' if field == 'succeeded' else 'intent'))
                await s.commit()
            scope = ToolIntentConvergenceScope('synthetic-intent', 601, 602, 'mcp:lookup', 'a'*64, 701, 'lookup', 'c'*64)
            if field == 'succeeded':
                assert await converge_tool_intent_unknown(sessions, scope=scope) is False
            else:
                value = getattr(scope, field)
                scope = dataclasses.replace(scope, **{field: value+1 if isinstance(value, int) else value+'-foreign'})
                with pytest.raises(ToolIntentConflict):
                    await converge_tool_intent_unknown(sessions, scope=scope)
            async with sessions() as s:
                row = await s.get(ToolExecutionIntent, 'synthetic-intent')
                assert row.state == ('succeeded' if field == 'succeeded' else 'executing')
                assert row.result_encrypted == ('preserve-existing' if field == 'succeeded' else None)
        finally:
            await engine.dispose()
    asyncio.run(run())
