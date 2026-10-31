"""真实官方 SDK transport/session 的离线 fixture，不替换 ClientSession 方法。"""
import asyncio
import json
import traceback
import logging
import os
import sys
from pathlib import Path

import httpx
import pytest
from eic.ai.mcp_client import HttpMcpClient, McpClientError


class Server:
    def __init__(self, mode='json'):
        self.mode, self.calls = mode, []

    async def __call__(self, request):
        payload = json.loads(request.content) if request.content else {}
        self.calls.append((request.method, str(request.url), payload))
        method = payload.get('method')
        if request.method == 'GET':
            return httpx.Response(405)
        if method in {'notifications/initialized', 'notifications/cancelled'}:
            return httpx.Response(202)
        if self.mode == 'redirect':
            return httpx.Response(307, headers={'Location': 'https://other.invalid/steal'})
        if method == 'initialize':
            result = {'protocolVersion': '2025-03-26', 'capabilities': {}, 'serverInfo': {'name': 'synthetic', 'version': '1'}}
        elif method == 'tools/list':
            cursor = payload.get('params', {}).get('cursor')
            result = {'tools': [{'name': 'lookup', 'inputSchema': {'type': 'object'}}]}
            if cursor is None:
                result['nextCursor'] = 'page-2'
        elif method == 'tools/call':
            if self.mode in {'timeout', 'cancel'}:
                await asyncio.sleep(30)
            if self.mode == 'rpc-error':
                return httpx.Response(200, json={'jsonrpc': '2.0', 'id': payload['id'], 'error': {'code': -32602, 'message': 'secret-token', 'data': 'secret-data'}})
            if self.mode == 'disconnect':
                raise httpx.ReadError('secret-token')
            result = {'content': [{'type': 'text', 'text': 'synthetic'}], 'isError': False}
        else:
            return httpx.Response(405)
        envelope = {'jsonrpc': '2.0', 'id': payload['id'], 'result': result}
        if self.mode == 'post-sse':
            return httpx.Response(200, headers={'Content-Type': 'text/event-stream'}, content='event: message\ndata: ' + json.dumps(envelope) + '\n\n')
        return httpx.Response(200, json=envelope, headers={'Mcp-Session-Id': 'synthetic-session'})


@pytest.mark.parametrize('mode', ['json', 'post-sse'])
def test_product_real_sdk_initialize_pagination_call(mode):
    server = Server(mode)
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(server)) as client:
            product = HttpMcpClient({'transport': 'http', 'endpoint': 'https://mcp.invalid/rpc', 'headers': {'Authorization': 'Bearer synthetic-only'}}, client=client, timeout=1)
            assert (await product.initialize())['protocolVersion'] == '2025-03-26'
            assert (await product.list_tools())['nextCursor'] == 'page-2'
            assert 'nextCursor' not in await product.list_tools(cursor='page-2')
            assert (await product.call_tool('lookup', {}))['content'][0]['text'] == 'synthetic'
            with pytest.raises(McpClientError) as info:
                await product.call_tool('lookup', {})
            assert info.value.code == 'session_expired'
    asyncio.run(run())
    assert sum(p.get('method') == 'tools/call' for _, _, p in server.calls) == 1
    assert sum(p.get('method') == 'initialize' for _, _, p in server.calls) == 1


@pytest.mark.parametrize('mode', ['rpc-error', 'disconnect', 'timeout', 'redirect'])
def test_errors_sanitized_no_retry(mode):
    server = Server(mode)
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(server)) as client:
            product = HttpMcpClient({'transport': 'http', 'endpoint': 'https://mcp.invalid/rpc'}, client=client, timeout=.2)
            with pytest.raises(McpClientError) as info:
                await product.call_tool('lookup', {})
            assert not info.value.retryable
            assert 'secret-token' not in ''.join(traceback.format_exception(info.value))
            if mode == 'rpc-error': assert info.value.rpc_code == -32602
            if mode == 'timeout': assert info.value.code == 'timeout'
    asyncio.run(run())
    assert sum(p.get('method') == 'tools/call' for _, _, p in server.calls) <= 1


def test_cancel_propagates_and_no_orphan_task():
    server = Server('cancel')
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(server)) as client:
            product = HttpMcpClient({'transport': 'http', 'endpoint': 'https://mcp.invalid/rpc'}, client=client, timeout=2)
            task = asyncio.create_task(product.call_tool('lookup', {}))
            for _ in range(100):
                if any(p.get('method') == 'tools/call' for _, _, p in server.calls): break
                await asyncio.sleep(.005)
            task.cancel()
            with pytest.raises(asyncio.CancelledError): await task
            await asyncio.sleep(0)
            assert len([t for t in asyncio.all_tasks() if t is not asyncio.current_task()]) == 0
    asyncio.run(run())


