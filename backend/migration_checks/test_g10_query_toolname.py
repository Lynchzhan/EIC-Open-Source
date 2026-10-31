"""公开provider入口的URL query语义；不调用SDK私有序列化函数。"""

import json
from urllib.parse import parse_qsl

import httpx
import pytest

from eic.ai import providers
from eic.models import AIModel


@pytest.mark.asyncio
@pytest.mark.parametrize('query,expected', [
    ('route=first&route=second&empty=&bare',
     [('route', 'first'), ('route', 'second'), ('empty', ''), ('bare', '')]),
    ('route=a%2Bb&route=a+b&empty=&text=%E4%B8%AD%E6%96%87&encoded=%26%3D%2F%25',
     [('route', 'a+b'), ('route', 'a b'), ('empty', ''), ('text', '中文'),
      ('encoded', '&=/%')]),
    ('route=first&marker=between&route=second&empty=',
     [('route', 'first'), ('marker', 'between'), ('route', 'second'), ('empty', '')]),
])
async def test_public_query_semantics_terminal(query, expected):
    requests = []

    def respond(request):
        requests.append(request)
        body = {'choices': [{'index': 0, 'delta': {'content': 'synthetic-terminal'},
                             'finish_reason': 'stop'}]}
        return httpx.Response(200, content=('data: ' + json.dumps(body) + '\n\n').encode(),
                              headers={'content-type': 'text/event-stream'})

    model = AIModel(id=101, tenant_id=102, provider='openai_compatible', model_type='chat',
                    endpoint='https://provider.invalid/v1/chat/completions?' + query,
                    model_name='synthetic', secret_encrypted=None,
                    max_input_tokens=1000, max_output_tokens=128)
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        result = [part async for part in providers.stream_chat_completion(
            model, 'synthetic-question', client=client)]
        assert not client.is_closed
    assert result == ['synthetic-terminal']
    assert len(requests) == 1
    assert requests[0].url.path == '/v1/chat/completions'
    # 语义由实际HTTP请求的URL确定；保留重复键的全局顺序而非只比较dict。
    assert parse_qsl(requests[0].url.query.decode('ascii'), keep_blank_values=True) == expected
    assert 'authorization' not in requests[0].headers


@pytest.mark.asyncio
@pytest.mark.parametrize('provider', ['openai_compatible', 'azure'])
async def test_public_raw_query_and_isolation(provider, monkeypatch):
    from eic.core.security import encrypt_secret

    query = 'route=first&marker=between&route=second&empty=&bare&encoded=%2f%2B+%26'
    path = '/v1/chat/completions'
    if provider == 'azure':
        path = '/openai/deployments/synthetic-dep/chat/completions'
        query = 'api-version=2024-10-21&' + query
    seen = []
    monkeypatch.setenv('OPENAI_API_KEY', 'foreign-env')
    monkeypatch.setenv('AZURE_OPENAI_API_KEY', 'foreign-env')

    def respond(request):
        seen.append(request)
        body = {'choices': [{'index': 0, 'delta': {'content': 'synthetic-terminal'},
                             'finish_reason': 'stop'}]}
        return httpx.Response(200, content=('data: ' + json.dumps(body) + '\n\n').encode(),
                              headers={'content-type': 'text/event-stream'})

    model = AIModel(id=101, tenant_id=102, provider=provider, model_type='chat',
                    endpoint='https://provider.invalid' + path + '?' + query,
                    model_name='synthetic', secret_encrypted=encrypt_secret('synthetic-explicit'),
                    max_input_tokens=1000, max_output_tokens=128)
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond),
            auth=('foreign', 'secret'), cookies={'foreign': 'cookie'},
            headers={'x-foreign': 'value'}, follow_redirects=True) as client:
        assert [part async for part in providers.stream_chat_completion(
            model, 'synthetic-question', client=client)] == ['synthetic-terminal']
        assert not client.is_closed
    assert len(seen) == 1
    assert seen[0].url.path == path
    assert seen[0].url.query == query.encode('ascii')
    assert 'cookie' not in seen[0].headers and 'x-foreign' not in seen[0].headers
    if provider == 'azure':
        assert seen[0].url.params.get_list('api-version') == ['2024-10-21']
        assert seen[0].headers['api-key'] == 'synthetic-explicit'
        assert 'authorization' not in seen[0].headers
    else:
        assert seen[0].headers['authorization'] == 'Bearer synthetic-explicit'


