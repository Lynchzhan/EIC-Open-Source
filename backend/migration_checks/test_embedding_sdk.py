import asyncio
import httpx
import pytest
import json
from eic.ai.embeddings import embed_texts
from eic.ai.providers import ModelProviderError
from eic.models import AIModel

def test_official_openai_embeddings_delegation_and_borrowed_transport():
    seen=[]
    async def run():
        def handler(request):
            seen.append(request.url.path)
            return httpx.Response(200,json={'data':[{'index':1,'embedding':[0.,1.]},{'index':0,'embedding':[1.,0.]}],'usage':{'prompt_tokens':2}})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            model=AIModel(id=1,tenant_id=1,model_type='embedding',provider='openai',model_name='fixture',endpoint='http://fixture.invalid',enabled=True,embedding_dimensions=2)
            return await embed_texts(model,['a','b'],tenant_id=1,client=client)
    assert asyncio.run(run())==[[1.,0.],[0.,1.]]
    assert seen==['/embeddings']

@pytest.mark.parametrize('provider',['openai','ollama'])
def test_official_wheel_method_called_and_usage(provider,monkeypatch):
    import langchain_openai,langchain_ollama
    cls=langchain_ollama.OllamaEmbeddings if provider=='ollama' else langchain_openai.OpenAIEmbeddings
    original=cls.aembed_documents;called=[];requests=[];usage={}
    async def counted(self,texts,**kwargs):
        called.append(list(texts));return await original(self,texts,**kwargs)
    monkeypatch.setattr(cls,'aembed_documents',counted)
    async def run():
        def handler(request):
            requests.append(json.loads(request.content));assert 'authorization' not in request.headers
            body={'embeddings':[[1.,0.]],'prompt_eval_count':3} if provider=='ollama' else {'data':[{'index':0,'embedding':[1.,0.]}],'usage':{'prompt_tokens':3,'total_tokens':3}}
            return httpx.Response(200,json=body)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            m=AIModel(id=1,tenant_id=1,model_type='embedding',provider=provider,model_name='fixture',endpoint='http://fixture.invalid',enabled=True,embedding_dimensions=2)
            assert await embed_texts(m,['literal'],tenant_id=1,client=client,usage_sink=usage)==[[1.,0.]]
            assert not client.is_closed
    asyncio.run(run());assert called==[['literal']] and len(requests)==1
    assert requests[0]['input']==['literal']
    from eic.core.observability import usage_fields
    expected=usage_fields({'prompt_eval_count':3},ollama=True) if provider=='ollama' else usage_fields({'usage':{'prompt_tokens':3,'total_tokens':3}})
    assert all(usage[k]==v for k,v in expected.items())

def test_no_implicit_retry():
    calls=[]
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r:(calls.append(r) or httpx.Response(503,json={'error':{'message':'synthetic'}})))) as c:
            m=AIModel(id=1,tenant_id=1,model_type='embedding',provider='openai',model_name='fixture',endpoint='http://fixture.invalid',enabled=True)
            with pytest.raises(ModelProviderError):await embed_texts(m,['x'],client=c)
    asyncio.run(run());assert len(calls)==1

def test_cancel_propagates_without_closing_borrowed_client():
    async def run():
        started=asyncio.Event()
        async def handler(request):started.set();await asyncio.Event().wait()
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            m=AIModel(id=1,tenant_id=1,model_type='embedding',provider='openai',model_name='fixture',endpoint='http://fixture.invalid',enabled=True)
            task=asyncio.create_task(embed_texts(m,['x'],client=c));await started.wait();task.cancel()
            with pytest.raises(asyncio.CancelledError):await task
            assert not c.is_closed
    asyncio.run(run())