@pytest.mark.parametrize('endpoint', ['https://u:p@mcp.invalid/rpc', 'https://mcp.invalid/rpc#secret'])
def test_invalid_endpoint_rejected(endpoint):
    with pytest.raises(ValueError): HttpMcpClient({'transport': 'http', 'endpoint': endpoint})


class QueueStream(httpx.AsyncByteStream):
    def __init__(self, queue):
        self.queue, self.closed = queue, False

    async def __aiter__(self):
        while True:
            yield await self.queue.get()

    async def aclose(self):
        self.closed = True


@pytest.mark.parametrize('transport_name', ['http', 'sse'])
@pytest.mark.parametrize('result_value', [{}, {'content': [{'type': 'text', 'text': 'must-not-publish'}]}])
def test_sdk_typed_extra_rejects_result_error_mutex(transport_name, result_value):
    from mcp import types
    # 两种官方union选择均保留冲突字段，不依赖字段顺序或重写parser。
    raw = {'jsonrpc': '2.0', 'id': 0, 'result': result_value, 'error': {'code': -1, 'message': 'secret'}}
    root = types.JSONRPCMessage.model_validate(raw).root
    assert 'result' in (root.model_extra or {}) or 'error' in (root.model_extra or {})
    calls, streams, published = [], [], []
    async def run():
        queue = asyncio.Queue()
        server = Server()
        async def handler(request):
            if request.method == 'GET':
                if transport_name == 'http':
                    return httpx.Response(405)
                await queue.put(b'event: endpoint\ndata: /messages\n\n')
                stream = QueueStream(queue)
                streams.append(stream)
                return httpx.Response(200, headers={'content-type': 'text/event-stream'}, stream=stream)
            body = json.loads(request.content)
            calls.append(body)
            if body['method'].startswith('notifications/'):
                return httpx.Response(202)
            if body['method'] == 'tools/call':
                value = dict(raw, id=body['id'])
            else:
                response = await server(request)
                value = json.loads(response.content)
            if transport_name == 'http':
                return httpx.Response(200, json=value)
            await queue.put(('event: message\ndata: ' + json.dumps(value) + '\n\n').encode())
            return httpx.Response(202)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            target = HttpMcpClient({'transport': transport_name, 'endpoint': 'https://mcp.invalid/rpc'}, client=client, timeout=.5)
            with pytest.raises(McpClientError) as error:
                published.append(await target.call_tool('lookup', {}))
            assert error.value.code == 'protocol_error'
            assert error.value.rpc_code is None
            assert error.value.retryable is False
            assert 'secret' not in ''.join(traceback.format_exception(error.value))
            with pytest.raises(McpClientError) as replay:
                await target.call_tool('lookup', {})
            assert replay.value.code == 'session_expired'
            assert replay.value.retryable is False
            await target.aclose()
    asyncio.run(run())
    assert published == []
    assert sum(p['method'] == 'initialize' for p in calls) == 1
    assert sum(p['method'] == 'tools/call' for p in calls) == 1
    assert all(s.closed for s in streams)


