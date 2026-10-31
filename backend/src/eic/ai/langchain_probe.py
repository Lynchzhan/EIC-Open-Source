"""非持久连接探测：官方 SDK 派发，保留 EIC 服务授权与依赖保护。"""
from __future__ import annotations

import asyncio
from urllib.parse import urlsplit

import httpx
from openai import AsyncOpenAI, AsyncAzureOpenAI, Omit

from eic.ai.providers import ModelProviderError, chat_parameters, message_units, probe_chat_payload
from eic.ai.stream_reader import complete_cleanup


class _BorrowedTransport(httpx.AsyncBaseTransport):
    def __init__(self, client, secret=None):
        self.client = client
        self.secret = secret

    async def handle_async_request(self, request):
        if self.client.is_closed:
            raise RuntimeError('borrowed client is closed')
        if self.secret is not None:
            for key in ('authorization', 'api-key', 'cookie'):
                if key in request.headers:
                    del request.headers[key]
            if self.secret:
                request.headers['Authorization'] = f'Bearer {self.secret}'
        return await self.client.send(request, stream=True, auth=None, follow_redirects=False)


def _isolated_async_client(client, timeout):
    if client is None:
        return httpx.AsyncClient(timeout=timeout, follow_redirects=False, headers={}, cookies={}, trust_env=False)
    return httpx.AsyncClient(transport=_BorrowedTransport(client), timeout=timeout,
                             follow_redirects=False, headers={}, cookies={}, trust_env=False)


async def _chat(model, secret, client, prompt, timeout_seconds):
    from eic.ai.langchain_stream import _IsolatedTransport, _azure_configuration

    azure = (_azure_configuration(model.endpoint)
             if model.provider.strip().lower() in {'azure', '2'} else None)
    # 与stream共用公开HTTP边界；SDK仅负责协议和路径，不聚合配置query。
    transport = _IsolatedTransport(client, secret, azure=bool(azure),
                                   endpoint_query=urlsplit(model.endpoint).query.encode('ascii'))
    if model.provider in {'openai_responses', 'anthropic'}:
        from eic.ai.protocol_bridge import ProtocolTransport
        transport = ProtocolTransport(transport, model.provider, secret)
    async with httpx.AsyncClient(transport=transport, timeout=httpx.Timeout(timeout_seconds, connect=8),
                                follow_redirects=False, trust_env=False) as isolated:
        return await _chat_with_client(model, secret, isolated, prompt, timeout_seconds)


def _result(text, finish, usage=None):
    if finish in {'length', 'max_tokens'}:
        raise ModelProviderError('上游输出额度已耗尽，响应未完成；请检查回答 Token 和思考配置。',
                                 reason='output_budget_exceeded', retryable=False)
    if finish == 'content_filter':
        raise ModelProviderError('上游内容策略阻止了本次回复。', reason='provider_content_filtered', retryable=False)
    if finish != 'stop':
        raise ModelProviderError('上游未返回有效完成标记。', reason='upstream_response_incomplete', retryable=False)
    if not isinstance(text, str) or not text.strip():
        raise ModelProviderError('上游已结束但未返回可见回答。', reason='empty_model_response', retryable=False)
    # 只公开供应商实报的计数，不以估算值填补缺失 usage。
    counts = {key: value for key, value in (usage or {}).items()
              if key in {'prompt_tokens', 'completion_tokens', 'total_tokens',
                         'input_tokens', 'output_tokens', 'cached_tokens'}
              and type(value) is int and value >= 0}
    return {'reply': text, 'finishReason': finish, 'usage': counts}


