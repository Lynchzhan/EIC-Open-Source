"""原生联网协议隔离测试：全部请求由 MockTransport 接管。"""
import json
from types import SimpleNamespace

import httpx
import pytest

from eic.ai import search
from eic.ai.budget import ChatConfigurationError
from eic.ai.protocol_bridge import ProtocolTransport
from eic.ai.providers import ModelProviderError


def request(**overrides):
    return {'model': 'fixture', 'messages': [{'role': 'user', 'content': 'fixture'}],
            'stream': True, **overrides}


@pytest.mark.asyncio
@pytest.mark.parametrize('enabled', [False, True])
async def test_responses_native_search_is_explicit_and_collects_citations(enabled):
    calls, citations = [], []

    def reply(upstream):
        calls.append(json.loads(upstream.content))
        assert upstream.url.path == '/v1/responses'
        event = {'type': 'response.completed', 'response': {'output': [{'content': [
            {'type': 'output_text', 'text': 'fixture', 'annotations': [
                {'type': 'url_citation', 'url': 'https://example.org/source', 'title': 'Source'},
                {'type': 'url_citation', 'url': 'javascript:alert(1)', 'title': 'Unsafe'},
                {'type': 'url_citation', 'url': 'https://example.org/source', 'title': 'Duplicate'},
            ]}]}]}}
        return httpx.Response(200, text='data: ' + json.dumps(event) + '\n\n',
                              headers={'content-type': 'text/event-stream'})

    transport = ProtocolTransport(httpx.MockTransport(reply), 'openai_responses', '',
                                  native_search=enabled, citation_sink=citations)
    async with httpx.AsyncClient(transport=transport, trust_env=False) as client:
        response = await client.post('http://fixture.invalid/v1/chat/completions', json=request())
    assert response.status_code == 200 and len(calls) == 1
    web_tools = [tool for tool in calls[0].get('tools', []) if tool['type'] == 'web_search']
    assert bool(web_tools) == enabled
    if enabled:
        assert calls[0]['tool_choice'] == {'type': 'web_search'}
    assert [row['url'] for row in citations] == ['https://example.org/source']


@pytest.mark.asyncio
@pytest.mark.parametrize('status', [400, 422])
async def test_native_rejection_does_not_retry_without_search(status):
    calls = []

    def reply(upstream):
        calls.append(json.loads(upstream.content))
        return httpx.Response(status, json={'error': 'unsupported web_search'})

    transport = ProtocolTransport(httpx.MockTransport(reply), 'openai_responses', '', native_search=True)
    async with httpx.AsyncClient(transport=transport, trust_env=False) as client:
        with pytest.raises(ModelProviderError) as error:
            await client.post('http://fixture.invalid/v1/chat/completions', json=request())
    assert error.value.reason == 'search_native_rejected'
    assert error.value.retryable is False
    assert len(calls) == 1 and {'type': 'web_search'} in calls[0]['tools']


@pytest.mark.asyncio
async def test_unsupported_native_protocol_never_sends_request():
    def forbidden(upstream):
        pytest.fail('不支持的原生协议不得发送上游请求')

    transport = ProtocolTransport(httpx.MockTransport(forbidden), 'anthropic', '', native_search=True)
    async with httpx.AsyncClient(transport=transport, trust_env=False) as client:
        with pytest.raises(ModelProviderError) as error:
            await client.post('http://fixture.invalid/v1/chat/completions', json=request())
    assert error.value.reason == 'search_native_unsupported'


@pytest.mark.parametrize('provider', ['openai', 'anthropic', 'ollama'])
def test_native_mode_rejects_unsupported_model_protocol(provider):
    with pytest.raises(ChatConfigurationError, match='search_native_unsupported'):
        search.validate_mode(SimpleNamespace(search_mode='native', provider=provider), SimpleNamespace())


def test_native_mode_needs_no_independent_search_credentials():
    assert search.validate_mode(SimpleNamespace(search_mode='native', provider='openai_responses'),
                                SimpleNamespace()) == 'native'


@pytest.mark.asyncio
async def test_native_search_keeps_business_tools():
    bodies = []

    def reply(upstream):
        bodies.append(json.loads(upstream.content))
        return httpx.Response(200, json={'status': 'completed', 'output': []})

    transport = ProtocolTransport(httpx.MockTransport(reply), 'openai_responses', '', native_search=True)
    async with httpx.AsyncClient(transport=transport, trust_env=False) as client:
        await client.post('http://fixture.invalid/v1/chat/completions', json=request(stream=False,
            tools=[{'type': 'function', 'function': {'name': 'fixture_tool', 'parameters': {'type': 'object'}}}],
            tool_choice='auto'))
    assert {tool['type'] for tool in bodies[0]['tools']} == {'function', 'web_search'}
    assert bodies[0]['tool_choice'] == 'auto'
