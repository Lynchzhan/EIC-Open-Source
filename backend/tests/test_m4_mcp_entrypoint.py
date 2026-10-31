"""服务入口标准握手、派发前拒绝和日志敏感字段独立回归。"""
import asyncio
import json

import httpx
import pytest
from sqlalchemy import select
from test_id_generation_safety import fixture_db

from eic import main
from eic.ai.mcp_client import HttpMcpClient, MCP_PROTOCOL_VERSION
from eic.models import Resource, ToolAudit


@pytest.mark.parametrize('fault', ['none', 'unadvertised', 'schema', 'cursor', 'revoked'])
def test_mcp_service_initializes_lists_and_checks_before_call(monkeypatch, tmp_path, fault):
    methods = []

    def handler(request):
        body = json.loads(request.content)
        method = body['method']
        methods.append(method)
        if method == 'notifications/initialized':
            return httpx.Response(202)
        if method == 'initialize':
            result = {'protocolVersion': MCP_PROTOCOL_VERSION, 'capabilities': {},
                      'serverInfo': {'name': 'fixture', 'version': '1'}}
        elif method == 'tools/list':
            result = {'tools': [{'name': 'other' if fault == 'unadvertised' else 'lookup',
                                'inputSchema': {'type': 'object', 'properties': {'q': {'type': 'string'}},
                                                'required': ['q']}}]}
            if fault == 'cursor':
                result['nextCursor'] = 'repeated'
        else:
            assert method == 'tools/call'
            result = {'content': [{'type': 'text', 'text': 'safe-answer'}]}
        return httpx.Response(200, json={'jsonrpc': '2.0', 'id': body['id'], 'result': result})

    async def run():
        async with fixture_db(monkeypatch, tmp_path, is_super_admin=True) as sessions:
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as transport:
                monkeypatch.setattr(main, 'HttpMcpClient', lambda config, **kwargs: HttpMcpClient(config, client=transport, **kwargs))
                async with sessions() as session:
                    session.add(Resource(id=602, tenant_id=1, owner_id=11, resource_type='mcps',
                                         data={'transport': 'http', 'endpoint': 'https://mcp.fixture/rpc'}))
                    await session.commit()
                    original = main.ensure_permission
                    checks = 0

                    async def permission(code, principal, current_session):
                        nonlocal checks
                        checks += 1
                        if fault == 'revoked' and checks == 2:
                            raise main.HTTPException(403, detail='revoked')
                        await original(code, principal, current_session)

                    monkeypatch.setattr(main, 'ensure_permission', permission)
                    payload = {'mcpId': 602, 'name': 'lookup', 'arguments': {'q': 1 if fault == 'schema' else 'safe'}}
                    if fault == 'none':
                        result = await main.mcp_call(payload, main.Principal(11, 1, 'fixture', True), session)
                        assert result['content'][0]['text'] == 'safe-answer'
                        assert methods == ['initialize', 'notifications/initialized', 'tools/list', 'tools/call']
                        assert (await session.scalar(select(ToolAudit))).outcome == 'success'
                    else:
                        with pytest.raises(main.HTTPException):
                            await main.mcp_call(payload, main.Principal(11, 1, 'fixture', True), session)
                        assert 'tools/call' not in methods
    asyncio.run(run())


@pytest.mark.parametrize('key', ['apiKey', 'API_KEY', 'api-key', 'access_token', 'accessToken',
                                'ACCESS-TOKEN', 'refresh_token', 'client-secret', 'worker_token'])
def test_log_redaction_preserves_safe_data_and_input(key):
    value = {'nested': [{key: 'SYNTHETIC_NEVER_LOG', 'token_count': 8}], 'totalTokens': 12}
    result = main.redact_for_log(value)
    assert 'SYNTHETIC_NEVER_LOG' not in json.dumps(result)
    assert value['nested'][0][key] == 'SYNTHETIC_NEVER_LOG'
    assert result['nested'][0]['token_count'] == 8 and result['totalTokens'] == 12
