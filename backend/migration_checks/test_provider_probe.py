import json
import httpx
import pytest
from eic.ai.providers import probe_model_connection, ModelProviderError
from eic.models import AIModel


def model(provider='openai', endpoint='https://provider.invalid/v1/chat/completions', kind='chat'):
    return AIModel(id=0, tenant_id=1, provider=provider, endpoint=endpoint, model_name='fixture',
        model_type=kind, max_input_tokens=1000, max_output_tokens=128, enabled=True)


def reply():
    return {'id':'probe','object':'chat.completion','created':0,'model':'fixture',
            'choices':[{'index':0,'message':{'role':'assistant','content':'ok'},'finish_reason':'stop'}]}


@pytest.mark.asyncio
@pytest.mark.parametrize('provider', ['openai','1','zhipu','3','dashscope','12','azure','2'])
@pytest.mark.parametrize('secret', ['override',''])
async def test_standard_probe_aliases_borrowed_query_auth(provider,secret):
    seen=[]
    def respond(r):
        seen.append(r); return httpx.Response(200,json=reply())
    m=model(provider,'https://provider.invalid/v1/chat/completions?route=1&tag=a&tag=b')
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as c:
        await probe_model_connection(m,secret,client=c)
        assert not c.is_closed
    assert len(seen)==1 and seen[0].url.path=='/v1/chat/completions'
    assert seen[0].url.params.get_list('tag')==['a','b']
    assert seen[0].headers.get('authorization')==('Bearer override' if secret else None)
    payload=json.loads(seen[0].content)
    assert payload['stream'] is False and payload['max_tokens']==m.max_output_tokens
    assert payload['messages']==[{'role':'user','content':'ping'}]
    assert m.secret_encrypted is None


@pytest.mark.asyncio
async def test_azure_native_deployment_api_version_auth():
    seen=[]
    async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda r:(seen.append(r),httpx.Response(200,json=reply()))[1])) as c:
        await probe_model_connection(model('azure','https://azure.example/openai/deployments/d/chat/completions?api-version=2024-10-21&route=x'),'override',client=c)
    r=seen[0]
    assert r.url.path=='/openai/deployments/d/chat/completions'
    assert r.url.params['api-version']=='2024-10-21' and r.url.params['route']=='x'
    assert r.headers['api-key']=='override'


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['timeout','401','429','500','malformed'])
async def test_probe_errors_no_retry(mode):
    seen=[]
    def respond(r):
        seen.append(r)
        if mode=='timeout': raise httpx.ReadTimeout('secret must not leak',request=r)
        if mode=='malformed': return httpx.Response(200,json={'unexpected':'secret'})
        return httpx.Response(int(mode),json={'error':{'message':'secret'}})
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as c:
        with pytest.raises(ModelProviderError) as exc:
            await probe_model_connection(model(),'override',client=c)
        assert not c.is_closed
    assert len(seen)==1 and 'secret' not in str(exc.value)


@pytest.mark.asyncio
@pytest.mark.parametrize('query', ['', '?route=x'])
async def test_ollama_official_client_probe(query):
    seen=[]
    async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda r:(seen.append(r),httpx.Response(200,json={'model':'fixture','message':{'role':'assistant','content':'ok'},'done':True}))[1])) as c:
        await probe_model_connection(model('ollama','https://ollama.example/api/chat'+query),'override',client=c)
        assert not c.is_closed
    assert seen[0].url.path=='/api/chat'
    assert seen[0].url.params.get('route')==('x' if query else None)
    assert json.loads(seen[0].content)['options']['num_predict']==128


@pytest.mark.asyncio
async def test_embedding_guarded_nonpersistent_override_and_dimensions():
    seen=[]
    m=model(kind='embedding',endpoint='https://provider.invalid/v1'); m.embedding_dimensions=2
    async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda r:(seen.append(r),httpx.Response(200,json={'data':[{'index':0,'embedding':[1.,2.]}]}))[1])) as c:
        await probe_model_connection(m,'override',client=c)
        assert not c.is_closed
    assert seen[0].headers['authorization']=='Bearer override' and m.secret_encrypted is None


@pytest.mark.asyncio
async def test_input_budget_rejects_before_dispatch():
    seen=[]; m=model(); m.max_input_tokens=1
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r:seen.append(r))) as c:
        with pytest.raises(ModelProviderError): await probe_model_connection(m,'',client=c)
    assert not seen


@pytest.mark.asyncio
@pytest.mark.parametrize('body', [{'data':[]}, {'data':[{'index':0,'embedding':[1.]}]},
                               {'data':[{'index':0,'embedding':[float('nan'),2.]}]}])
