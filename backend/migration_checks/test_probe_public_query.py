"""真实公开probe入口的query字节及借入所有权契约。"""
import httpx
import pytest

from eic.ai.providers import probe_model_connection
from eic.models import AIModel


@pytest.mark.asyncio
@pytest.mark.parametrize('provider', ['openai', 'azure', 'ollama'])
@pytest.mark.parametrize('query', ['', 'a=1&b=2&a=3&bare&empty=&encoded=%2F%20%2B'])
async def test_public_probe_query_and_ownership(provider, query):
    azure = provider == 'azure'
    if azure:
        query = 'api-version=2024-10-21' + ('&' + query if query else '')
    path = ('/openai/deployments/fixture/chat/completions' if azure else
            '/api/chat' if provider == 'ollama' else '/v1/chat/completions')
    endpoint = 'https://fixture.invalid' + path + ('?' + query if query else '')
    model = AIModel(id=0, tenant_id=1, provider=provider, endpoint=endpoint,
                    model_name='fixture', model_type='chat', max_input_tokens=1000,
                    max_output_tokens=128, enabled=True)
    seen = []
    def respond(request):
        seen.append(request)
        body = ({'model': 'fixture', 'message': {'role': 'assistant', 'content': 'ok'}, 'done': True}
                if provider == 'ollama' else {'id': 'fixture', 'object': 'chat.completion',
                'created': 0, 'model': 'fixture', 'choices': [{'index': 0,
                'message': {'role': 'assistant', 'content': 'ok'}, 'finish_reason': 'stop'}]})
        return httpx.Response(200, json=body)
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond),
            headers={'X-Borrowed': 'never-transfer'}, cookies={'borrowed': 'never-transfer'},
            auth=httpx.BasicAuth('borrowed', 'synthetic'), follow_redirects=True) as client:
        await probe_model_connection(model, 'synthetic-explicit', client=client)
        assert not client.is_closed
        assert client.cookies['borrowed'] == 'never-transfer'
    assert len(seen) == 1
    request = seen[0]
    assert str(request.url) == endpoint
    assert request.url.query == query.encode('ascii')
    assert 'cookie' not in request.headers and 'x-borrowed' not in request.headers
    assert request.headers.get('api-key' if azure else 'authorization') == (
        'synthetic-explicit' if azure else 'Bearer synthetic-explicit')
    assert model.secret_encrypted is None
