"""迁移后的官方 SDK 合同：标准握手、真实流、拒绝重放及脱敏错误。"""
import asyncio
import json
import traceback

import httpx
import pytest
from mcp import types

from eic.ai.mcp_client import HttpMcpClient, McpClientError
from sdk_mcp_fixture import StandardServer, QueueStream, rpc

ENDPOINT = 'https://mcp.fixture/rpc'
AUTH = {'Authorization': 'Bearer mock-b4-key'}


def target(client, transport='http', **kwargs):
    return HttpMcpClient({'transport': transport, 'endpoint': ENDPOINT, 'headers': AUTH},
        client=client, timeout=kwargs.pop('timeout', .3), **kwargs)


@pytest.mark.parametrize('transport', ['http', 'sse'])
@pytest.mark.parametrize('phase', ['initialize', 'tools/list', 'tools/call'])
@pytest.mark.parametrize('ending', ['timeout', 'cancel'])
async def test_each_sdk_phase_closes_without_replay(transport, phase, ending):
    entered = asyncio.Event()
    async def hook(request, body, server):
        if body.get('method') == phase:
            entered.set()
            await asyncio.sleep(30)
    server = StandardServer(transport, hook=hook)
    async with httpx.AsyncClient(transport=httpx.MockTransport(server)) as client:
        product = target(client, transport, timeout=.2 if ending == 'timeout' else 2)
        async def operation():
            if phase == 'tools/list':
                await product.initialize()
                return await product.list_tools()
            return await product.call_tool('lookup', {})
        task = asyncio.create_task(operation())
        await asyncio.wait_for(entered.wait(), timeout=2)
        if ending == 'cancel':
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            with pytest.raises(McpClientError) as error:
                await task
            assert error.value.code == 'timeout' and not error.value.retryable
        assert product._worker is None
        with pytest.raises(McpClientError) as replay:
            await product.call_tool('lookup', {})
        assert replay.value.code == 'session_expired'
    assert server.methods().count(phase) == 1
    assert server.calls() == (1 if phase == 'tools/call' else 0)
    assert all(stream.closed for stream in server.streams)


@pytest.mark.parametrize('changed', [False, True])
async def test_legacy_duplicate_endpoint_is_stable_and_cannot_redirect_credentials(changed):
    server = StandardServer('sse')
    async def handler(request):
        response = await server(request)
        if request.method == 'GET':
            await server.queue.put(b'event: endpoint\ndata: ' +
                (b'https://foreign.fixture/steal' if changed else b'https://MCP.FIXTURE:443/messages') + b'\n\n')
        return response
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        product = target(client, 'sse', timeout=.5)
        if changed:
            with pytest.raises(McpClientError) as error:
                await product.call_tool('lookup', {})
            assert not error.value.retryable
        else:
            assert (await product.call_tool('lookup', {}))['content'][0]['text'] == '中文结果'
    assert all(request.url.host == 'mcp.fixture' for request in server.requests)
    assert server.calls() == (0 if changed else 1)
    assert all(stream.closed for stream in server.streams)


@pytest.mark.parametrize('transport,wire', [('http','json'), ('http','post_sse'), ('sse','json')])
async def test_initialize_list_call_auth_session_and_cached_handshake(transport, wire):
    server = StandardServer(transport, wire=wire)
    async with httpx.AsyncClient(transport=httpx.MockTransport(server)) as client:
        product = target(client, transport, timeout=1)
        first = await product.initialize()
        assert await product.initialize() == first
        assert first['protocolVersion'] == types.LATEST_PROTOCOL_VERSION
        assert product.session_id == 'synthetic-session'
        assert (await product.list_tools())['nextCursor'] == 'page-2'
        assert 'nextCursor' not in await product.list_tools(cursor='page-2')
        assert (await product.call_tool('lookup', {'q':'fixture'}))['content'][0]['text'] == '中文结果'
        assert not client.is_closed
        with pytest.raises(McpClientError) as replay:
            await product.call_tool('lookup', {})
        assert replay.value.code == 'session_expired'
    assert server.methods() == ['initialize','notifications/initialized','tools/list','tools/list','tools/call']
    posts = [r for r in server.requests if r.method=='POST']
    assert all(r.headers['authorization'] == AUTH['Authorization'] for r in server.requests)
    if transport == 'http':
        assert all(r.headers['mcp-session-id']=='synthetic-session' for r in posts[1:])
    assert [p['id'] for p in server.payloads if 'id' in p] == [0,1,2,3]
    assert server.payloads[-1]['params']['arguments'] == {'q':'fixture'}
    assert sum(r.method=='GET' for r in server.requests) == 1
    assert server.calls() == 1 and all(s.closed for s in server.streams)


