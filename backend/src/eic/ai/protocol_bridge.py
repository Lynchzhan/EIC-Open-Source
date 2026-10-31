"""将 Responses / Messages 的 HTTP 协议适配到现有 LangChain 流契约。"""
import json
from copy import deepcopy
from urllib.parse import urlsplit, urlunsplit

import httpx

from eic.ai.providers import ModelProviderError


def base_url(endpoint):
    parts = urlsplit(endpoint)
    if parts.scheme not in {'http', 'https'} or not parts.hostname or parts.username or parts.password or parts.fragment:
        raise ModelProviderError('模型地址无效', retryable=False)
    path = parts.path.rstrip('/')
    for suffix in ('/chat/completions', '/responses', '/messages', '/models'):
        if path.endswith(suffix):
            path = path[:-len(suffix)]
            break
    if not path:
        path = '/v1'
    return urlunsplit((parts.scheme, parts.netloc, path, parts.query, ''))


def api_url(endpoint, suffix):
    parts = urlsplit(base_url(endpoint))
    return urlunsplit((parts.scheme, parts.netloc, parts.path + suffix, parts.query, ''))


async def fetch_models(endpoint, provider, secret, tenant_id):
    from eic.core.dependency_controls import guard, DependencyDenied
    headers = {'x-api-key': secret, 'anthropic-version': '2023-06-01'} if provider == 'anthropic' else {'Authorization': f'Bearer {secret}'}
    if provider == 'azure':
        headers = {'api-key': secret}
    try:
        url = api_url(endpoint, '/models')
        if provider in {'ollama', 'ollama_embedding'}:
            parts = urlsplit(endpoint)
            path = parts.path.rstrip('/')
            for suffix in ('/api/chat', '/api/embed', '/api/embeddings', '/v1'):
                if path.endswith(suffix):
                    path = path[:-len(suffix)]
                    break
            url = urlunsplit((parts.scheme, parts.netloc, path + '/api/tags', parts.query, ''))
        async with guard('chat', tenant_id, provider=provider, endpoint=endpoint, reserve_units=1):
            async with httpx.AsyncClient(timeout=15, follow_redirects=False, trust_env=False) as client:
                response = await client.get(url, headers=headers)
                response.raise_for_status()
                data = response.json()
        items = data.get('data', data.get('models', []))
        return {'models': sorted({str(i.get('id') or i.get('name')) for i in items
                                 if isinstance(i, dict) and (i.get('id') or i.get('name'))})[:2000]}
    except (httpx.HTTPError, ValueError, TypeError, AttributeError, ModelProviderError, DependencyDenied):
        from fastapi import HTTPException
        raise HTTPException(400, detail={'errMsg': '无法获取上游模型列表；请检查地址、协议和权限，也可手动填写模型'}) from None


