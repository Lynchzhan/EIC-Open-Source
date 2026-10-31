"""MCP 生命周期定向验证：官方 SDK、关闭等待和入口配置贯通。"""

import asyncio
import json
import logging

import httpx
import pytest

from eic.ai.mcp_client import HttpMcpClient
from eic.ai.mcp_sdk_adapter import _SDK_LOG_SCOPE, _SDK_SOURCE_ROOT, _SdkRootLogBoundary


class Server:
    def __init__(self, delay=0):
        self.delay = delay
        self.calls = []

    async def __call__(self, request):
        body = json.loads(request.content)
        self.calls.append(body)
        method = body.get('method')
        if method.startswith('notifications/'):
            return httpx.Response(202)
        if self.delay and method == 'tools/call':
            await asyncio.sleep(self.delay)
        if method == 'initialize':
            result = {'protocolVersion': '2025-03-26', 'capabilities': {}, 'serverInfo': {'name': 'synthetic', 'version': '1'}}
        elif method == 'tools/list':
            result = {'tools': [{'name': 'lookup', 'inputSchema': {'type': 'object'}}]}
        else:
            result = {'content': [{'type': 'text', 'text': 'synthetic'}], 'isError': False}
        return httpx.Response(200, json={'jsonrpc': '2.0', 'id': body['id'], 'result': result})


@pytest.mark.parametrize('mode', ['success', 'replay', 'not-advertised', 'timeout', 'cancel', 'invalid-bool', 'invalid-zero', 'invalid-inf', 'invalid-string'])
def test_real_service_entry_lifecycle(mode, monkeypatch, tmp_path):
    from fastapi import HTTPException
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from eic import main
    from eic.models import Base, Resource, ToolAudit, ToolExecutionIntent

    server = Server(delay=30 if mode in {'timeout', 'cancel'} else 0)
    products = []

    async def permission(code, principal, session):
        assert code == 'mcp.call' and principal.tenant_id == 777

    async def identifier(session):
        identifier.value += 1
        return identifier.value
    identifier.value = 800
    monkeypatch.setattr(main, 'ensure_permission', permission)
    monkeypatch.setattr(main, 'new_id', identifier)

    async def run():
        engine = create_async_engine('sqlite+aiosqlite:///' + (tmp_path / 'synthetic.db').as_posix())
        async with engine.begin() as connection:
            await connection.run_sync(lambda sync: Base.metadata.create_all(sync, tables=[Resource.__table__, ToolAudit.__table__, ToolExecutionIntent.__table__]))
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        monkeypatch.setattr(main, 'SessionLocal', sessions)
        try:
            async with httpx.AsyncClient(transport=httpx.MockTransport(server)) as borrowed:
                def factory(config, **kwargs):
                    product = HttpMcpClient(config, client=borrowed, **kwargs)
                    products.append(product)
                    return product
                monkeypatch.setattr(main, 'HttpMcpClient', factory)
                async with sessions() as session:
                    timeout = {'invalid-bool': True, 'invalid-zero': 0, 'invalid-inf': float('inf'), 'invalid-string': '2'}.get(mode, .08 if mode == 'timeout' else 2)
                    resource=Resource(id=1, tenant_id=777, resource_type='mcps', data={'transport': 'http', 'endpoint': 'https://mcp.invalid/rpc', 'timeout': 2 if mode=='invalid-inf' else timeout})
                    session.add(resource)
                    await session.commit()
                    if mode=='invalid-inf':
                        # 严格 JSON 存储不允许 Infinity；直接注入已加载配置验证执行边界。
                        from sqlalchemy.orm.attributes import set_committed_value
                        set_committed_value(resource,'data',{**resource.data,'timeout':timeout})
                    principal = main.Principal(778, 777, 'synthetic', False)
                    payload = {'mcpId': 1, 'name': 'absent' if mode == 'not-advertised' else 'lookup', 'arguments': {}, 'idempotencyKey': 'synthetic-key'}
                    if mode.startswith('invalid-'):
                        with pytest.raises(HTTPException) as invalid:
                            await main.mcp_call(payload, principal, session)
                        assert invalid.value.status_code == 502
                        assert products == [] and server.calls == []
                        return
                    if mode == 'cancel':
                        task = asyncio.create_task(main.mcp_call(payload, principal, session))
                        for _ in range(100):
                            if any(x.get('method') == 'tools/call' for x in server.calls):
                                break
                            await asyncio.sleep(.005)
                        task.cancel()
                        with pytest.raises(asyncio.CancelledError):
                            await task
                    elif mode in {'not-advertised', 'timeout'}:
                        with pytest.raises(HTTPException) as failure:
                            await main.mcp_call(payload, principal, session)
                        assert failure.value.status_code == 502
                    else:
                        assert (await main.mcp_call(payload, principal, session))['content'][0]['text'] == 'synthetic'
                        if mode == 'replay':
                            assert (await main.mcp_call(payload, principal, session))['content'][0]['text'] == 'synthetic'
                    assert all(p._worker is None for p in products)
                    assert products[0]._timeout == (.08 if mode == 'timeout' else 2)
                    assert sum(x.get('method') == 'tools/call' for x in server.calls) == (0 if mode == 'not-advertised' else 1)
                    if mode in {'timeout', 'cancel'}:
                        intent = await session.scalar(select(ToolExecutionIntent))
                        assert intent.state == 'unknown'
                        with pytest.raises(HTTPException) as replay:
                            await main.mcp_call(payload, principal, session)
                        assert replay.value.status_code == 409
                        assert sum(x.get('method') == 'tools/call' for x in server.calls) == 1
                        assert all(p._worker is None for p in products)
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_client_cancel_waits_for_close_and_never_retries():
    server = Server(delay=30)

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(server)) as borrowed:
            client = HttpMcpClient({'transport': 'http', 'endpoint': 'https://mcp.invalid/rpc'}, client=borrowed, timeout=2)
            task = asyncio.create_task(client.call_tool('lookup', {}))
            for _ in range(100):
                if any(x.get('method') == 'tools/call' for x in server.calls):
                    break
                await asyncio.sleep(.005)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            await asyncio.sleep(0)
            assert sum(x.get('method') == 'tools/call' for x in server.calls) == 1
            assert client._worker is None
    asyncio.run(run())