async def test_embedding_rejects_count_dimension_and_nonfinite(body):
    m=model(kind='embedding',endpoint='https://provider.invalid/v1'); m.embedding_dimensions=2
    # 非有限数值通过原始 JSON 字节构造，避免 httpx 编码层提前拒绝。
    async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda r:httpx.Response(200,content=json.dumps(body).encode()))) as c:
        with pytest.raises(ModelProviderError): await probe_model_connection(m,'override',client=c)
        assert not c.is_closed


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['chat','embedding'])
async def test_dependency_guard_denial_before_sdk_dispatch(monkeypatch,kind):
    from contextlib import asynccontextmanager
    from eic.core import dependency_controls
    seen=[]
    @asynccontextmanager
    async def denied(*args,**kwargs):
        assert args[1]==1 and kwargs['reserve_units']>0
        raise ModelProviderError('denied',reason='fixture_dependency_denied',retryable=False)
        yield
    monkeypatch.setattr(dependency_controls,'guard',denied)
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r:seen.append(r))) as c:
        with pytest.raises(ModelProviderError) as exc:
            await probe_model_connection(model(kind=kind),'override',client=c)
    assert exc.value.reason=='fixture_dependency_denied' and not seen


@pytest.mark.asyncio
@pytest.mark.parametrize('provider', ['openai', 'openai_compatible', 'bge_rerank'])
async def test_rerank_existing_compressor_probe(provider):
    seen=[]
    async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda r:(seen.append(r),httpx.Response(200,json={'results':[{'index':0,'relevance_score':0.9}]}))[1])) as c:
        await probe_model_connection(model(provider,kind='rerank',endpoint='https://provider.invalid/v1'),'override',client=c)
        assert not c.is_closed
    assert len(seen)==1


@pytest.mark.asyncio
@pytest.mark.parametrize('provider', ['openai','ollama'])
@pytest.mark.parametrize('mode', ['timeout','malformed','cancel'])
async def test_owned_client_closed_and_single_dispatch(monkeypatch,provider,mode):
    import asyncio
    from contextlib import asynccontextmanager
    import eic.ai.langchain_probe as probe
    from eic.core import dependency_controls
    factory=httpx.AsyncClient
    clients=[]; seen=[]; entered=asyncio.Event()
    async def respond(r):
        seen.append(r)
        if mode=='timeout': raise httpx.ReadTimeout('fixture',request=r)
        if mode=='cancel':
            entered.set()
            await asyncio.Event().wait()
        return httpx.Response(200,json={'unexpected':'fixture'})
    class Owned(factory):
        def __init__(self,*args,**kwargs):
            if 'transport' not in kwargs:
                kwargs['transport']=httpx.MockTransport(respond)
            super().__init__(*args,**kwargs)
            clients.append(self)
    monkeypatch.setattr(probe.httpx,'AsyncClient',Owned)
    @asynccontextmanager
    async def permit(*args, **kwargs):
        yield {}
    monkeypatch.setattr(dependency_controls, 'guard', permit)
    error=asyncio.CancelledError if mode=='cancel' else ModelProviderError
    if mode=='cancel':
        task=asyncio.create_task(probe_model_connection(model(provider,'https://provider.invalid'),'override'))
        await asyncio.wait_for(entered.wait(),5)
        task.cancel()
        with pytest.raises(error): await task
    else:
        with pytest.raises(error):
            await probe_model_connection(model(provider,'https://provider.invalid'),'override')
    assert len(seen)==1 and clients and all(c.is_closed for c in clients)


@pytest.mark.asyncio
async def test_azure_incomplete_native_configuration_before_dispatch():
    seen=[]
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r:seen.append(r))) as c:
        with pytest.raises(ModelProviderError) as exc:
            await probe_model_connection(model('azure','https://azure.example/openai/deployments/d/chat/completions'),'override',client=c)
    assert exc.value.reason=='invalid_provider_configuration' and not seen


@pytest.mark.asyncio
@pytest.mark.parametrize('provider', ['openai','azure','ollama'])
@pytest.mark.parametrize('secret', ['', 'explicit-only'])
async def test_borrowed_credentials_and_environment_never_inherited(monkeypatch,provider,secret):
    for name in ('OPENAI_API_KEY','AZURE_OPENAI_API_KEY','AZURE_OPENAI_AD_TOKEN','OLLAMA_API_KEY'):
        monkeypatch.setenv(name,'fixture-environment-credential')
    seen=[]
    endpoint=('https://azure.example/openai/deployments/d/chat/completions?api-version=2024-10-21'
              if provider=='azure' else 'https://provider.invalid')
    def respond(r):
        seen.append(r)
        return httpx.Response(200,json=({'message':{'role':'assistant','content':'ok'},'done':True}
                                     if provider=='ollama' else reply()))
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond),
            auth=httpx.BasicAuth('borrowed-user','borrowed-password'),
            headers={'X-Borrowed':'must-not-transfer','Authorization':'borrowed-auth','api-key':'borrowed-key'},
            cookies={'borrowed-cookie':'must-not-transfer'},follow_redirects=True) as c:
        await probe_model_connection(model(provider,endpoint),secret,client=c)
        assert not c.is_closed and c.cookies['borrowed-cookie']=='must-not-transfer'
    r=seen[0]
    assert len(seen)==1 and 'x-borrowed' not in r.headers and 'cookie' not in r.headers
    if provider=='azure':
        assert r.headers.get('api-key')==(secret or None)
        assert 'authorization' not in r.headers
    else:
        assert r.headers.get('authorization')==('Bearer '+secret if secret else None)
        assert 'api-key' not in r.headers
    assert 'fixture-environment' not in str(r.headers)


