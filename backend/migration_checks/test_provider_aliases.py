import json
import asyncio
import copy

import httpx
import pytest

from eic.ai import langchain_stream, providers
from eic.core.security import encrypt_secret
from eic.models import AIModel


def model(provider, endpoint):
    return AIModel(id=1, tenant_id=1, provider=provider, model_type="chat",
                   endpoint=endpoint, model_name="fixture", secret_encrypted=encrypt_secret("key"),
                   max_input_tokens=1000, max_output_tokens=128)


def sse():
    return ("data: " + json.dumps({"choices": [{"index": 0, "delta": {"content": "ok"},
        "finish_reason": "stop"}]}) + "\n\n").encode()


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["openai", "1", "zhipu", "3", "dashscope", "12"])
async def test_openai_compatible_aliases_use_public_langchain_entrypoint(provider):
    async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: httpx.Response(200, content=sse(),
                                            headers={"content-type": "text/event-stream"}))) as client:
        assert [x async for x in providers.stream_chat_completion(
            model(provider, "https://provider.invalid/v1/chat/completions"), "q", client=client)] == ["ok"]


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", ["https://azure.example/openai/chat/completions",
                                       "https://azure.example/openai/deployments/d/chat/completions"])
async def test_azure_missing_or_incomplete_configuration_fails_before_dispatch(endpoint):
    calls = []
    async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: (calls.append(request), httpx.Response(500))[1])) as client:
        with pytest.raises(providers.ModelProviderError) as exc:
            _ = [x async for x in providers.stream_chat_completion(
                model("azure", endpoint), "q", client=client)]
    assert exc.value.reason == "invalid_provider_configuration"
    assert calls == []


@pytest.mark.asyncio
async def test_ollama_alias_does_not_fallback_to_legacy_parser():
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(
            200, content=b'{"message":{"role":"assistant","content":"ok"},"done":true}\n'))) as client:
        assert [x async for x in providers.stream_chat_completion(
            model("ollama", "https://ollama.example"), "q", client=client)] == ["ok"]


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["azure", "2"])
async def test_azure_sdk_route_uses_deployment_version_and_api_key(provider):
    seen = []
    def respond(request):
        seen.append(request)
        return httpx.Response(200, content=sse(), headers={"content-type": "text/event-stream"})
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        assert [x async for x in providers.stream_chat_completion(model(provider,
            "https://azure.example/openai/deployments/dep/chat/completions?api-version=2024-10-21"),
            "q", client=client)] == ["ok"]
    assert len(seen) == 1
    assert seen[0].url.path == "/openai/deployments/dep/chat/completions"
    assert seen[0].url.params["api-version"] == "2024-10-21"
    assert seen[0].headers["api-key"] == "key"


@pytest.mark.asyncio
@pytest.mark.parametrize('provider', ['ollama', '10'])
async def test_ollama_standard_sdk_preserves_wire_usage_reasoning_and_client(provider):
    seen = []
    body = (b'{"message":{"content":"ok","thinking":"private"},"done":true,'
            b'"prompt_eval_count":7,"eval_count":3}\n')
    def respond(request):
        seen.append(request)
        return httpx.Response(200, content=body)
    usage = {}; reasoning = []
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        assert [x async for x in providers.stream_chat_completion(
            model(provider, 'https://ollama.example'), 'q', client=client,
            usage_sink=usage, reasoning_sink=reasoning, temperature=0.4)] == ['ok']
        assert not client.is_closed
    assert len(seen) == 1 and seen[0].url.path == '/api/chat'
    assert seen[0].headers['authorization'] == 'Bearer key'
    assert json.loads(seen[0].content)['options'] == {
        'temperature': 0.4, 'num_predict': 128, 'num_ctx': 1128}
    assert usage['input_tokens'] == 7 and usage['output_tokens'] == 3
    assert reasoning == ['private']


def tool(name):
    return {'type': 'function', 'function': {'name': name, 'parameters': {'type': 'object'}}}