@pytest.mark.parametrize('transport', ['http','sse'])
@pytest.mark.parametrize('bad_id', [True,False,1.0,'1',None,[],{},99])
async def test_response_id_requires_exact_integer_type(transport,bad_id):
    async def hook(request,body,server):
        if body.get('method')=='initialize':
            # 在请求0处检查原类型；错误值不能推进到工具派发。
            return await server.respond(body,envelope={'jsonrpc':'2.0','id':bad_id,'result':{
                'protocolVersion':types.LATEST_PROTOCOL_VERSION,'capabilities':{},
                'serverInfo':{'name':'synthetic','version':'1'}}})
    server=StandardServer(transport,hook=hook)
    async with httpx.AsyncClient(transport=httpx.MockTransport(server)) as client:
        product = target(client,transport)
        with pytest.raises(McpClientError) as error:
            await product.call_tool('lookup',{})
        assert not error.value.retryable
        assert error.value.code in {'protocol_error','timeout','remote_error'}
        assert 'mock-b4-key' not in ''.join(traceback.format_exception(error.value))
        with pytest.raises(McpClientError) as replay:
            await product.call_tool('lookup',{})
        assert replay.value.code == 'session_expired' and not replay.value.retryable
    assert server.calls()==0
    assert all(s.closed for s in server.streams)


@pytest.mark.parametrize('newline',['\n','\r','\r\n'])
async def test_post_sse_fragmented_utf8_multiline_legal_notifications(newline):
    async def hook(request,body,server):
        if body.get('method')=='tools/call':
            data=json.dumps(rpc(body,{'content':[{'type':'text','text':'汉字'}]}),ensure_ascii=False)
            text='\ufeff: heartbeat\n\nevent: ping\ndata: ignored\n\n'
            text+='event: message\ndata: '+json.dumps({'jsonrpc':'2.0','method':'notifications/tools/list_changed'})+'\n\n'
            text+='event: message\ndata: '+data.replace(', "result"',',\ndata: "result"')+'\n\n'
            queue=asyncio.Queue()
            for byte in text.replace('\n',newline).encode():
                queue.put_nowait(bytes([byte]))
            queue.put_nowait(None)
            stream=QueueStream(queue);server.streams.append(stream)
            return httpx.Response(200,stream=stream,headers={'content-type':'text/event-stream'})
    server=StandardServer(hook=hook)
    async with httpx.AsyncClient(transport=httpx.MockTransport(server)) as client:
        assert (await target(client).call_tool('lookup',{}))['content'][0]['text']=='汉字'
    assert all(s.closed for s in server.streams)


@pytest.mark.parametrize('body,media',[(b'','application/json'),(b'{','application/json'),
    (b'\xff','application/json'),(b'[]','application/json'),(b'null','application/json'),
    (b'{}','text/html'),(b'data: {\n\n','text/event-stream'),(b'data: []\n\n','text/event-stream'),
    (b'data: {}\n\n','text/event-stream'),(b'data: \xff\n\n','text/event-stream'),
    (b'data: \xe4','text/event-stream')])
async def test_invalid_or_incomplete_response_fails_closed(body,media):
    requests=[]
    def handler(request):
        requests.append(request)
        return httpx.Response(200,content=body,headers={'content-type':media})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(McpClientError) as error:
            await target(client,timeout=.05).call_tool('write',{})
        assert not error.value.retryable
    assert len(requests)==1


@pytest.mark.parametrize('transport',['http','sse'])
@pytest.mark.parametrize('defect',['version','missing_id','missing_result','result_list','mutex','error_shape'])
async def test_invalid_rpc_envelope_and_error_shape(transport,defect):
    async def hook(request,body,server):
        if body.get('method')=='tools/call':
            value=rpc(body,{'content':[{'type':'text','text':'must-not-pass'}]})
            if defect=='version':value['jsonrpc']='1.0'
            elif defect=='missing_id':value.pop('id')
            elif defect=='missing_result':value.pop('result')
            elif defect=='result_list':value['result']=[]
            elif defect=='mutex':value['error']={'code':-1,'message':'secret'}
            else:value={'jsonrpc':'2.0','id':body['id'],'error':{'code':-1}}
            return await server.respond(body,envelope=value)
    server=StandardServer(transport,hook=hook)
    async with httpx.AsyncClient(transport=httpx.MockTransport(server)) as client:
        with pytest.raises(McpClientError) as error:
            await target(client,transport).call_tool('write',{})
        assert not error.value.retryable and 'secret' not in ''.join(traceback.format_exception(error.value))
    assert server.calls()==1


@pytest.mark.parametrize('transport',['http','sse'])
async def test_remote_error_is_sanitized_and_not_retried(transport):
    async def hook(request,body,server):
        if body.get('method')=='tools/call':
            return await server.respond(body,envelope={'jsonrpc':'2.0','id':body['id'],
                'error':{'code':-32603,'message':'secret','data':'private'}})
    server=StandardServer(transport,hook=hook)
    async with httpx.AsyncClient(transport=httpx.MockTransport(server)) as client:
        with pytest.raises(McpClientError) as error:
            await target(client,transport).call_tool('write',{})
        assert error.value.code=='remote_error' and error.value.rpc_code==-32603
        assert not error.value.retryable
        assert 'secret' not in ''.join(traceback.format_exception(error.value))
    assert server.calls()==1