def request_body(data, protocol, continuation=None):
    messages = data.get('messages', [])
    limit = data.get('max_completion_tokens', data.get('max_tokens', 8192))
    common = {'model': data['model'], 'stream': data.get('stream', False)}
    if 'temperature' in data:
        common['temperature'] = data['temperature']
    if protocol == 'openai_responses':
        inputs = []
        for message in messages:
            if message['role'] == 'tool':
                inputs.append({'type': 'function_call_output', 'call_id': message['tool_call_id'], 'output': message.get('content', '')})
                continue
            if message.get('content'):
                content = message['content']
                if isinstance(content, list):
                    converted = []
                    for part in content:
                        if part.get('type') == 'image_url':
                            image = part['image_url']
                            converted.append({'type': 'input_image', 'image_url': image['url'] if isinstance(image, dict) else image})
                        else:
                            converted.append({**part, 'type': 'input_text' if part.get('type') == 'text' else part.get('type')})
                    content = converted
                inputs.append({'role': message['role'], 'content': content})
            for call in message.get('tool_calls', []):
                inputs.append({'type': 'function_call', 'call_id': call['id'], **call['function']})
        common.update(input=inputs, max_output_tokens=limit, store=False)
        if "reasoning_effort" in data:
            common["reasoning"] = {"effort": data["reasoning_effort"]}
        if data.get('tools'):
            common['tools'] = [{'type': 'function', **t['function']} for t in data['tools']]
        if data.get('tool_choice') is not None:
            choice = data['tool_choice']
            common['tool_choice'] = {'type': 'function', 'name': choice['function']['name']} if isinstance(choice, dict) else choice
        if data.get('response_format'):
            fmt = data['response_format']
            common['text'] = {'format': {'type': 'json_schema', **fmt['json_schema']} if fmt.get('type') == 'json_schema' else fmt}
        return common
    converted, system = [], []
    for message in messages:
        role = message['role']
        content = message.get('content') or ''
        if role in {'system', 'developer'}:
            system.append(content if isinstance(content, str) else json.dumps(content))
            continue
        if role == 'tool':
            blocks = [{'type': 'tool_result', 'tool_use_id': message['tool_call_id'], 'content': content}]
            role = 'user'
        else:
            blocks = [{'type': 'text', 'text': content}] if isinstance(content, str) and content else content or []
            blocks = list(blocks)
            call_ids = tuple(call['id'] for call in message.get('tool_calls', []))
            if role == 'assistant' and call_ids:
                saved = (continuation or {}).get('rounds', {}).get(call_ids)
                if saved:
                    blocks = deepcopy(saved) + blocks
                elif data.get('thinking', {}).get('type') == 'enabled':
                    raise ModelProviderError('工具续轮缺少完整思考签名，请重新发起请求',
                                             reason='missing_thinking_continuation', retryable=False)
            for index, block in enumerate(blocks):
                if block.get('type') == 'image_url':
                    image = block['image_url']
                    url = image['url'] if isinstance(image, dict) else image
                    if url.startswith('data:'):
                        header, encoded = url.split(',', 1)
                        source = {'type': 'base64', 'media_type': header[5:].split(';')[0], 'data': encoded}
                    else:
                        source = {'type': 'url', 'url': url}
                    blocks[index] = {'type': 'image', 'source': source}
            for call in message.get('tool_calls', []):
                blocks.append({'type': 'tool_use', 'id': call['id'], 'name': call['function']['name'],
                               'input': json.loads(call['function']['arguments'])})
        if converted and converted[-1]['role'] == role:
            converted[-1]['content'].extend(blocks)
        else:
            converted.append({'role': role, 'content': blocks})
    common.update(messages=converted, max_tokens=limit)
    if "thinking" in data:
        common["thinking"] = data["thinking"]
    if system:
        common['system'] = '\n\n'.join(system)
    choice = data.get('tool_choice')
    has_tool_history = any(block.get('type') in {'tool_use', 'tool_result'}
                           for message in converted for block in message['content'])
    if has_tool_history and not data.get('tools'):
        raise ModelProviderError('Messages 工具历史缺少工具定义',
                                 reason='invalid_model_messages', retryable=False)
    # 官方 Messages 支持 none；禁止新调用时仍保留历史所需的工具定义。
    if data.get('tools'):
        common['tools'] = [{'name': t['function']['name'], 'description': t['function'].get('description', ''),
                            'input_schema': t['function']['parameters']} for t in data['tools']]
        if choice:
            common['tool_choice'] = ({'type': 'tool', 'name': choice['function']['name']} if isinstance(choice, dict)
                                    else {'type': {'required': 'any', 'auto': 'auto', 'none': 'none'}[choice]})
    elif choice == 'none':
        common['tool_choice'] = {'type': 'none'}
    if data.get('response_format'):
        fmt = data['response_format']
        if fmt.get('type') != 'json_schema':
            raise ModelProviderError('Messages 结构化输出需要显式 JSON Schema', retryable=False)
        common['output_config'] = {'format': {'type': 'json_schema', 'schema': fmt['json_schema']['schema']}}
    return common