async def _chat_with_client(model, secret, client, prompt, timeout_seconds):
    from eic.ai.langchain_stream import (
        _EICChatOpenAI, _EICAzureChatOpenAI,
        _azure_configuration, _chat_completions_base_url, _endpoint_query,
    )
    payload = probe_chat_payload(model, prompt)
    _, profile = chat_parameters(model)
    if message_units(payload['messages'], None) > profile['inputLimit']:
        raise ModelProviderError('模型输入预算超限。', reason='input_budget_exceeded', retryable=False)
    provider = model.provider.strip().lower()
    from eic.ai.protocol_bridge import base_url
    base = base_url(model.endpoint) if model.provider in {'openai_responses', 'anthropic'} else _chat_completions_base_url(model.endpoint)
    timeout = httpx.Timeout(timeout_seconds, connect=8)
    if provider in {'ollama', '10'}:
        from ollama import AsyncClient
        parts = urlsplit(model.endpoint)
        # httpx base_url 支持保留 query；显式 /api/chat 归一化为服务基址。
        if base.endswith('/api/chat'):
            base = base[:-len('/api/chat')]
        transport = {'transport': _BorrowedTransport(client, secret)}
        sdk = None
        try:
            sdk = AsyncClient(host=base, timeout=timeout, follow_redirects=False,
                              headers={'Authorization': f'Bearer {secret}'} if secret else {},
                              cookies={}, params=list(httpx.QueryParams(parts.query).multi_items()), **transport)
            # 官方非流式客户端提供完整 response 校验；不用流式 parser。
            response = await sdk.chat(model=model.model_name,
                messages=payload['messages'], stream=False, options=payload['options'],
                **({'think': payload['think']} if 'think' in payload else {}))
            if response.message is None:
                raise ModelProviderError('上游未返回回答。', reason='empty_model_response', retryable=False)
            return _result(response.message.content,
                'length' if response.done_reason == 'length' else 'stop' if response.done else None,
                {'input_tokens': response.prompt_eval_count, 'output_tokens': response.eval_count})
        finally:
            if sdk is not None:
                await complete_cleanup(sdk._client.aclose())
    if provider not in {'openai', 'openai_compatible', 'openai_responses', 'anthropic', '1', 'azure', '2', 'zhipu', '3', 'dashscope', '12'}:
        raise ModelProviderError('模型供应商不支持连接探测。', reason='unsupported_chat_provider', retryable=False)
    azure = _azure_configuration(model.endpoint) if provider in {'azure', '2'} else None
    configuration = azure or {'base_url': base, 'default_query': _endpoint_query(model.endpoint)}
    async def anonymous_key():
        return ''
    key = secret if secret else anonymous_key
    root = (AsyncAzureOpenAI if azure else AsyncOpenAI)(
        api_key=key, **configuration, max_retries=0, http_client=client, timeout=timeout,
        default_headers={} if secret else {'Authorization': Omit()})
    cls = _EICAzureChatOpenAI if azure else _EICChatOpenAI
    with httpx.Client(timeout=timeout, follow_redirects=False) as sync_client:
        # AzureChatOpenAI 的同步初始化仅接受 SecretStr；该占位值不用于异步派发。
        chat = cls(model=model.model_name,
            api_key=(secret or 'eic-anonymous-unused') if azure else secret or (lambda: ''),
            root_async_client=root, async_client=root.chat.completions,
            http_async_client=client, http_client=sync_client, max_retries=0, use_responses_api=False,
            eic_output_field=profile['outputField'],
            eic_parameters={k:v for k,v in payload.items() if k not in {'model','messages','stream'}},
            **({'azure_ad_token': None, 'azure_ad_token_provider': None} if azure else {}),
            **configuration)
        response = await chat.ainvoke(payload['messages'],
                                     **({'extra_headers': {'Authorization': Omit(), 'api-key': Omit()}} if not secret else {}))
        from eic.ai.providers import _text_from_content
        info = response.response_metadata or {}
        return _result(_text_from_content(response.content), info.get('finish_reason'), info.get('token_usage'))