def test_root_filter_is_scoped_and_redacts_sdk_record():
    record = logging.LogRecord('mcp.shared.session', logging.ERROR, '/frozen/sdk/mcp/shared/session.py', 1, 'secret-token', (), None)
    assert _SdkRootLogBoundary().filter(record) is True
    assert record.msg == 'secret-token'
    token = _SDK_LOG_SCOPE.set(True)
    try:
        record = logging.LogRecord('root', logging.ERROR, _SDK_SOURCE_ROOT + 'shared/session.py', 1, 'secret-token', (), None)
        assert _SdkRootLogBoundary().filter(record) is True
        assert record.msg == 'MCP SDK event (details omitted)'
    finally:
        _SDK_LOG_SCOPE.reset(token)


def test_real_sdk_root_validation_log_redacted_without_business_log_change(caplog):
    server = Server()
    async def handler(request):
        response = await server(request)
        if json.loads(request.content).get('method') == 'tools/call':
            envelope = json.loads(response.content)
            invalid = {'jsonrpc': '2.0', 'method': 'notifications/progress', 'params': {'progressToken': 'synthetic', 'progress': 'sensitive-root-field'}}
            return httpx.Response(200, headers={'content-type': 'text/event-stream'}, content='event: message\ndata: ' + json.dumps(invalid) + '\n\nevent: message\ndata: ' + json.dumps(envelope) + '\n\n')
        return response
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as borrowed:
            target = HttpMcpClient({'transport': 'http', 'endpoint': 'https://mcp.invalid/rpc'}, client=borrowed, timeout=1)
            assert (await target.call_tool('lookup', {}))['content'][0]['text'] == 'synthetic'
        token = _SDK_LOG_SCOPE.set(True)
        try:
            logging.getLogger().warning('business-root-must-remain')
            logging.getLogger('synthetic.business').warning('business-child-must-remain')
        finally:
            _SDK_LOG_SCOPE.reset(token)
        logging.getLogger().warning('outside-root-must-remain')
    with caplog.at_level(logging.DEBUG):
        asyncio.run(run())
    sdk_root = [r for r in caplog.records if r.name == 'root' and str(r.pathname).replace('\\', '/').startswith(_SDK_SOURCE_ROOT)]
    assert sdk_root, '必须由官方SDK实际接收循环生成root验证日志'
    assert all(r.getMessage() == 'MCP SDK event (details omitted)' for r in sdk_root)
    assert 'sensitive-root-field' not in caplog.text
    assert all(text in caplog.text for text in ('business-root-must-remain', 'business-child-must-remain', 'outside-root-must-remain'))
    assert sum(x.get('method') == 'tools/call' for x in server.calls) == 1