def ndjson(calls=(), done=False, content=''):
    return (json.dumps({'message': {'content': content, 'tool_calls': [
        {'function': {'name': name, 'arguments': args}} for name, args in calls]},
        'done': done}) + '\n').encode()


@pytest.mark.asyncio
@pytest.mark.parametrize('provider,endpoint', [
    ('openai', 'https://fixture.invalid/v1/chat/completions'),
    ('azure', 'https://fixture.invalid/openai/deployments/d/chat/completions?api-version=2024-10-21'),
    ('zhipu', 'https://fixture.invalid/v1/chat/completions'),
    ('dashscope', 'https://fixture.invalid/v1/chat/completions'),
    ('ollama', 'https://fixture.invalid'),
])
@pytest.mark.parametrize('secret', ['', 'explicit'])
async def test_stream_credentials_only_explicit_borrowed_client_stays_open(monkeypatch, provider, endpoint, secret):
    monkeypatch.setenv('OLLAMA_API_KEY', 'foreign-env')
    monkeypatch.setenv('OPENAI_ORG_ID', 'foreign-org')
    monkeypatch.setenv('OPENAI_PROJECT_ID', 'foreign-project')
    monkeypatch.setenv('AZURE_OPENAI_AD_TOKEN', 'foreign-token')
    seen = []
    def respond(request):
        seen.append(request)
        return httpx.Response(200, content=ndjson(done=True, content='ok') if provider == 'ollama' else sse(),
                              headers={'content-type': 'text/event-stream'} if provider != 'ollama' else {})
    m = model(provider, endpoint)
    m.secret_encrypted = encrypt_secret(secret) if secret else None
    if provider == 'azure' and not secret:
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            with pytest.raises(providers.ModelProviderError):
                _ = [x async for x in providers.stream_chat_completion(m, 'q', client=client)]
        assert not seen
        return
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond),
            auth=httpx.BasicAuth('foreign', 'password'), cookies={'foreign': 'cookie'},
            headers={'x-foreign': 'default', 'authorization': 'foreign-header'}, follow_redirects=True) as client:
        assert [x async for x in providers.stream_chat_completion(m, 'q', client=client)] == ['ok']
        assert not client.is_closed
    assert len(seen) == 1
    headers = seen[0].headers
    assert not any(k in headers for k in ('cookie', 'x-foreign', 'openai-organization', 'openai-project'))
    if provider == 'azure':
        assert headers.get('api-key') == secret and 'authorization' not in headers
    else:
        assert headers.get('authorization') == (f'Bearer {secret}' if secret else None)


@pytest.mark.asyncio
@pytest.mark.parametrize('provider', ['openai', 'ollama'])
async def test_stream_does_not_follow_borrowed_redirect(provider):
    seen = []
    def respond(request):
        seen.append(request)
        return httpx.Response(307, headers={'location': 'https://other.invalid/stolen'})
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond), follow_redirects=True) as client:
        with pytest.raises(providers.ModelProviderError):
            _ = [x async for x in providers.stream_chat_completion(
                model(provider, 'https://fixture.invalid'), 'q', client=client)]
        assert not client.is_closed
    assert len(seen) == 1 and seen[0].url.host == 'fixture.invalid'


def openai_tool_response():
    return ('data: ' + json.dumps({'choices': [{'index': 0, 'delta': {'tool_calls': [
        {'index': 0, 'id': 'new', 'type': 'function', 'function': {'name': 'a', 'arguments': '{}'}}]},
        'finish_reason': 'tool_calls'}]}) + '\n\n').encode()