@pytest.mark.parametrize('transport_name', ['http', 'sse'])
@pytest.mark.parametrize('message_kind', ['wrong_integer', 'wrong_string', 'wrong_error', 'notification', 'server_request', 'unsolicited'])
def test_sdk_response_id_fail_closed_and_legal_message_boundaries(transport_name, message_kind):
    calls, streams, published = [], [], []
    async def run():
        queue = asyncio.Queue()
        async def handler(request):
            if request.method == 'GET':
                if transport_name == 'http':
                    return httpx.Response(405)
                await queue.put(b'event: endpoint\ndata: /messages\n\n')
                stream = QueueStream(queue)
                streams.append(stream)
                return httpx.Response(200, headers={'content-type': 'text/event-stream'}, stream=stream)
            body = json.loads(request.content)
            calls.append(body)
            method = body.get('method')
            if method is None or method.startswith('notifications/'):
                return httpx.Response(202)
            if method == 'initialize':
                result = {'protocolVersion': '2025-03-26', 'capabilities': {}, 'serverInfo': {'name': 'synthetic', 'version': '1'}}
            elif method == 'tools/list':
                result = {'tools': []}
            else:
                result = {'content': [{'type': 'text', 'text': 'correct-after-wrong'}]}
            correct = {'jsonrpc': '2.0', 'id': body['id'], 'result': result}
            messages = [correct]
            if method == 'tools/call':
                wrong = str(body['id']) if message_kind == 'wrong_string' else body['id'] + 77
                first = {'jsonrpc': '2.0', 'id': wrong, 'result': {'content': [{'type': 'text', 'text': 'must-not-publish'}]}}
                if message_kind == 'wrong_error':
                    first = {'jsonrpc': '2.0', 'id': wrong, 'error': {'code': -1, 'message': 'secret'}}
                elif message_kind == 'notification':
                    first = {'jsonrpc': '2.0', 'method': 'notifications/tools/list_changed'}
                elif message_kind == 'server_request':
                    first = {'jsonrpc': '2.0', 'id': 'server-legal-id', 'method': 'ping'}
                messages = [first, correct]
            if method == 'initialize' and message_kind == 'unsolicited':
                unsolicited = {'jsonrpc': '2.0', 'id': 9876, 'result': {}}
                if transport_name == 'http':
                    messages.insert(0, unsolicited)
                else:
                    messages.append(unsolicited)
            frames = b''.join(('event: message\ndata: ' + json.dumps(m) + '\n\n').encode() for m in messages)
            if transport_name == 'sse':
                await queue.put(frames)
                return httpx.Response(202)
            return httpx.Response(200, headers={'content-type': 'text/event-stream'}, content=frames)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            product = HttpMcpClient({'transport': transport_name, 'endpoint': 'https://mcp.invalid/rpc'}, client=client, timeout=.5)
            if message_kind in {'notification', 'server_request'}:
                published.append(await product.call_tool('lookup', {}))
                assert published[0]['content'][0]['text'] == 'correct-after-wrong'
                if transport_name == 'sse' and message_kind == 'server_request':
                    assert any(p.get('id') == 'server-legal-id' and p.get('result') == {} for p in calls)
            else:
                with pytest.raises(McpClientError) as error:
                    published.append(await product.call_tool('lookup', {}))
                assert error.value.code == 'protocol_error'
                assert error.value.retryable is False
                assert published == []
                with pytest.raises(McpClientError):
                    await product.call_tool('lookup', {})
            await product.aclose()
            # 官方HTTP SSE异步生成器的finally在下一轮事件循环完成。
            for _ in range(10):
                if not [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]:
                    break
                await asyncio.sleep(0)
            assert not [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
    asyncio.run(run())
    count = sum(p.get('method') == 'tools/call' for p in calls)
    assert count == (0 if message_kind == 'unsolicited' else 1)
    assert sum(p.get('method') == 'initialize' for p in calls) == 1
    assert all(s.closed for s in streams)


def test_sdk_idle_sse_unsolicited_response_rejects_cached_handshake_and_dispatch():
    calls, streams = [], []
    async def run():
        queue = asyncio.Queue()
        async def handler(request):
            if request.method == 'GET':
                await queue.put(b'event: endpoint\ndata: /messages\n\n')
                stream = QueueStream(queue)
                streams.append(stream)
                return httpx.Response(200, stream=stream, headers={'content-type': 'text/event-stream'})
            body = json.loads(request.content)
            calls.append(body)
            if body['method'] == 'notifications/initialized':
                return httpx.Response(202)
            assert body['method'] == 'initialize'
            result = {'protocolVersion': '2025-03-26', 'capabilities': {}, 'serverInfo': {'name': 'synthetic', 'version': '1'}}
            await queue.put(('event: message\ndata: ' + json.dumps({'jsonrpc': '2.0', 'id': body['id'], 'result': result}) + '\n\n').encode())
            return httpx.Response(202)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            product = HttpMcpClient({'transport': 'sse', 'endpoint': 'https://mcp.invalid/rpc'}, client=client, timeout=1)
            await product.initialize()
            await queue.put(b'event: message\ndata: {"jsonrpc":"2.0","id":9876,"result":{}}\n\n')
            for _ in range(100):
                if product._boundary.response_id_failed:
                    break
                await asyncio.sleep(.001)
            assert product._boundary.response_id_failed
            for operation in [product.initialize, lambda: product.call_tool('lookup', {})]:
                with pytest.raises(McpClientError) as error:
                    await operation()
                assert error.value.code == 'protocol_error'
                assert error.value.retryable is False
            await product.aclose()
    asyncio.run(run())
    assert [p['method'] for p in calls] == ['initialize', 'notifications/initialized']
    assert all(s.closed for s in streams)


@pytest.mark.parametrize('scheme,port', [('http', 80), ('https', 443), ('http', 8080), ('https', 8443)])
@pytest.mark.parametrize('transport_name', ['http', 'sse'])
@pytest.mark.parametrize('foreign', [False, 'host', 'port'])
def test_ipv6_sdk_origin_and_log_boundary(scheme, port, transport_name, foreign, caplog):
    from eic.ai.mcp_sdk_adapter import canonical_endpoint, origin
    endpoint = f'{scheme}://[::1]:{port}/rpc?token=ipv6-secret'
    foreign_endpoint = f'{scheme}://[::2]:{port}/steal' if foreign == 'host' else f'{scheme}://[::1]:{port + 1}/steal'
    canonical = canonical_endpoint(endpoint)
    assert origin(canonical) == (scheme, '::1', port)
    assert canonical.startswith(f'{scheme}://[::1]' + (f':{port}' if port not in (80, 443) else '/'))
    calls, streams = [], []
    server = Server()
    caplog.set_level(logging.DEBUG, logger='httpx')
    async def run():
        queue = asyncio.Queue()
        async def handler(request):
            calls.append(request)
            assert request.url.host == '::1'
            assert origin(request.url) == origin(endpoint)
            assert request.headers['authorization'] == 'Bearer ipv6-synthetic'
            if transport_name == 'http':
                if foreign:
                    return httpx.Response(307, headers={'Location': foreign_endpoint})
                return await server(request)
            if request.method == 'GET':
                target = foreign_endpoint if foreign else f'{scheme}://[::1]:{port}/messages?token=ipv6-secret'
                await queue.put(('event: endpoint\ndata: ' + target + '\n\n').encode())
                stream = QueueStream(queue)
                streams.append(stream)
                return httpx.Response(200, headers={'Content-Type': 'text/event-stream'}, stream=stream)
            body = json.loads(request.content)
            if body['method'] == 'notifications/initialized':
                return httpx.Response(202)
            if body['method'] == 'initialize':
                result = {'protocolVersion': '2025-03-26', 'capabilities': {}, 'serverInfo': {'name': 'synthetic', 'version': '1'}}
            elif body['method'] == 'tools/list':
                result = {'tools': []}
            else:
                result = {'content': [{'type': 'text', 'text': 'ipv6-ok'}]}
            await queue.put(('event: message\ndata: ' + json.dumps({'jsonrpc': '2.0', 'id': body['id'], 'result': result}) + '\n\n').encode())
            return httpx.Response(202)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            product = HttpMcpClient({'transport': transport_name, 'endpoint': endpoint, 'headers': {'Authorization': 'Bearer ipv6-synthetic'}}, client=client, timeout=1)
            if foreign:
                with pytest.raises(McpClientError):
                    await product.initialize()
            else:
                await product.initialize()
                await product.list_tools()
                result = await product.call_tool('lookup', {})
                assert result['content'][0]['text'] == ('synthetic' if transport_name == 'http' else 'ipv6-ok')
    asyncio.run(run())
    payloads = [json.loads(r.content) for r in calls if r.content]
    assert sum(p.get('method') == 'tools/call' for p in payloads) == (0 if foreign else 1)
    if foreign:
        assert len(calls) == 1
    assert all(s.closed for s in streams)
    records = [r for r in caplog.records if r.name == 'httpx']
    assert records
    assert all(r.getMessage() == 'MCP SDK event (details omitted)' for r in records)
    assert 'ipv6-secret' not in caplog.text


@pytest.mark.parametrize('foreign', [False, True])
@pytest.mark.parametrize('ack', [200, 202, 204])
@pytest.mark.parametrize('endpoint_form', ['/messages', 'messages', 'https://MCP.INVALID:443/messages'])
def test_sdk_sse_transport_real_stream_and_foreign_endpoint(foreign, ack, endpoint_form):
    streams, calls = [], []
    async def run():
        queue = None
        async def handler(request):
            nonlocal queue
            calls.append((request.method, str(request.url), dict(request.headers)))
            if request.method == 'GET':
                queue = asyncio.Queue()
                endpoint = 'https://evil.invalid/messages' if foreign else endpoint_form
                await queue.put(('event: endpoint\ndata: ' + endpoint + '\n\n').encode())
                stream = QueueStream(queue)
                streams.append(stream)
                return httpx.Response(200, headers={'Content-Type': 'text/event-stream'}, stream=stream)
            body = json.loads(request.content)
            if body['method'] == 'notifications/initialized': return httpx.Response(ack)
            if body['method'] == 'initialize':
                result = {'protocolVersion': '2025-03-26', 'capabilities': {}, 'serverInfo': {'name': 'synthetic', 'version': '1'}}
            elif body['method'] == 'tools/list': result = {'tools': []}
            else: result = {'content': [{'type': 'text', 'text': 'sse-ok'}]}
            await queue.put(('event: message\ndata: ' + json.dumps({'jsonrpc': '2.0', 'id': body['id'], 'result': result}) + '\n\n').encode())
            return httpx.Response(ack)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as transport:
            product = HttpMcpClient({'transport': 'sse', 'endpoint': 'https://mcp.invalid/sse', 'headers': {'Authorization': 'Bearer synthetic'}}, client=transport, timeout=.3)
            if foreign:
                with pytest.raises(McpClientError): await product.initialize()
            else:
                await product.initialize()
                assert (await product.list_tools())['tools'] == []
                assert (await product.call_tool('lookup', {}))['content'][0]['text'] == 'sse-ok'
    asyncio.run(run())
    assert all('evil.invalid' not in url for _, url, _ in calls)
    assert all(stream.closed for stream in streams)
    assert all(headers.get('authorization') == 'Bearer synthetic' for _, _, headers in calls)


def test_session_expired_no_reinitialize_and_credentials_isolated():
    calls = []
    async def run():
        server = Server()
        async def handler(request):
            calls.append(request)
            assert 'cookie' not in request.headers
            assert 'x-borrowed-secret' not in request.headers
            assert request.headers['authorization'] == 'Bearer explicit'
            if json.loads(request.content or b'{}').get('method') == 'tools/call':
                return httpx.Response(404)
            return await server(request)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler), headers={'X-Borrowed-Secret': 'hidden'}, cookies={'secret': 'hidden'}, auth=('hidden', 'hidden')) as transport:
            product = HttpMcpClient({'transport': 'http', 'endpoint': 'https://mcp.invalid/rpc', 'headers': {'Authorization': 'Bearer explicit'}}, client=transport, timeout=.5)
            await product.initialize()
            await product.list_tools()
            with pytest.raises(McpClientError) as info:
                await product.call_tool('lookup', {})
            assert info.value.code == 'session_expired'
            assert not info.value.retryable
    asyncio.run(run())
    payloads = [json.loads(r.content or b'{}') for r in calls]
    assert sum(p.get('method') == 'initialize' for p in payloads) == 1
    assert sum(p.get('method') == 'tools/call' for p in payloads) == 1