def restore_protocol_continuation(value):
    """仅接受解密 envelope 的私有包；错误信息不包含包内容。"""
    if value is None or value == {}:
        return {}
    try:
        if not isinstance(value, dict) or set(value) != {'version', 'identity', 'thinking', 'rounds'}:
            raise ValueError
        if type(value['version']) is not int or value['version'] != 1:
            raise ValueError
        identity, thinking, entries = value['identity'], value['thinking'], value['rounds']
        if not isinstance(identity, list) or len(identity) != 5 or any(not isinstance(v, str) for v in identity):
            raise ValueError
        if thinking is not None:
            if not isinstance(thinking, dict) or thinking.get('type') not in {'enabled', 'disabled'}:
                raise ValueError
            if thinking['type'] == 'enabled' and (type(thinking.get('budget_tokens')) is not int
                                                or thinking['budget_tokens'] < 1024):
                raise ValueError
        if not isinstance(entries, list):
            raise ValueError
        rounds = {}
        for entry in entries:
            if not isinstance(entry, dict) or set(entry) != {'callIds', 'blocks'}:
                raise ValueError
            ids, blocks = entry['callIds'], entry['blocks']
            if (not isinstance(ids, list) or not ids
                    or any(not isinstance(v, str) or not v for v in ids)
                    or len(set(ids)) != len(ids) or tuple(ids) in rounds or not isinstance(blocks, list)):
                raise ValueError
            for block in blocks:
                if not isinstance(block, dict):
                    raise ValueError
                if block.get('type') == 'thinking':
                    if not isinstance(block.get('thinking'), str) or not isinstance(block.get('signature'), str) or not block['signature']:
                        raise ValueError
                elif block.get('type') == 'redacted_thinking':
                    if not isinstance(block.get('data'), str) or not block['data']:
                        raise ValueError
                else:
                    raise ValueError
            if blocks:
                rounds[tuple(ids)] = deepcopy(blocks)
        # 验证整个包可无损 JSON 编码，不用 default=str 隐式改写签名字段。
        json.dumps(value, ensure_ascii=False, allow_nan=False)
        return {'identity': tuple(identity), 'thinking': deepcopy(thinking), 'rounds': rounds}
    except (ValueError, TypeError, KeyError, OverflowError):
        raise ModelProviderError('加密思考续轮快照格式无效',
                                 reason='invalid_thinking_continuation', retryable=False) from None


def export_protocol_continuation(value):
    """输出仅供 seal 使用的 JSON 安全包，不用于日志或图状态。"""
    if not value:
        return None
    packed = {'version': 1, 'identity': list(value['identity']),
              'thinking': deepcopy(value.get('thinking')),
              'rounds': [{'callIds': list(ids), 'blocks': deepcopy(blocks)}
                         for ids, blocks in value.get('rounds', {}).items()]}
    restore_protocol_continuation(packed)
    return packed


def continuation_usage(model, messages, parameters, tools, tool_choice, continuation=None):
    """校验私有续轮绑定；仅返回可观测的数值，不返回签名正文。"""
    from eic.ai.budget import message_units

    provider = model.provider.strip().lower()
    identity = (str(model.id), provider, model.endpoint, model.model_name,
                json.dumps(model.reasoning_policy, sort_keys=True))
    if continuation and continuation.get('rounds'):
        if (continuation.get('identity') != identity
                or continuation.get('thinking') != parameters.get('thinking')):
            raise ModelProviderError('思考工具回合的模型、协议或预算配置已变化',
                                     reason='thinking_continuation_changed', retryable=False)
    if provider != 'anthropic':
        return {}
    wire = request_body({'model': model.model_name, 'messages': messages,
                         'tools': tools, 'tool_choice': tool_choice, **parameters},
                        provider, continuation)
    wire_messages = wire['messages'] + ([{'role': 'system', 'content': wire['system']}]
                                       if wire.get('system') else [])
    thinking_units = sum(len(json.dumps(block, ensure_ascii=False, separators=(',', ':')).encode('utf-8'))
                         for item in wire['messages'] for block in item['content']
                         if block.get('type') in {'thinking', 'redacted_thinking'})
    if continuation is not None:
        continuation['identity'] = identity
        continuation['thinking'] = deepcopy(parameters.get('thinking'))
    return {'inputEstimate': max(message_units(messages, tools),
                                 message_units(wire_messages, wire.get('tools'))),
            'thinkingBlockUnits': thinking_units}