async def probe_model_connection_langchain(model, secret_override, *, client=None, prompt='ping', timeout_seconds=180):
    from eic.ai.budget import ChatConfigurationError
    from eic.ai.langchain_errors import map_openai_exception
    from eic.core.dependency_controls import DependencyDenied, guard
    from eic.core.security import encrypt_secret
    if not model.tenant_id:
        raise ModelProviderError('探测缺少租户上下文。', reason='invalid_provider_configuration', retryable=False)
    # 临时模型不写 session，不读取已保存凭据，也不修改调用方对象。
    from eic.models import AIModel
    probe = AIModel(**{name: getattr(model, name) for name in (
        'id', 'tenant_id', 'provider', 'model_type', 'endpoint', 'model_name',
        'embedding_dimensions', 'max_input_tokens', 'max_output_tokens', 'chat_parameter_profile', 'reasoning_policy')})
    probe.secret_encrypted = encrypt_secret(secret_override) if secret_override else None
    probe.enabled = True
    kind = (model.model_type or '').strip().lower()
    if kind not in {'chat', 'embedding', '2', 'rerank', '4'}:
        raise ModelProviderError('模型类型不支持连接探测。', reason='unsupported_model_type', retryable=False)
    if kind in {'rerank', '4'} and (model.provider or '').strip().lower() not in {
        'openai', 'openai_compatible', '1', 'bge_rerank', '8', 'dashscope', '12',
        'dashscope_rerank', 'aliyun_rerank', 'gte-rerank-v2',
    }:
        # 与聊天、向量探测一致，未知供应商在任何传输前拒绝，不能隐式猜测协议。
        raise ModelProviderError('模型供应商不支持重排探测。', reason='unsupported_rerank_provider', retryable=False)
    if client is not None and client.is_closed:
        raise ModelProviderError('探测客户端已关闭。', reason='invalid_provider_configuration', retryable=False)
    if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 16000:
        raise ModelProviderError('测试文本不能为空或超过 16000 字符。', reason='invalid_test_prompt', retryable=False)
    if type(timeout_seconds) is not int or not 10 <= timeout_seconds <= 300:
        raise ModelProviderError('测试超时必须为 10–300 秒。', reason='invalid_test_timeout', retryable=False)
    active = _isolated_async_client(client, httpx.Timeout(timeout_seconds, connect=8))
    try:
        if kind in {'embedding', '2'}:
            from eic.ai.embeddings import EICEmbeddings
            probe.model_type = 'embedding'
            await EICEmbeddings(probe, tenant_id=model.tenant_id, client=active).aembed_query('ping')
            return {'reply': '', 'message': '向量化探测完成', 'finishReason': 'completed', 'usage': {}}
        elif kind in {'rerank', '4'}:
            from eic.ai.rerank import rerank_hits
            from eic.vector.qdrant_reader import VectorHit
            await rerank_hits(probe, 'ping', [VectorHit('probe', 0., {'content':'pong'})],
                              limit=1, client=active, secret_override=secret_override)
            return {'reply': '', 'message': '重排探测完成', 'finishReason': 'completed', 'usage': {}}
        else:
            async with guard('chat', model.tenant_id, provider=model.provider, model_id=model.id,
                             endpoint=model.endpoint,
                             admission_lifetime=timeout_seconds + 30,
                             reserve_units=message_units([{'role':'user','content':prompt}], None)
                                           + chat_parameters(probe)[1]['outputLimit']):
                return await _chat(probe, secret_override, active, prompt, timeout_seconds)
    except (httpx.TimeoutException, asyncio.TimeoutError):
        raise ModelProviderError('模型测试超时；未自动重发，请核对上游状态后手动重试。',
                                 reason='provider_timeout', retryable=False) from None
    except (ChatConfigurationError, DependencyDenied) as exc:
        raise ModelProviderError('模型探测配置或依赖保护拒绝请求。', reason=getattr(exc,'reason',getattr(exc,'code','provider_error')), retryable=False) from None
    except Exception as exc:
        from openai import APITimeoutError
        if isinstance(exc, APITimeoutError):
            raise ModelProviderError('模型测试超时；未自动重发，请核对上游状态后手动重试。',
                                     reason='provider_timeout', retryable=False) from None
        raise map_openai_exception(exc) from None
    finally:
        await complete_cleanup(active.aclose())