def test_pre_dispatch_owner_deadline_closes_tasks():
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(Server())) as transport:
            product = HttpMcpClient({'transport': 'http', 'endpoint': 'https://mcp.invalid/rpc'}, client=transport, timeout=.05)
            await product.initialize()
            await asyncio.sleep(.1)
            assert product._worker.done()
            assert len([t for t in asyncio.all_tasks() if t is not asyncio.current_task()]) == 0
    asyncio.run(run())


def test_same_origin_redirect_is_not_followed():
    calls = []
    async def handler(request):
        calls.append(request)
        return httpx.Response(307, headers={'Location': '/another'})
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as transport:
            product = HttpMcpClient({'transport': 'http', 'endpoint': 'https://mcp.invalid/rpc'}, client=transport, timeout=.2)
            with pytest.raises(McpClientError) as info:
                await product.call_tool('lookup', {})
            assert info.value.code == 'protocol_error'
    asyncio.run(run())
    assert len(calls) == 1


@pytest.mark.parametrize('mode', ['rpc-error', 'disconnect', 'timeout'])
def test_uncertain_outcome_cannot_be_replayed_on_same_client(mode):
    server = Server(mode)
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(server)) as transport:
            product = HttpMcpClient({'transport': 'http', 'endpoint': 'https://mcp.invalid/rpc'}, client=transport, timeout=.1)
            with pytest.raises(McpClientError):
                await product.call_tool('lookup', {})
            with pytest.raises(McpClientError) as info:
                await product.call_tool('lookup', {})
            assert info.value.code == 'session_expired'
    asyncio.run(run())
    assert sum(p.get('method') == 'tools/call' for _, _, p in server.calls) == 1