class EventStream(httpx.AsyncByteStream):
    def __init__(self, response, protocol, continuation=None, thinking_required=False, citation_sink=None):
        self.response, self.protocol = response, protocol
        self.continuation = continuation
        self.thinking_required = thinking_required
        self.citation_sink = citation_sink

    async def __aiter__(self):
        usage, calls, indices, pending_finish = {}, False, {}, None
        thinking_blocks, open_blocks, call_ids = [], {}, []
        async for line in self.response.aiter_lines():
            if not line.startswith('data:'):
                continue
            raw = line[5:].strip()
            if raw == '[DONE]':
                continue
            event = json.loads(raw)
            kind = event.get('type')
            delta, finish = {}, None
            if kind in {'error', 'response.failed', 'response.incomplete'}:
                raise ModelProviderError('上游模型未完成响应', reason='provider_outcome_uncertain', retryable=False)
            if kind == 'response.output_text.delta':
                delta = {'content': event['delta']}
            elif kind == 'response.reasoning_summary_text.delta':
                delta = {'reasoning_content': event['delta']}
            elif kind == 'response.output_item.added' and event['item']['type'] == 'function_call':
                item = event['item']; calls = True
                index = len(indices); indices[event['output_index']] = index
                delta = {'tool_calls': [{'index': index, 'id': item['call_id'], 'type': 'function',
                                        'function': {'name': item['name'], 'arguments': item.get('arguments', '')}}]}
            elif kind == 'response.function_call_arguments.delta':
                delta = {'tool_calls': [{'index': indices[event['output_index']], 'function': {'arguments': event['delta']}}]}
            elif kind == 'response.completed':
                value = event['response']; usage = value.get('usage') or {}
                from eic.ai.search import collect_native_citations
                collect_native_citations(value, self.citation_sink)
                finish = 'tool_calls' if calls else 'stop'
            elif kind == 'message_start':
                usage.update(event.get('message', {}).get('usage') or {})
            elif kind == 'content_block_start' and event['content_block']['type'] == 'tool_use':
                item = event['content_block']; calls = True
                call_ids.append(item['id'])
                delta = {'tool_calls': [{'index': event['index'], 'id': item['id'], 'type': 'function',
                                        'function': {'name': item['name'], 'arguments': ''}}]}
            elif kind == 'content_block_start' and event['content_block']['type'] in {'thinking', 'redacted_thinking'}:
                if event['index'] in open_blocks:
                    raise ModelProviderError('上游思考块索引重复', reason='provider_outcome_uncertain', retryable=False)
                open_blocks[event['index']] = deepcopy(event['content_block'])
            elif kind == 'content_block_stop' and event['index'] in open_blocks:
                block = open_blocks.pop(event['index'])
                if block['type'] == 'thinking' and not block.get('signature'):
                    raise ModelProviderError('上游思考块缺少签名', reason='provider_outcome_uncertain', retryable=False)
                if block['type'] == 'redacted_thinking' and not block.get('data'):
                    raise ModelProviderError('上游隐藏思考块不完整', reason='provider_outcome_uncertain', retryable=False)
                thinking_blocks.append(block)
            elif kind == 'content_block_delta':
                item = event['delta']
                block = open_blocks.get(event['index'])
                if item['type'] in {'thinking_delta', 'signature_delta'} and block is None:
                    raise ModelProviderError('上游思考分片缺少起始块', reason='provider_outcome_uncertain', retryable=False)
                if block is not None and item['type'] in {'thinking_delta', 'signature_delta'}:
                    field = 'thinking' if item['type'] == 'thinking_delta' else 'signature'
                    block[field] = block.get(field, '') + item[field]
                if item['type'] == 'text_delta': delta = {'content': item['text']}
                elif item['type'] == 'thinking_delta': delta = {'reasoning_content': item['thinking']}
                elif item['type'] == 'input_json_delta':
                    delta = {'tool_calls': [{'index': event['index'], 'function': {'arguments': item['partial_json']}}]}
            elif kind == 'message_delta':
                usage.update(event.get('usage') or {})
                stop = event['delta'].get('stop_reason')
                if stop:
                    pending_finish = {'end_turn': 'stop', 'stop_sequence': 'stop', 'tool_use': 'tool_calls', 'max_tokens': 'length'}.get(stop, stop)
            elif kind == 'message_stop':
                if open_blocks:
                    raise ModelProviderError('上游思考块未完成', reason='provider_outcome_uncertain', retryable=False)
                if pending_finish == 'tool_calls' and self.thinking_required and not thinking_blocks:
                    raise ModelProviderError('上游工具回合缺少思考签名块', reason='provider_outcome_uncertain', retryable=False)
                if pending_finish == 'tool_calls' and thinking_blocks and self.continuation is not None:
                    self.continuation.setdefault('rounds', {})[tuple(call_ids)] = thinking_blocks
                finish = pending_finish
            if delta or finish:
                chunk = {'id': 'eic-protocol', 'object': 'chat.completion.chunk', 'created': 0, 'model': '',
                         'choices': [{'index': 0, 'delta': delta, 'finish_reason': finish}]}
                if finish and usage:
                    normalized = {}
                    if 'input_tokens' in usage:
                        prompt = usage['input_tokens']
                        if self.protocol == 'anthropic':
                            prompt += usage.get('cache_read_input_tokens', 0) + usage.get('cache_creation_input_tokens', 0)
                        normalized['prompt_tokens'] = prompt
                    if 'output_tokens' in usage:
                        normalized['completion_tokens'] = usage['output_tokens']
                    if 'total_tokens' in usage:
                        normalized['total_tokens'] = usage['total_tokens']
                    cached = (usage.get('input_tokens_details') or {}).get('cached_tokens', usage.get('cache_read_input_tokens'))
                    if cached is not None:
                        normalized['prompt_tokens_details'] = {'cached_tokens': cached}
                    chunk['usage'] = normalized
                yield ('data: ' + json.dumps(chunk) + '\n\n').encode()

    async def aclose(self):
        await self.response.aclose()