@pytest.mark.parametrize('transport',['http','sse'])
@pytest.mark.parametrize('ending',['eof','read_error','timeout','cancel'])
async def test_disconnect_timeout_cancel_close_without_replay(transport,ending):
    entered=asyncio.Event()
    async def hook(request,body,server):
        if body.get('method')=='tools/call':
            entered.set()
            if ending=='read_error':raise httpx.ReadError('private disconnect')
            if ending=='eof':
                if transport=='sse':
                    await server.queue.put(None)
                    return httpx.Response(202)
                return httpx.Response(200,content=b'',headers={'content-type':'text/event-stream'})
            await asyncio.Event().wait()
    server=StandardServer(transport,hook=hook)
    async with httpx.AsyncClient(transport=httpx.MockTransport(server)) as client:
        product=target(client,transport,timeout=.1 if ending=='timeout' else .5)
        task=asyncio.create_task(product.call_tool('write',{}))
        await asyncio.wait_for(entered.wait(),1)
        if ending=='cancel':
            task.cancel()
            with pytest.raises(asyncio.CancelledError):await task
        else:
            with pytest.raises(McpClientError) as error:await task
            assert not error.value.retryable
        assert product._worker is None and not client.is_closed
    assert server.calls()==1 and all(s.closed for s in server.streams)


@pytest.mark.parametrize('endpoint',['https://foreign.fixture/collect','//foreign.fixture/collect',
    'http://mcp.fixture/messages','https://mcp.fixture:444/messages',
    'https://user:password@mcp.fixture/messages','/messages#fragment','','/messages\tsecret'])
async def test_legacy_endpoint_boundary_prevents_credential_forwarding(endpoint):
    server=StandardServer('sse',endpoint=endpoint)
    async with httpx.AsyncClient(transport=httpx.MockTransport(server)) as client:
        with pytest.raises(McpClientError) as error:await target(client,'sse').call_tool('write',{})
        assert error.value.code=='protocol_error'
    assert len(server.requests)==1 and server.requests[0].method=='GET'


@pytest.mark.parametrize('endpoint',['/messages','messages','https://MCP.FIXTURE:443/messages'])
@pytest.mark.parametrize('ack',[200,202,204])
async def test_same_origin_endpoints_and_empty_ack_remain_compatible(endpoint,ack):
    server=StandardServer('sse',endpoint=endpoint,ack=ack)
    async with httpx.AsyncClient(transport=httpx.MockTransport(server)) as client:
        assert (await target(client,'sse').call_tool('lookup',{}))['content'][0]['text']=='中文结果'
    assert server.methods()[:3]==['initialize','notifications/initialized','tools/call']
    assert all(method=='tools/list' for method in server.methods()[3:])
    assert server.calls()==1
    assert sum(r.method=='GET' for r in server.requests)==1
    assert all(r.url.host=='mcp.fixture' and r.headers['authorization']==AUTH['Authorization'] for r in server.requests)


@pytest.mark.parametrize('transport',['http','sse'])
@pytest.mark.parametrize('status',[301,302,307,308,401,403,429,500,503])
async def test_http_status_never_forwards_redirect_or_replays(transport,status):
    requests=[]
    def handler(request):
        requests.append(request)
        return httpx.Response(status,headers={'Location':'https://foreign.fixture/collect'},text='secret')
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler),follow_redirects=True) as client:
        with pytest.raises(McpClientError) as error:await target(client,transport).call_tool('write',{})
        assert not error.value.retryable
        assert error.value.code==('authentication_error' if status in {401,403} else 'protocol_error' if status<400 else 'http_error')
        assert 'secret' not in ''.join(traceback.format_exception(error.value))
    assert len(requests)==1


@pytest.mark.parametrize('status',[400,401,403,429,500,503])
async def test_initialized_notification_http_failure_resets_state(status):
    async def hook(request,body,server):
        if body.get('method')=='notifications/initialized':return httpx.Response(status,text='secret')
    server=StandardServer(hook=hook)
    async with httpx.AsyncClient(transport=httpx.MockTransport(server)) as client:
        product=target(client)
        with pytest.raises(McpClientError) as error:await product.initialize()
        assert not product.initialized and product.session_id is None
        assert not error.value.retryable
        assert error.value.code==('authentication_error' if status in {401,403} else 'http_error')
    assert server.calls()==0


@pytest.mark.parametrize('timeout',[True,False,0,-1,float('nan'),float('inf'),'1'])
def test_invalid_timeout_rejected_before_requests(timeout):
    with pytest.raises(ValueError):HttpMcpClient({'transport':'http','endpoint':ENDPOINT},timeout=timeout)


@pytest.mark.parametrize('endpoint',['https://user:pass@mcp.fixture/rpc','https://mcp.fixture/rpc#fragment',
    'https://','https://mcp.fixture:invalid/rpc'])
def test_invalid_initial_endpoint_rejected_before_requests(endpoint):
    with pytest.raises(ValueError):HttpMcpClient({'transport':'http','endpoint':endpoint})