@pytest.mark.parametrize('transport_name', ['http', 'sse'])
def test_sdk_auth_failure_without_retry(transport_name):
    requests = []
    async def handler(request):
        requests.append(request)
        return httpx.Response(401, text='synthetic-secret')
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as transport:
            product = HttpMcpClient({'transport': transport_name, 'endpoint': 'https://mcp.invalid/rpc'}, client=transport, timeout=.1)
            with pytest.raises(McpClientError) as info:
                await product.call_tool('lookup', {})
            assert info.value.code == 'authentication_error'
            assert not info.value.retryable
    asyncio.run(run())
    assert len(requests) == 1


def test_actual_official_sdk_call_chain_and_log_boundary(caplog):
    calls = set()
    def profile(frame, event, arg):
        module = frame.f_globals.get('__name__', '')
        if event == 'call' and module.startswith('mcp.'):
            calls.add(module + ':' + frame.f_code.co_name)
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(Server())) as transport:
            product = HttpMcpClient({'transport': 'http', 'endpoint': 'https://mcp.invalid/rpc?token=synthetic-query-secret'}, client=transport, timeout=.5)
            await product.initialize()
            await product.list_tools()
            await product.call_tool('lookup', {})
    caplog.set_level(logging.DEBUG)
    previous = sys.getprofile()
    sys.setprofile(profile)
    try:
        asyncio.run(run())
    finally:
        sys.setprofile(previous)
    for method in ('initialize', 'list_tools', 'call_tool'):
        assert 'mcp.client.session:' + method in calls
    assert any(value.startswith('mcp.client.streamable_http:') for value in calls)
    assert 'synthetic-query-secret' not in caplog.text
    if os.environ.get('P4_SDK_TRACE'):
        Path(os.environ['P4_SDK_TRACE']).write_text(json.dumps(sorted(calls), indent=2), encoding='utf-8')