@pytest.mark.asyncio
async def test_openai_second_round_preserves_historical_call_ids_without_mutation():
    seen = []
    history = [{'role': 'assistant', 'content': None, 'tool_calls': [
        {'id': 'old-a', 'type': 'function', 'function': {'name': 'a', 'arguments': '{}'}},
        {'id': 'old-b', 'type': 'function', 'function': {'name': 'b', 'arguments': '{"n":2}'}}]},
        {'role': 'tool', 'name': 'a', 'tool_call_id': 'old-a', 'content': 'one'},
        {'role': 'tool', 'name': 'b', 'tool_call_id': 'old-b', 'content': 'two'}]
    before = copy.deepcopy(history)
    def respond(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, content=sse(), headers={'content-type': 'text/event-stream'})
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        assert [x async for x in providers.stream_chat_completion(model('openai', 'https://fixture.invalid'),
            'q', messages=history, client=client)] == ['ok']
    assert [m['tool_call_id'] for m in seen[0]['messages'][-2:]] == ['old-a', 'old-b']
    assert [c['id'] for c in seen[0]['messages'][0]['tool_calls']] == ['old-a', 'old-b']
    assert history == before


@pytest.mark.asyncio
async def test_openai_constructor_failure_closes_owned_facades_only(monkeypatch):
    closed = []
    borrowed = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: pytest.fail('must not dispatch')))
    original_async = httpx.AsyncClient.aclose
    original_sync = httpx.Client.close
    async def close_async(self):
        await original_async(self)
        if self is not borrowed:
            closed.append('async')
    def close_sync(self):
        original_sync(self)
        closed.append('sync')
    def fail(*args, **kwargs):
        raise ValueError('fixture constructor failure')
    monkeypatch.setattr(httpx.AsyncClient, 'aclose', close_async)
    monkeypatch.setattr(httpx.Client, 'close', close_sync)
    monkeypatch.setattr(langchain_stream, '_EICChatOpenAI', fail)
    try:
        with pytest.raises(providers.ModelProviderError):
            _ = [x async for x in providers.stream_chat_completion(
                model('openai', 'https://fixture.invalid'), 'q', client=borrowed)]
        assert not borrowed.is_closed and closed == ['async', 'sync']
    finally:
        await original_async(borrowed)


@pytest.mark.asyncio
@pytest.mark.parametrize('provider', ['openai', 'ollama'])
async def test_none_unsolicited_tool_preserves_existing_sink(provider):
    sink = [{'id': 'existing', 'nested': {'value': [1]}}]
    existing = sink[0]
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(
            200, content=ndjson([('a', {})], True) if provider == 'ollama' else openai_tool_response(),
            headers={} if provider == 'ollama' else {'content-type': 'text/event-stream'}))) as client:
        with pytest.raises(providers.ModelProviderError) as exc:
            _ = [x async for x in providers.stream_chat_completion(
                model(provider, 'https://fixture.invalid'), 'q', client=client,
                tools=[tool('a')], tool_choice='none', tool_call_sink=sink)]
    assert exc.value.reason == 'provider_outcome_uncertain'
    assert sink == [{'id': 'existing', 'nested': {'value': [1]}}] and sink[0] is existing


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['async', 'sync', 'cancel'])
async def test_openai_cleanup_never_publishes_tool_sink(monkeypatch, failure):
    sink = [{'id': 'existing'}]
    existing = sink[0]
    borrowed = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(
        200, content=openai_tool_response(), headers={'content-type': 'text/event-stream'})))
    original_async = httpx.AsyncClient.aclose
    original_sync = httpx.Client.close
    closed = []; entered = asyncio.Event(); release = asyncio.Event()
    async def close_async(self):
        await original_async(self)
        if self is not borrowed:
            closed.append('async')
            assert sink == [{'id': 'existing'}]
            if failure == 'async':
                raise RuntimeError('fixture close failure')
            if failure == 'cancel':
                entered.set()
                await release.wait()
    def close_sync(self):
        original_sync(self)
        closed.append('sync')
        assert sink == [{'id': 'existing'}]
        if failure == 'sync':
            raise RuntimeError('fixture sync close failure')
    monkeypatch.setattr(httpx.AsyncClient, 'aclose', close_async)
    monkeypatch.setattr(httpx.Client, 'close', close_sync)
    async def consume():
        return [x async for x in providers.stream_chat_completion(model('openai', 'https://fixture.invalid'),
            'q', client=borrowed, tools=[tool('a')], tool_choice='required', tool_call_sink=sink)]
    try:
        if failure == 'cancel':
            task = asyncio.create_task(consume())
            await asyncio.wait_for(entered.wait(), 5)
            task.cancel(); release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            with pytest.raises(providers.ModelProviderError) as exc:
                await consume()
            assert exc.value.reason == 'provider_cleanup_failed'
        assert not borrowed.is_closed
        assert sink == [{'id': 'existing'}] and sink[0] is existing
        assert 'async' in closed and 'sync' in closed
    finally:
        await original_async(borrowed)