@pytest.mark.asyncio
@pytest.mark.parametrize('version', ['', 'api-version=&', 'api-version=a&api-version=b&'])
async def test_azure_invalid_version_never_dispatches(version):
    from eic.core.security import encrypt_secret

    seen = []
    model = AIModel(id=101, tenant_id=102, provider='azure', model_type='chat',
                    endpoint='https://provider.invalid/openai/deployments/d/chat/completions?'
                    + version + 'route=first&marker=between&route=second',
                    model_name='synthetic', secret_encrypted=encrypt_secret('synthetic-explicit'),
                    max_input_tokens=1000, max_output_tokens=128)
    async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: (seen.append(request), httpx.Response(500))[1])) as client:
        with pytest.raises(providers.ModelProviderError) as error:
            _ = [part async for part in providers.stream_chat_completion(model, 'q', client=client)]
        assert not client.is_closed
    assert error.value.reason == 'invalid_provider_configuration'
    assert error.value.retryable is False
    assert seen == []


@pytest.mark.asyncio
async def test_owned_transport_has_no_environment_retry_or_redirect(monkeypatch):
    seen = []
    closed = []
    options = []

    class Transport(httpx.MockTransport):
        async def aclose(self):
            closed.append(True)
            await super().aclose()

    def factory(**kwargs):
        options.append(kwargs)
        return Transport(lambda request: (seen.append(request), httpx.Response(
            307, headers={'location': 'https://other.invalid/not-allowed'}))[1])

    monkeypatch.setattr(httpx, 'AsyncHTTPTransport', factory)
    monkeypatch.setenv('HTTPS_PROXY', 'http://foreign.invalid:9999')
    model = AIModel(id=101, tenant_id=102, provider='openai_compatible', model_type='chat',
                    endpoint='https://provider.invalid/v1/chat/completions?x=1&y=&x=2',
                    model_name='synthetic', secret_encrypted=None,
                    max_input_tokens=1000, max_output_tokens=128)
    with pytest.raises(providers.ModelProviderError):
        _ = [part async for part in providers.stream_chat_completion(model, 'q')]
    assert options == [{'trust_env': False, 'retries': 0}]
    assert len(seen) == 1 and len(closed) == 1
    assert seen[0].url.host == 'provider.invalid'
    assert seen[0].url.query == b'x=1&y=&x=2'
    assert 'authorization' not in seen[0].headers


@pytest.mark.asyncio
@pytest.mark.parametrize('suffix', ['', '?'])
async def test_public_empty_query_has_no_trailing_delimiter(suffix):
    seen = []
    endpoint = 'https://provider.invalid/v1/chat/completions'

    def respond(request):
        seen.append(request)
        body = {'choices': [{'index': 0, 'delta': {'content': 'synthetic-terminal'},
                             'finish_reason': 'stop'}]}
        return httpx.Response(200, content=('data: ' + json.dumps(body) + '\n\n').encode(),
                              headers={'content-type': 'text/event-stream'})

    model = AIModel(id=101, tenant_id=102, provider='openai_compatible', model_type='chat',
                    endpoint=endpoint + suffix, model_name='synthetic', secret_encrypted=None,
                    max_input_tokens=1000, max_output_tokens=128)
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        assert [part async for part in providers.stream_chat_completion(
            model, 'synthetic-question', client=client)] == ['synthetic-terminal']
        assert not client.is_closed
    assert len(seen) == 1
    assert str(seen[0].url) == endpoint
    assert seen[0].url.raw_path == b'/v1/chat/completions'
    assert seen[0].url.query == b''
    assert 'authorization' not in seen[0].headers
