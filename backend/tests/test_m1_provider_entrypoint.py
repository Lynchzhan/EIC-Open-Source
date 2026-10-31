"""公开 provider 入口到真实 ChatOpenAI 的离线契约验证。"""
import asyncio,json
import httpx,pytest
from eic.ai import providers,langchain_stream as adapter
from eic.core.security import encrypt_secret
from eic.models import AIModel

def model(endpoint='https://provider.invalid/custom/v1/chat/completions?api-version=demo&route=local',secret='fixture-key'):
    return AIModel(id=1,tenant_id=1,provider='openai_compatible',model_type='chat',
        endpoint=endpoint,model_name='fixture',secret_encrypted=encrypt_secret(secret) if secret else None,
        max_input_tokens=1000,max_output_tokens=128)

def sse(delta=None,finish=None,usage=None):
    item={'choices':[{'index':0,'delta':delta or {},'finish_reason':finish}]}
    if usage is not None: item['usage']=usage
    return ('data: '+json.dumps(item)+'\n\n').encode()

@pytest.mark.asyncio
async def test_public_empty_tools_text_label_keeps_post_terminal_usage():
    body=sse({'content':'hello'},'tool_calls')+sse(usage={'prompt_tokens':7,'completion_tokens':3})
    usage={};calls=[]
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r:httpx.Response(200,content=body,
        headers={'content-type':'text/event-stream'}))) as client:
        assert [part async for part in providers.stream_chat_completion(model(),'Q',client=client,
            tools=[],usage_sink=usage,tool_call_sink=calls)]==['hello']
    assert calls==[] and usage['input_tokens']==7 and usage['output_tokens']==3

@pytest.mark.asyncio
@pytest.mark.parametrize('strict,tools,choice,delta',[
    (True,[],None,{'content':'hello'}),
    (False,None,None,{'content':'hello'}),
    (False,[],'required',{'content':'hello'}),
    (False,[],'lookup',{'content':'hello'}),
    (False,[],None,{'tool_calls':[{'index':0,'id':'call-1','type':'function',
        'function':{'name':'unknown','arguments':'{}'}}]}),
])
async def test_empty_tools_exception_never_authorizes_tool_deltas_or_strict_mode(strict,tools,choice,delta):
    sink=[]
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r:httpx.Response(200,
        content=sse(delta,'tool_calls'),headers={'content-type':'text/event-stream'}))) as client:
        entry=adapter.stream_chat_completion_langchain if strict else providers.stream_chat_completion
        with pytest.raises(providers.ModelProviderError):
            _=[part async for part in entry(model(),'Q',client=client,tools=tools,
                tool_choice=choice,tool_call_sink=sink)]
    assert sink==[]

@pytest.mark.asyncio
async def test_public_entrypoint_uses_langchain_and_preserves_url_usage_reasoning(monkeypatch):
    calls=[];requests=[]
    original=adapter._EICChatOpenAI._astream
    async def tracked(self,*args,**kwargs):
        calls.append(self.max_retries)
        async for value in original(self,*args,**kwargs): yield value
    monkeypatch.setattr(adapter._EICChatOpenAI,'_astream',tracked)
    def forbidden(*args,**kwargs): raise AssertionError('legacy provider loop used')
    monkeypatch.setattr(providers,'_openai_url',forbidden)
    body=sse({'content':'hello','reasoning_content':'private reasoning'})+sse(finish='stop',usage={
        'prompt_tokens':7,'completion_tokens':3,'prompt_tokens_details':{'cached_tokens':2}})+b'data: [DONE]\n\n'
    def handle(request):
        requests.append(request)
        return httpx.Response(200,content=body,headers={'content-type':'text/event-stream'})
    usage={};reasoning=[]
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        result=[part async for part in providers.stream_chat_completion(model(),'Q',client=client,
            usage_sink=usage,reasoning_sink=reasoning)]
        assert not client.is_closed
    assert result==['hello'] and reasoning==['private reasoning'] and calls==[0]
    assert str(requests[0].url)=='https://provider.invalid/custom/v1/chat/completions?api-version=demo&route=local'
    assert requests[0].headers['authorization']=='Bearer fixture-key'
    assert usage['input_tokens']==7 and usage['output_tokens']==3
    assert usage['cached_tokens']==2