class ProtocolTransport(httpx.AsyncBaseTransport):
    def __init__(self, inner, protocol, secret, continuation=None, *, native_search=False, citation_sink=None):
        self.inner, self.protocol, self.secret = inner, protocol, secret
        self.continuation = continuation
        self.native_search, self.citation_sink = native_search, citation_sink
        inner.anthropic = protocol == 'anthropic'

    async def handle_async_request(self, request):
        if not request.url.path.endswith('/chat/completions'):
            return await self.inner.handle_async_request(request)
        data = json.loads(await request.aread())
        body = request_body(data, self.protocol, self.continuation)
        if self.native_search:
            if self.protocol != 'openai_responses':
                raise ModelProviderError('当前协议不支持原生联网', reason='search_native_unsupported', retryable=False)
            body.setdefault('tools', []).append({'type': 'web_search'})
            if not data.get('tools'):
                body['tool_choice'] = {'type': 'web_search'}
        suffix = '/responses' if self.protocol == 'openai_responses' else '/messages'
        headers = dict(request.headers)
        headers.pop('content-length', None)
        if self.protocol == 'anthropic':
            headers.pop('authorization', None)
            headers.update({'x-api-key': self.secret, 'anthropic-version': '2023-06-01'})
        upstream = httpx.Request('POST', api_url(str(request.url), suffix), headers=headers, json=body)
        upstream.extensions = request.extensions
        response = await self.inner.handle_async_request(upstream)
        if self.native_search and response.status_code in {400, 422}:
            await response.aclose()
            raise ModelProviderError('上游拒绝了原生联网请求', reason='search_native_rejected', retryable=False)
        if response.status_code >= 300:
            return response
        if body['stream']:
            return httpx.Response(200, headers={'content-type': 'text/event-stream'},
                                  stream=EventStream(response, self.protocol, self.continuation,
                                      thinking_required=body.get('thinking', {}).get('type') == 'enabled',
                                      citation_sink=self.citation_sink))
        try:
            value = json.loads(await response.aread())
        finally:
            await response.aclose()
        if self.protocol == 'openai_responses':
            text = ''.join(c.get('text', '') for i in value.get('output', []) for c in i.get('content', [])
                           if c.get('type') == 'output_text')
            if value.get('status') != 'completed':
                exhausted = (value.get('incomplete_details') or {}).get('reason') == 'max_output_tokens'
                raise ModelProviderError('上游输出额度已耗尽，响应未完成；请检查回答 Token 和思考配置。'
                                         if exhausted else '上游响应未完成。',
                                         reason='output_budget_exceeded' if exhausted else 'upstream_response_incomplete',
                                         retryable=False)
        else:
            text = ''.join(c.get('text', '') for c in value.get('content', []))
            stop = value.get('stop_reason')
            if stop == 'max_tokens':
                raise ModelProviderError('上游输出额度已耗尽，响应未完成；请检查回答 Token 和思考配置。',
                                         reason='output_budget_exceeded', retryable=False)
            if stop not in {'end_turn', 'stop_sequence'}:
                raise ModelProviderError('上游响应终态无效。', reason='upstream_response_incomplete', retryable=False)
        usage = value.get('usage') or {}
        normalized = {}
        for upstream, downstream in [('input_tokens', 'prompt_tokens'), ('output_tokens', 'completion_tokens'),
                                     ('total_tokens', 'total_tokens')]:
            if type(usage.get(upstream)) is int and usage[upstream] >= 0:
                normalized[downstream] = usage[upstream]
        return httpx.Response(200, json={'id': value.get('id', 'probe'), 'object': 'chat.completion', 'created': 0,
            'model': value.get('model', data['model']), 'usage': normalized,
            'choices': [{'index': 0, 'message': {'role': 'assistant', 'content': text}, 'finish_reason': 'stop'}]})

    async def aclose(self):
        await self.inner.aclose()