def test_precise_legacy_missing_json_media_type_still_uses_sdk():
    server = Server()
    async def handler(request):
        response = await server(request)
        response.headers.pop('content-type', None)
        return response
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as transport:
            product = HttpMcpClient({'transport': 'http', 'endpoint': 'https://mcp.invalid/rpc'}, client=transport, timeout=.5)
            assert (await product.call_tool('lookup', {}))['content'][0]['text'] == 'synthetic'
    asyncio.run(run())
    assert sum(p.get('method') == 'tools/call' for _, _, p in server.calls) == 1


@pytest.mark.parametrize('ending', ['failure', 'cancel'])
def test_real_sdk_cleanup_failure_or_cancel_is_observable_without_replay(ending):
    calls = []
    async def run():
        queue = asyncio.Queue()
        cleaning = asyncio.Event()
        class CleanupStream(QueueStream):
            async def aclose(self):
                self.closed = True
                cleaning.set()
                if ending == 'failure':
                    raise RuntimeError('synthetic-cleanup-secret')
                await asyncio.Event().wait()
        async def handler(request):
            if request.method == 'GET':
                await queue.put(b'event: endpoint\ndata: /messages\n\n')
                return httpx.Response(200, stream=CleanupStream(queue), headers={'content-type': 'text/event-stream'})
            body = json.loads(request.content)
            calls.append(body)
            if body['method'] == 'notifications/initialized':
                return httpx.Response(202)
            result = {'protocolVersion': '2025-03-26', 'capabilities': {}, 'serverInfo': {'name': 'synthetic', 'version': '1'}} if body['method'] == 'initialize' else {'tools': []} if body['method'] == 'tools/list' else {'content': [{'type': 'text', 'text': 'executed'}]}
            await queue.put(('event: message\ndata: ' + json.dumps({'jsonrpc': '2.0', 'id': body['id'], 'result': result}) + '\n\n').encode())
            return httpx.Response(202)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as transport:
            product = HttpMcpClient({'transport': 'sse', 'endpoint': 'https://mcp.invalid/sse'}, client=transport, timeout=.5)
            task = asyncio.create_task(product.call_tool('lookup', {}))
            if ending == 'cancel':
                await asyncio.wait_for(cleaning.wait(), .5)
                assert not task.done()
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            else:
                with pytest.raises(McpClientError) as info:
                    await task
                assert info.value.code == 'outcome_uncertain'
                assert info.value.retryable is False
                assert 'synthetic-cleanup-secret' not in ''.join(traceback.format_exception(info.value))
            with pytest.raises(McpClientError):
                await product.call_tool('lookup', {})
            assert not [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
    asyncio.run(run())
    assert sum(p['method'] == 'tools/call' for p in calls) == 1