@pytest.mark.asyncio
async def test_first_visible_chunk_precedes_terminal_and_cancellation_closes():
    class Source(httpx.AsyncByteStream):
        closed=False
        async def __aiter__(self):
            yield sse({'content':'hello'})
            await asyncio.Event().wait()
        async def aclose(self): self.closed=True
    source=Source()
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r:httpx.Response(200,
        stream=source,headers={'content-type':'text/event-stream'}))) as client:
        stream=providers.stream_chat_completion(model(),'Q',client=client)
        async with asyncio.timeout(2): assert await anext(stream)=='hello'
        await stream.aclose()
        assert source.closed

@pytest.mark.asyncio
@pytest.mark.parametrize('status,reason,retryable',[(400,'provider_parameters_rejected',False),
    (401,'invalid_provider_credentials',False),(403,'provider_permission_denied',False),
    (429,'provider_rate_limited',True),(500,'provider_outcome_uncertain',False)])
async def test_public_error_has_no_fallback_or_sdk_retry(status,reason,retryable):
    requests=[]
    def handle(request):
        requests.append(request)
        return httpx.Response(status,content=b'PRIVATE-provider-error')
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(providers.ModelProviderError) as error:
            _=[part async for part in providers.stream_chat_completion(model(),'Q',client=client)]
    assert len(requests)==1
    assert error.value.reason==reason and error.value.retryable==retryable
    assert 'PRIVATE' not in str(error.value) and error.value.__suppress_context__

@pytest.mark.asyncio
async def test_done_only_and_anonymous_configuration_are_preserved():
    requests=[];usage={}
    def handle(request):
        requests.append(request)
        return httpx.Response(200,content=sse({'content':'hello'})+b'data: [DONE]\n\n',
            headers={'content-type':'text/event-stream'})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        assert [part async for part in providers.stream_chat_completion(model(secret=''),'Q',client=client,
            usage_sink=usage)]==['hello']
    assert 'authorization' not in requests[0].headers and usage=={}

@pytest.mark.asyncio
async def test_endpoint_duplicate_and_blank_query_are_preserved():
    requests=[]
    def handle(request):
        requests.append(request)
        return httpx.Response(200,content=sse({'content':'hello'},'stop'),
            headers={'content-type':'text/event-stream'})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        _=[part async for part in providers.stream_chat_completion(
            model('https://provider.invalid/v1?route=a&route=b&empty='),'Q',client=client)]
    assert requests[0].url.params.get_list('route')==['a','b']
    assert requests[0].url.params['empty']==''

@pytest.mark.asyncio
@pytest.mark.parametrize('name,profile,temp',[('o1','auto',None),('gpt-5','openai_legacy',.4)])
async def test_sdk_does_not_override_explicit_profile(name,profile,temp):
    requests=[]
    def handle(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200,content=sse({'content':'hello'},'stop'),
            headers={'content-type':'text/event-stream'})
    item=model();item.model_name=name;item.chat_parameter_profile=profile
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        _=[part async for part in providers.stream_chat_completion(item,'Q',client=client,temperature=temp)]
    if temp is None: assert 'temperature' not in requests[0]
    else: assert requests[0]['temperature']==temp and requests[0]['max_tokens']==128

@pytest.mark.asyncio
async def test_public_tools_require_complete_terminal_and_never_fabricate_usage():
    tools=[{'type':'function','function':{'name':'lookup','parameters':{'type':'object','properties':{}}}}]
    delta={'tool_calls':[{'index':0,'id':'call-1','type':'function','function':{'name':'lookup','arguments':'{}'}}]}
    for finish in ('tool_calls',None):
        sink=[];usage={}
        body=sse(delta)+(sse(finish=finish) if finish else b'')+b'data: [DONE]\n\n'
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r:httpx.Response(200,content=body,
            headers={'content-type':'text/event-stream'}))) as client:
            async def collect():
                return [part async for part in providers.stream_chat_completion(model(),'Q',client=client,
                    tools=tools,tool_call_sink=sink,usage_sink=usage)]
            if finish: assert await collect()==[] and sink[0]['id']=='call-1'
            else:
                with pytest.raises(providers.ModelProviderError): await collect()
                assert sink==[]
        assert usage=={}