@pytest.mark.asyncio
@pytest.mark.parametrize('split', [False, True])
async def test_actual_sdk_multi_tool_ids_order_and_second_round(split):
    first = [('a', {'n': 1}), ('b', {'n': 2})]
    body = (ndjson(first[:1]) + ndjson(first[1:], True)) if split else ndjson(first, True)
    seen = []; sink = []
    def respond(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, content=body if len(seen) == 1 else ndjson(done=True, content='ok'))
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        m = model('ollama', 'https://ollama.example/api/chat')
        _ = [x async for x in providers.stream_chat_completion(m, 'q', client=client,
            tools=[tool('a'), tool('b')], tool_choice='required', tool_call_sink=sink)]
        assert [x['function']['name'] for x in sink] == ['a', 'b']
        assert len({x['id'] for x in sink}) == 2
        history = [{'role': 'user', 'content': 'q'},
                   {'role': 'assistant', 'content': '', 'tool_calls': sink}] + [
                       {'role': 'tool', 'tool_call_id': x['id'], 'name': x['function']['name'],
                        'content': str(i)} for i, x in enumerate(sink)]
        assert [x async for x in providers.stream_chat_completion(m, 'q', messages=history,
            client=client)] == ['ok']
        assert not client.is_closed
    assert seen[1]['messages'][-2]['content'] == '0'
    assert seen[1]['messages'][-1]['content'] == '1'
    assert len(seen[1]['messages'][1]['tool_calls']) == 2
    assert [m['tool_name'] for m in seen[1]['messages'][-2:]] == ['a', 'b']
    assert all('tool_call_id' not in m for m in seen[1]['messages'][-2:])


@pytest.mark.asyncio
@pytest.mark.parametrize('body,choice', [
    (ndjson([('a', {})]) + ndjson([('a', {})], True), 'auto'),
    (ndjson([('a', {})]), 'auto'),
    (ndjson(done=True), 'required'),
    (ndjson([('b', {})], True), 'a'),
    (ndjson([('a', {})], True), 'none'),
])
async def test_ollama_invalid_or_incomplete_response_never_commits_calls(body, choice):
    sink = [{'id': 'existing'}]
    existing = sink[0]
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, content=body))) as client:
        with pytest.raises(providers.ModelProviderError):
            _ = [x async for x in providers.stream_chat_completion(
                model('10', 'https://ollama.example'), 'q', client=client,
                tools=[tool('a'), tool('b')], tool_choice=choice, tool_call_sink=sink)]
    assert sink == [{'id': 'existing'}] and sink[0] is existing


@pytest.mark.asyncio
async def test_ollama_query_endpoint_rejected_before_dispatch():
    seen = []
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: seen.append(r))) as client:
        with pytest.raises(providers.ModelProviderError) as exc:
            _ = [x async for x in providers.stream_chat_completion(
                model('ollama', 'https://ollama.example/api/chat?route=1'), 'q', client=client)]
    assert exc.value.reason == 'invalid_provider_endpoint' and seen == []


@pytest.mark.asyncio
@pytest.mark.parametrize('second', [[('b', {'y': 2, 'x': 1}), ('c', {})],
                                  [('a', {}), ('b', {'x': 1, 'y': 2})]])