@pytest.mark.parametrize('provider,endpoint,secret,expected_cls',[
    ('azure','http://fixture.invalid/openai/deployments/dep/embeddings?api-version=2024-02-01&x=1&x=2','explicit','AzureOpenAIEmbeddings'),
    ('azure','http://fixture.invalid/v1/embeddings?x=1&x=2','','OpenAIEmbeddings'),
    ('openai','http://fixture.invalid/v1/embeddings?x=1&x=2','explicit','OpenAIEmbeddings'),
    ('ollama','http://fixture.invalid/api/embed?x=1&x=2','','OllamaEmbeddings'),
])
def test_endpoint_identity_and_official_delegate(provider,endpoint,secret,expected_cls,monkeypatch):
    from eic.ai import embeddings
    import langchain_openai,langchain_ollama
    monkeypatch.setattr(embeddings,'decrypt_secret',lambda _:secret)
    monkeypatch.setenv('OPENAI_API_KEY','environment-must-not-leak')
    monkeypatch.setenv('AZURE_OPENAI_API_KEY','environment-must-not-leak')
    monkeypatch.setenv('AZURE_OPENAI_AD_TOKEN','environment-must-not-leak')
    module=langchain_ollama if provider=='ollama' else langchain_openai
    cls=getattr(module,expected_cls);original=cls.aembed_documents;delegated=[];seen=[]
    async def counted(self,texts,**kwargs):delegated.append(expected_cls);return await original(self,texts,**kwargs)
    monkeypatch.setattr(cls,'aembed_documents',counted)
    async def run():
        def handler(request):
            seen.append(request)
            assert str(request.url)==endpoint
            assert 'cookie' not in request.headers and 'x-borrowed' not in request.headers
            assert request.headers.get('authorization')==(f'Bearer {secret}' if secret and provider!='azure' else None)
            assert request.headers.get('api-key')==(secret if secret and '/openai/deployments/' in endpoint else None)
            return httpx.Response(200,json={'embeddings':[[1.,0.]]} if provider=='ollama' else {'data':[{'index':0,'embedding':[1.,0.]}]})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler),auth=('borrowed','password'),cookies={'borrowed':'cookie'},headers={'Authorization':'Bearer borrowed','X-borrowed':'default'},follow_redirects=True) as client:
            m=AIModel(id=1,tenant_id=1,model_type='embedding',provider=provider,model_name='fixture',endpoint=endpoint,enabled=True,secret_encrypted='synthetic' if secret else None)
            assert await embed_texts(m,['literal'],client=client)==[[1.,0.]]
            assert not client.is_closed
    asyncio.run(run());assert len(seen)==1 and delegated==[expected_cls]

@pytest.mark.parametrize('provider,endpoint',[
    ('azure','http://fixture.invalid/openai/deployments/dep/embeddings'),
    ('azure','http://fixture.invalid/openai/deployments//embeddings?api-version=v'),
    ('azure','http://fixture.invalid/openai/deployments/dep/embeddings?api-version=v&api-version=w'),
    ('azure','http://fixture.invalid/openai/embeddings?api-version=v'),
    ('azure','http://fixture.invalid/v1?api-version=v'),
    ('unregistered','http://fixture.invalid'),
])
def test_invalid_configuration_no_dispatch(provider,endpoint):
    async def run():
        def forbidden(request):raise AssertionError('dispatched')
        async with httpx.AsyncClient(transport=httpx.MockTransport(forbidden)) as c:
            m=AIModel(id=1,tenant_id=1,model_type='embedding',provider=provider,model_name='fixture',endpoint=endpoint,enabled=True)
            with pytest.raises(ModelProviderError):await embed_texts(m,['x'],client=c)
    asyncio.run(run())

def test_redirect_not_followed():
    calls=[]
    async def run():
        def handler(request):calls.append(request);return httpx.Response(307,headers={'location':'http://other.invalid/embeddings'})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler),follow_redirects=True) as c:
            m=AIModel(id=1,tenant_id=1,model_type='embedding',provider='openai',model_name='fixture',endpoint='http://fixture.invalid',enabled=True)
            with pytest.raises(ModelProviderError):await embed_texts(m,['x'],client=c)
            assert not c.is_closed
    asyncio.run(run());assert len(calls)==1