@pytest.mark.asyncio
@pytest.mark.parametrize('provider', ['openai','azure','ollama'])
async def test_redirect_never_crosses_origin_with_borrowed_follow_redirects(provider):
    seen=[]
    def respond(r):
        seen.append(r)
        return httpx.Response(307,headers={'location':'https://other.invalid/steal'})
    endpoint=('https://azure.example/openai/deployments/d/chat/completions?api-version=2024-10-21'
              if provider=='azure' else 'https://provider.invalid')
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond),follow_redirects=True) as c:
        with pytest.raises(ModelProviderError):
            await probe_model_connection(model(provider,endpoint),'explicit',client=c)
        assert not c.is_closed
    assert len(seen)==1 and seen[0].url.host!='other.invalid'


@pytest.mark.asyncio
@pytest.mark.parametrize('kind,provider', [('unknown','openai'),('','openai'),(None,'openai'),
                                        ('chat','unknown'),('embedding','unknown'),('rerank','unknown')])
async def test_unknown_type_and_provider_rejected_before_dispatch(kind,provider):
    seen=[]
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r:seen.append(r))) as c:
        with pytest.raises(ModelProviderError):
            await probe_model_connection(model(provider,kind=kind),'explicit',client=c)
        assert not c.is_closed
    assert not seen


@pytest.mark.asyncio
@pytest.mark.parametrize('provider', ['openai','ollama'])
@pytest.mark.parametrize('failure', ['construct','cancel','cleanup'])
async def test_isolated_clients_failure_boundaries_borrowed_open(monkeypatch,provider,failure):
    import asyncio
    from eic.ai import langchain_stream
    import ollama
    factory=httpx.AsyncClient
    close=factory.aclose
    sync_factory=httpx.Client
    owned=[];sync_owned=[];seen=[]
    entered=asyncio.Event()
    async def respond(r):
        seen.append(r)
        if failure=='cancel':
            entered.set()
            await asyncio.Event().wait()
        return httpx.Response(200,json=({'message':{'role':'assistant','content':'ok'},'done':True}
                                     if provider=='ollama' else reply()))
    borrowed=factory(transport=httpx.MockTransport(respond))
    class Tracked(factory):
        def __init__(self,*args,**kwargs):
            super().__init__(*args,**kwargs);owned.append(self)
        async def aclose(self):
            await close(self)
            if failure=='cleanup':
                raise RuntimeError('fixture cleanup failure')
    class SyncTracked(sync_factory):
        def __init__(self,*args,**kwargs):
            super().__init__(*args,**kwargs);sync_owned.append(self)
    monkeypatch.setattr(httpx,'AsyncClient',Tracked)
    monkeypatch.setattr(httpx,'Client',SyncTracked)
    if failure=='construct':
        def rejected(*args,**kwargs):
            raise ValueError('fixture construction failure')
        monkeypatch.setattr(ollama if provider=='ollama' else langchain_stream,
                            'AsyncClient' if provider=='ollama' else '_EICChatOpenAI',rejected)
    try:
        if failure=='cancel':
            task=asyncio.create_task(probe_model_connection(model(provider,'https://provider.invalid'),'explicit',client=borrowed))
            await asyncio.wait_for(entered.wait(),5)
            task.cancel()
            with pytest.raises(asyncio.CancelledError): await task
        else:
            with pytest.raises((ModelProviderError,RuntimeError)):
                await probe_model_connection(model(provider,'https://provider.invalid'),'explicit',client=borrowed)
        assert not borrowed.is_closed
        assert owned and all(c.is_closed for c in owned+sync_owned)
        assert len(seen)==(0 if failure=='construct' else 1)
    finally:
        await close(borrowed)


@pytest.mark.asyncio
async def test_closed_borrowed_client_refused_before_transport():
    seen=[]
    c=httpx.AsyncClient(transport=httpx.MockTransport(lambda r:seen.append(r)))
    await c.aclose()
    with pytest.raises(ModelProviderError): await probe_model_connection(model(),'explicit',client=c)
    assert not seen