async def test_overlap_canonical_calls_preserve_existing_sink(second):
    sink = [{'id': 'existing', 'nested': {'value': [1]}}]
    before = copy.deepcopy(sink)
    existing = sink[0]
    body = ndjson([('a', {}), ('b', {'x': 1, 'y': 2})]) + ndjson(second, True)
    async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda r: httpx.Response(200, content=body))) as client:
        with pytest.raises(providers.ModelProviderError) as exc:
            _ = [x async for x in providers.stream_chat_completion(
                model('ollama', 'https://ollama.example'), 'q', client=client,
                tools=[tool('a'), tool('b'), tool('c')], tool_call_sink=sink)]
        assert not client.is_closed
    assert exc.value.reason == 'provider_outcome_uncertain'
    assert sink == before and sink[0] is existing


@pytest.mark.asyncio
async def test_same_chunk_equal_arguments_without_upstream_ids_rejects_whole_round():
    sink = [{'id': 'existing'}]
    async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda r: httpx.Response(200, content=ndjson([('a', {}), ('a', {})], True)))) as client:
        with pytest.raises(providers.ModelProviderError) as error:
            _ = [x async for x in providers.stream_chat_completion(
                model('ollama', 'https://ollama.example'), 'q', client=client,
                tools=[tool('a')], tool_call_sink=sink)]
        assert not client.is_closed
    assert error.value.reason == 'provider_outcome_uncertain'
    assert sink == [{'id': 'existing'}]


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['source', 'async', 'sync', 'cancel'])
async def test_cleanup_failure_or_cancel_never_publishes_existing_sink(monkeypatch, failure):
    sink = [{'id': 'existing', 'nested': {'value': [1]}}]
    before = copy.deepcopy(sink)
    existing = sink[0]
    async_close = httpx.AsyncClient.aclose
    sync_close = httpx.Client.close
    async_calls = []
    sync_calls = []
    entered = asyncio.Event()
    release = asyncio.Event()
    borrowed = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda r: httpx.Response(200, content=ndjson([('a', {})], True))))
    async def close_async(self):
        if self is borrowed:
            return await async_close(self)
        async_calls.append(self)
        await async_close(self)
        # 第一次是 SDK 原始 async client；第二次在完整响应后清理 replacement。
        if len(async_calls) == 2:
            assert sink == before
            if failure == 'async':
                raise RuntimeError('fixture async cleanup failure')
            if failure == 'cancel':
                entered.set()
                await release.wait()
    def close_sync(self):
        sync_calls.append(self)
        sync_close(self)
        assert sink == before
        if failure == 'sync':
            raise RuntimeError('fixture sync cleanup failure')
    monkeypatch.setattr(httpx.AsyncClient, 'aclose', close_async)
    monkeypatch.setattr(httpx.Client, 'close', close_sync)
    if failure == 'source':
        from langchain_core.runnables.base import RunnableBinding
        sdk_astream = RunnableBinding.astream
        class FailingClose:
            def __init__(self, source):
                self.source = source
            def __aiter__(self):
                return self
            async def __anext__(self):
                return await anext(self.source)
            async def aclose(self):
                await self.source.aclose()
                assert sink == before
                raise RuntimeError('fixture source cleanup failure')
        # 仅故障注入 SDK iterator 的资源边界，仍执行官方解析与真实公开入口。
        def failing_astream(self, *args, **kwargs):
            return FailingClose(sdk_astream(self, *args, **kwargs))
        monkeypatch.setattr(RunnableBinding, 'astream', failing_astream)
    async def consume():
        return [x async for x in providers.stream_chat_completion(
            model('ollama', 'https://ollama.example'), 'q', client=borrowed,
            tools=[tool('a')], tool_choice='required', tool_call_sink=sink)]
    try:
        if failure == 'cancel':
            task = asyncio.create_task(consume())
            await asyncio.wait_for(entered.wait(), 5)
            task.cancel()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            with pytest.raises(RuntimeError):
                await consume()
        assert not borrowed.is_closed
        assert len(async_calls) >= 3 and sync_calls
        assert all(c.is_closed for c in async_calls + sync_calls)
        assert sink == before and sink[0] is existing
    finally:
        await async_close(borrowed)