def test_constructor_failure_closes_owned_clients(monkeypatch):
    from eic.ai import langchain_embeddings as module
    original_async=httpx.AsyncClient;original_sync=httpx.Client;created=[]
    def make_async(*a,**kw):obj=original_async(*a,**kw);created.append(obj);return obj
    def make_sync(*a,**kw):obj=original_sync(*a,**kw);created.append(obj);return obj
    monkeypatch.setattr(module.httpx,'AsyncClient',make_async);monkeypatch.setattr(module.httpx,'Client',make_sync)
    monkeypatch.setattr(module,'OpenAIEmbeddings',lambda **kw:(_ for _ in ()).throw(ValueError('constructor secret')))
    async def run():
        async with original_async(transport=httpx.MockTransport(lambda r:httpx.Response(200))) as borrowed:
            m=AIModel(provider='openai',endpoint='http://fixture.invalid',model_name='fixture')
            with pytest.raises(ModelProviderError) as exc:await module.official_embeddings(m,['x'],borrowed,'',{})
            assert 'constructor secret' not in str(exc.value)
            assert not borrowed.is_closed
    asyncio.run(run());assert created and all(c.is_closed for c in created)

def test_dependency_budget_denies_before_sdk(monkeypatch):
    from contextlib import asynccontextmanager
    from eic.core import dependency_controls
    @asynccontextmanager
    async def denied(*a,**kw):
        raise dependency_controls.DependencyDenied('cost_budget_exceeded','synthetic denial')
        yield
    monkeypatch.setattr(dependency_controls,'guard',denied)
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r:(_ for _ in ()).throw(AssertionError('dispatched')))) as c:
            m=AIModel(id=1,tenant_id=1,model_type='embedding',provider='openai',model_name='fixture',endpoint='http://fixture.invalid',enabled=True)
            with pytest.raises(ModelProviderError) as exc:await embed_texts(m,['x'],client=c)
            assert exc.value.reason=='cost_budget_exceeded'
    asyncio.run(run())

def test_cleanup_failure_continues_and_redacts():
    from eic.ai.langchain_embeddings import _cleanup
    visited=[]
    class A:
        async def aclose(self):visited.append('async');raise ValueError('secret')
    class S:
        def close(self):visited.append('sync')
    with pytest.raises(ModelProviderError) as error:asyncio.run(_cleanup([A()],[S()]))
    assert visited==['async','sync'] and 'secret' not in str(error.value)

@pytest.mark.parametrize('case',['tenant','disabled','type'])
def test_service_validation_rejects_before_dispatch(case):
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r:(_ for _ in ()).throw(AssertionError('dispatched')))) as c:
            m=AIModel(id=1,tenant_id=1,model_type='chat' if case=='type' else 'embedding',provider='openai',model_name='fixture',endpoint='http://fixture.invalid',enabled=case!='disabled')
            with pytest.raises(ModelProviderError):await embed_texts(m,['x'],tenant_id=2 if case=='tenant' else 1,client=c)
    asyncio.run(run())

def test_ollama_http_error_sanitized():
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r:httpx.Response(400,json={'error':'secret-provider-error'}))) as c:
            m=AIModel(id=1,tenant_id=1,model_type='embedding',provider='ollama',model_name='fixture',endpoint='http://fixture.invalid',enabled=True)
            with pytest.raises(ModelProviderError) as exc:await embed_texts(m,['x'],client=c)
            assert 'secret-provider-error' not in str(exc.value)
            assert exc.value.__cause__ is None
    asyncio.run(run())

def test_cleanup_survives_second_cancel():
    from eic.ai.langchain_embeddings import _cleanup
    from eic.ai.stream_reader import complete_cleanup
    async def run():
        started=asyncio.Event();release=asyncio.Event();visited=[]
        class A:
            async def aclose(self):started.set();await release.wait();visited.append('async')
        class S:
            def close(self):visited.append('sync')
        task=asyncio.create_task(complete_cleanup(_cleanup([A()],[S()])))
        await started.wait();task.cancel();release.set()
        with pytest.raises(asyncio.CancelledError):await task
        assert visited==['async','sync']
    asyncio.run(run())
