"""ChatOpenAI 流到 EIC 字符串与工具调用契约的隔离适配。"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any
from urllib.parse import urlsplit, urlunsplit, parse_qsl

import httpx
from cryptography.fernet import InvalidToken
from langchain_core.messages import ToolMessage, convert_to_messages
from langchain_core.outputs import ChatGenerationChunk
from langchain_openai import ChatOpenAI, AzureChatOpenAI
from openai._streaming import ServerSentEvent
from openai import Omit, AsyncOpenAI, AsyncAzureOpenAI
from eic.ai.stream_reader import complete_cleanup
from eic.ai.tool_arguments import (
    MAX_TOOL_ARGUMENT_BYTES, MAX_TOOL_ROUND_BYTES, MAX_TOOL_CALLS,
    ToolArgumentsError, parse_tool_arguments, tool_argument_text_size,
)

from eic.ai.budget import ChatConfigurationError
from eic.ai.business_prompts import format_conversation
from eic.ai.response_contract import (
    ResponseContractError, parse_response, response_format, validate_response_schema,
)
from eic.ai.langchain_errors import map_langchain_stream_errors
from eic.ai.providers import (
    CONNECT_TIMEOUT_SECONDS,
    READ_TIMEOUT_SECONDS,
    ModelProviderError,
    VisibleStreamGate,
    _split_think_content,
    _text_from_content,
    chat_parameters,
    message_units,
)
from eic.core.observability import observe_model_stream, usage_fields
from eic.core.security import decrypt_secret
from eic.models import AIModel


class _EICChatOpenAI(ChatOpenAI):
    """保留 ChatOpenAI 归一化前的 usage，避免把 SDK 补值当供应商实报。"""

    eic_output_field: str = 'max_completion_tokens'
    eic_parameters: dict = {}

    def _get_request_payload(self, *args, **kwargs):
        payload = super()._get_request_payload(*args, **kwargs)
        if self.eic_output_field == 'max_tokens' and 'max_completion_tokens' in payload:
            payload['max_tokens'] = payload.pop('max_completion_tokens')
        payload.pop('temperature', None)
        payload.update(self.eic_parameters)
        if 'thinking' in payload:
            payload['extra_body'] = {**(payload.get('extra_body') or {}), 'thinking': payload.pop('thinking')}
        return payload



    def _convert_chunk_to_generation_chunk(
        self,
        chunk: dict,
        default_chunk_class: type,
        base_generation_info: dict | None,
    ) -> ChatGenerationChunk | None:
        chunk = dict(chunk)
        if not chunk.get('choices') and isinstance(chunk.get('output'), dict):
            chunk['choices'] = chunk['output'].get('choices', [])
        if chunk.get('choices'):
            chunk['choices'] = [{**choice, 'index': choice.get('index', index),
                'delta': choice.get('delta') or choice.get('message') or {}}
                for index, choice in enumerate(chunk['choices'])]
        generation = super()._convert_chunk_to_generation_chunk(
            chunk, default_chunk_class, base_generation_info
        )
        raw_usage = chunk.get("usage") if isinstance(chunk, dict) else None
        if generation is not None:
            if isinstance(raw_usage, dict):
                generation.generation_info = {
                    **(generation.generation_info or {}),
                    "eic_provider_usage_present": True,
                    "eic_provider_usage": dict(raw_usage),
                }
            choices = chunk.get("choices", []) if isinstance(chunk, dict) else []
            if not choices and isinstance(chunk, dict):
                nested = chunk.get("chunk")
                choices = nested.get("choices", []) if isinstance(nested, dict) else []
            if choices and isinstance(choices[0], dict):
                delta = choices[0].get("delta")
                reasoning = delta.get("reasoning_content") if isinstance(delta, dict) else None
                if reasoning is not None:
                    generation.message.additional_kwargs["reasoning_content"] = reasoning
        return generation


class _EICAzureChatOpenAI(_EICChatOpenAI, AzureChatOpenAI):
    """仅共享 EIC 参数及原始分片契约，Azure 派发仍由官方 SDK 负责。"""
    eic_output_field: str = 'max_completion_tokens'
    eic_parameters: dict = {}


def _azure_configuration(endpoint: str) -> dict | None:
    parts = urlsplit(endpoint)
    path = parts.path.rstrip('/')
    marker = '/openai/deployments/'
    # 旧 Azure 标签允许显式兼容基址；只有 Azure 原生路径才要求 deployment/version。
    if '/openai' not in path and 'api-version' not in _endpoint_query(endpoint):
        return None
    if marker not in path or not path.endswith('/chat/completions'):
        raise ModelProviderError('Azure 地址必须包含 deployment 和 chat/completions。',
                                 reason='invalid_provider_configuration', retryable=False)
    prefix, deployment = path.split(marker, 1)
    deployment = deployment.removesuffix('/chat/completions')
    query = _endpoint_query(endpoint)
    version = query.pop('api-version', None)
    if not deployment or '/' in deployment or not isinstance(version, str) or not version:
        raise ModelProviderError('Azure 必须配置唯一 api-version 和 deployment。',
                                 reason='invalid_provider_configuration', retryable=False)
    return {'azure_endpoint': urlunsplit((parts.scheme, parts.netloc, prefix, '', '')),
            'azure_deployment': deployment, 'api_version': version}


def _chat_completions_base_url(endpoint: str) -> str:
    parts = urlsplit(endpoint.strip())
    if (parts.scheme not in {"http", "https"} or not parts.netloc or parts.username
            or parts.password or parts.fragment):
        raise ModelProviderError(
            "模型服务地址配置无效。", reason="invalid_provider_endpoint", retryable=False
        )
    path = parts.path.rstrip("/")
    suffix = "/chat/completions"
    if path.lower().endswith(suffix):
        path = path[:-len(suffix)].rstrip("/")
    return urlunsplit((parts.scheme, parts.netloc, path or "", "", ""))


class _CompatibleDecoder:
    """委托 SDK SSE 解码，只补齐旧公开入口明确的 DONE 终态。"""
    def __init__(self, decoder):
        self.decoder = decoder

    async def aiter_bytes(self, source):
        terminal = False
        async for event in self.decoder.aiter_bytes(source):
            if event.data == '[DONE]':
                if not terminal:
                    yield ServerSentEvent(data=json.dumps({'choices': [
                        {'index': 0, 'delta': {}, 'finish_reason': 'stop'}]}))
            else:
                value = event.json()
                if value.get('error'):
                    raise ModelProviderError('模型服务返回错误。',
                                             reason='provider_error', retryable=False)
                choices = value.get('choices') or value.get('output', {}).get('choices', [])
                terminal = terminal or any(c.get('finish_reason') for c in choices)
            yield event


class _CompatibleCompletions:
    def __init__(self, resource, anonymous=False):
        self.resource = resource
        self.anonymous = anonymous

    async def create(self, **kwargs):
        if self.anonymous:
            kwargs['extra_headers'] = {**kwargs.get('extra_headers', {}), 'Authorization': Omit()}
        response = await self.resource.create(**kwargs)
        response._decoder = _CompatibleDecoder(response._decoder)
        return response


def _endpoint_query(endpoint):
    query = {}
    for key, value in parse_qsl(urlsplit(endpoint).query, keep_blank_values=True):
        if key not in query:
            query[key] = value
        else:
            previous = query[key]
            query[key] = [*previous, value] if isinstance(previous, list) else [previous, value]
    return query


def _bounded_decoded_tool_arguments(arguments: Any, *, reason: str) -> None:
    # SDK 已解码对象仍须在本地摘要/复制之前受限；不承诺覆盖 SDK 内部分配。
    from eic.ai.declarative_skills import validate_skill_arguments
    from eic.ai.tool_center import ToolExecutionError

    try:
        validate_skill_arguments(arguments)
    except ToolExecutionError:
        raise ModelProviderError(
            "模型工具参数对象无效或超过本地限制。", reason=reason, retryable=False
        ) from None


def _langchain_messages(messages: list[dict[str, Any]]) -> list:
    normalized: list[dict[str, Any]] = []
    for original in messages:
        if not isinstance(original, dict):
            raise ModelProviderError(
                "对话消息格式无效。", reason="invalid_model_messages", retryable=False
            )
        item = dict(original)
        if item.get("content") is None:
            item["content"] = ""
        if item.get("role") == "assistant" and isinstance(item.get("tool_calls"), list):
            calls: list[dict[str, Any]] = []
            for call in item["tool_calls"]:
                if not isinstance(call, dict) or not isinstance(call.get("function"), dict):
                    raise ModelProviderError(
                        "历史工具调用格式无效。", reason="invalid_model_messages", retryable=False
                    )
                function = call["function"]
                arguments = function.get("arguments", {})
                if isinstance(arguments, str):
                    try:
                        arguments = parse_tool_arguments(arguments)
                    except ToolArgumentsError:
                        raise ModelProviderError(
                            "历史工具调用参数格式无效。",
                            reason="invalid_model_messages",
                            retryable=False,
                        ) from None
                if not isinstance(arguments, dict):
                    raise ModelProviderError(
                        "历史工具调用参数格式无效。", reason="invalid_model_messages", retryable=False
                    )
                _bounded_decoded_tool_arguments(arguments, reason="invalid_model_messages")
                calls.append({
                    "name": function.get("name"),
                    "args": arguments,
                    "id": call.get("id"),
                    "type": "tool_call",
                })
            item["tool_calls"] = calls
        normalized.append(item)
    try:
        return format_conversation(convert_to_messages(normalized))
    except (TypeError, ValueError) as exc:
        raise ModelProviderError(
            "对话消息格式无效。", reason="invalid_model_messages", retryable=False
        ) from exc


class _IsolatedTransport(httpx.AsyncBaseTransport):
    """仅显式模型凭据；借入 client 使用公开 send，不继承其身份或关闭所有权。"""

    def __init__(self, client, secret, *, azure=False, endpoint_query=b''):
        self.client = client
        self.secret = secret
        self.azure = azure
        self.endpoint_query = endpoint_query
        from eic.core.runtime_io import borrowed_http_transport
        self.owned = (borrowed_http_transport() or httpx.AsyncHTTPTransport(trust_env=False, retries=0)) if client is None else None

    async def handle_async_request(self, request):
        if self.client is not None and self.client.is_closed:
            raise ModelProviderError('模型客户端已关闭。',
                                     reason='invalid_provider_configuration', retryable=False)
        headers = dict(request.headers)
        for key in ('authorization', 'api-key', 'cookie', 'openai-organization', 'openai-project'):
            headers.pop(key, None)
        if self.secret and not getattr(self, 'anthropic', False):
            headers['api-key' if self.azure else 'authorization'] = (
                self.secret if self.azure else f'Bearer {self.secret}')
        # SDK 负责路径及协议；配置 query 保留原字节，避免 dict 聚合重复键。
        url = request.url.copy_with(query=self.endpoint_query or None)
        isolated = httpx.Request(request.method, url, headers=headers,
                                 content=await request.aread(), extensions=request.extensions)
        if self.client is not None:
            return await self.client.send(isolated, stream=True, auth=None, follow_redirects=False)
        return await self.owned.handle_async_request(isolated)

    async def aclose(self):
        if self.owned is not None:
            await self.owned.aclose()


async def _close_openai_resources(source, async_client, sync_client):
    failed = []
    cancelled = False
    for resource in (source, async_client):
        if resource is not None:
            try:
                await complete_cleanup(resource.aclose())
            except BaseException as exc:
                if isinstance(exc, asyncio.CancelledError):
                    cancelled = True
                else:
                    failed.append(exc)
    if sync_client is not None:
        try:
            sync_client.close()
        except BaseException as exc:
            failed.append(exc)
    if cancelled:
        raise asyncio.CancelledError
    if failed:
        raise ModelProviderError('模型资源清理失败。', reason='provider_cleanup_failed', retryable=False) from None


def _record_tool_deltas(
    collected: dict[int, dict[str, Any]], chunks: object
) -> None:
    if not isinstance(chunks, list):
        return
    for item in chunks:
        if not isinstance(item, dict):
            raise ModelProviderError(
                "模型工具调用分片格式无效。", reason="provider_outcome_uncertain", retryable=False
            )
        index = item.get("index")
        if isinstance(index, bool) or not isinstance(index, int) or index < 0:
            raise ModelProviderError(
                "模型工具调用缺少有效索引。", reason="provider_outcome_uncertain", retryable=False
            )
        if index not in collected and len(collected) >= MAX_TOOL_CALLS:
            raise ModelProviderError(
                "模型工具调用数量超过本地限制。", reason="provider_outcome_uncertain", retryable=False
            )
        current = collected.setdefault(index, {"id": "", "name": "", "arguments": "", "_argument_bytes": 0})
        call_id = item.get("id")
        if call_id:
            call_id = str(call_id)
            if current["id"] and current["id"] != call_id:
                raise ModelProviderError(
                    "模型工具调用标识不一致。", reason="provider_outcome_uncertain", retryable=False
                )
            current["id"] = call_id
        name = item.get("name")
        if name:
            current["name"] += str(name)
        arguments = item.get("args")
        if isinstance(arguments, str):
            try:
                size = tool_argument_text_size(arguments)
            except ToolArgumentsError:
                raise ModelProviderError(
                    "模型工具参数超过本地限制。", reason="provider_outcome_uncertain", retryable=False
                ) from None
            if (current["_argument_bytes"] + size > MAX_TOOL_ARGUMENT_BYTES
                    or sum(call["_argument_bytes"] for call in collected.values()) + size > MAX_TOOL_ROUND_BYTES):
                raise ModelProviderError(
                    "模型工具参数累计超过本地限制。", reason="provider_outcome_uncertain", retryable=False
                )
            current["_argument_bytes"] += size
            current["arguments"] += arguments
        elif arguments is not None:
            raise ModelProviderError(
                "模型工具调用参数分片格式无效。",
                reason="provider_outcome_uncertain",
                retryable=False,
            )


def _complete_tool_calls(collected: dict[int, dict[str, Any]]) -> list[dict[str, object]]:
    if not collected:
        raise ModelProviderError(
            "模型声明了工具调用，但未返回完整调用内容。",
            reason="provider_outcome_uncertain",
            retryable=False,
        )
    calls: list[dict[str, object]] = []
    seen_ids: set[str] = set()
    for index in sorted(collected):
        item = collected[index]
        call_id, name, arguments = item["id"], item["name"], item["arguments"]
        if not call_id or not name or not arguments or call_id in seen_ids:
            raise ModelProviderError(
                "模型工具调用不完整或标识重复。",
                reason="provider_outcome_uncertain",
                retryable=False,
            )
        try:
            parsed = parse_tool_arguments(arguments)
        except ToolArgumentsError:
            raise ModelProviderError(
                "模型工具调用参数不完整。",
                reason="provider_outcome_uncertain",
                retryable=False,
            ) from None
        if not isinstance(parsed, dict):
            raise ModelProviderError(
                "模型工具调用参数格式无效。",
                reason="provider_outcome_uncertain",
                retryable=False,
            )
        seen_ids.add(call_id)
        calls.append({
            "id": call_id,
            "type": "function",
            "function": {"name": name, "arguments": arguments},
        })
    return calls


def _raw_usage(chunk: Any) -> dict[str, Any] | None:
    metadata = getattr(chunk, "response_metadata", None)
    if not isinstance(metadata, dict):
        return None
    if metadata.get("eic_provider_usage_present") is True:
        usage = metadata.get("eic_provider_usage")
        return usage if isinstance(usage, dict) else {}
    for key in ("eic_provider_usage", "token_usage", "usage"):
        if key in metadata:
            usage = metadata[key]
            return usage if isinstance(usage, dict) else None
    return None


def _tool_choice_requires_call(tool_choice: object | None) -> bool:
    if tool_choice is True:
        return True
    if isinstance(tool_choice, str):
        normalized = tool_choice.strip().lower()
        return bool(normalized) and normalized not in {"auto", "none"}
    if isinstance(tool_choice, dict):
        choice_type = tool_choice.get("type")
        if choice_type == "auto" or choice_type == "none":
            return False
        function = tool_choice.get("function")
        return choice_type == "function" or (
            isinstance(function, dict) and bool(function.get("name"))
        )
    return False


def _tool_choice_function_name(tool_choice: object | None) -> str | None:
    if isinstance(tool_choice, str):
        normalized = tool_choice.strip().lower()
        return tool_choice.strip() if normalized not in {"", "auto", "none", "required", "any"} else None
    if isinstance(tool_choice, dict):
        function = tool_choice.get("function")
        if isinstance(function, dict):
            name = function.get("name")
            return name if isinstance(name, str) and name else None
    return None


def _declared_tool_names(tools: list[dict[str, object]] | None) -> set[str]:
    names: set[str] = set()
    for tool in tools or []:
        function = tool.get("function") if isinstance(tool, dict) else None
        name = function.get("name") if isinstance(function, dict) else None
        if isinstance(name, str) and name:
            names.add(name)
    return names


def _validate_tool_call_names(
    calls: list[dict[str, object]],
    declared_names: set[str],
    forced_name: str | None,
) -> None:
    for call in calls:
        function = call.get("function")
        name = function.get("name") if isinstance(function, dict) else None
        if name not in declared_names or (forced_name is not None and name != forced_name):
            raise ModelProviderError(
                "模型返回了未请求或未声明的工具调用。",
                reason="provider_outcome_uncertain",
                retryable=False,
            )


async def _stream_ollama(model, messages, *, client, reasoning_sink, temperature,
                         tools, tool_choice, tool_call_sink, usage_sink, model_tools=None, reasoning_selection=None):
    from langchain_ollama import ChatOllama
    from ollama import AsyncClient, ChatResponse, Message, ResponseError
    class CompatibleChatOllama(ChatOllama):
        def _convert_messages_to_ollama_messages(self, messages):
            converted = super()._convert_messages_to_ollama_messages(messages)
            for message, wire in zip(messages, converted, strict=True):
                if isinstance(message, ToolMessage):
                    # ID 留在标准 ToolMessage 内；Ollama 原生 schema 只接受 tool_name。
                    wire.pop('tool_call_id', None)
                    if message.name:
                        wire['tool_name'] = message.name
            return converted

        def _chat_params(self, *args, **kwargs):
            params = super()._chat_params(*args, **kwargs)
            if 'think' in parameters:
                params['think'] = parameters['think']
            else:
                params.pop('think', None)
            return params
    class CompatibleMessage(Message):
        role: str = 'assistant'
    class CompatibleResponse(ChatResponse):
        message: CompatibleMessage
    class CompatibleClient(AsyncClient):
        async def _request(self, cls, *args, **kwargs):
            return await super()._request(
                CompatibleResponse if cls is ChatResponse else cls, *args, **kwargs)
    parameters, profile = chat_parameters(model, temperature, reasoning_selection=reasoning_selection)
    if not messages or message_units(messages, tools) > profile['inputLimit']:
        raise ModelProviderError('模型输入预算超限。', reason='input_budget_exceeded', retryable=False)
    base_url = _chat_completions_base_url(model.endpoint)
    if urlsplit(model.endpoint).query:
        raise ModelProviderError('Ollama 地址暂不支持 query，请配置无 query 的服务地址。',
                                 reason='invalid_provider_endpoint', retryable=False)
    if base_url.endswith('/api/chat'):
        base_url = base_url[:-len('/api/chat')]
    secret = decrypt_secret(model.secret_encrypted) if model.secret_encrypted else ''
    kwargs = {'timeout': httpx.Timeout(READ_TIMEOUT_SECONDS, connect=CONNECT_TIMEOUT_SECONDS),
              'headers': {'Authorization': f'Bearer {secret}'} if secret else {},
              'follow_redirects': False, 'trust_env': False}
    chat = None
    replacement = None
    collected = {}
    call_signatures = set()
    argument_bytes = 0
    terminal = False
    received = False
    source = None
    try:
        chat = CompatibleChatOllama(model=model.model_name, base_url=base_url, reasoning=True,
                          client_kwargs=kwargs,
                          async_client_kwargs={'transport': _IsolatedTransport(client, secret)},
                          validate_model_on_init=False)
        replacement = CompatibleClient(host=base_url, **kwargs,
                                       transport=_IsolatedTransport(client, secret))
        await complete_cleanup(chat._async_client._client.aclose())
        chat._async_client = replacement
        runnable = chat.bind_tools(model_tools if model_tools is not None else tools) if tools and tool_choice != 'none' else chat
        if usage_sink is not None:
            usage_sink['_invoked'] = True
        source = runnable.astream(_langchain_messages(messages), options=parameters['options'])
        async for chunk in source:
            received = True
            metadata = chunk.response_metadata
            if terminal and (chunk.tool_calls or chunk.content):
                raise ModelProviderError('模型终态后仍返回调用。', reason='provider_outcome_uncertain', retryable=False)
            if usage_sink is not None:
                usage_sink.update(usage_fields(metadata, ollama=True))
            if metadata.get('done'):
                terminal = True
                if metadata.get('done_reason') == 'length':
                    raise ModelProviderError('模型输出达到长度上限。', reason='output_budget_exceeded', retryable=False)
            if reasoning_sink is not None and chunk.additional_kwargs.get('reasoning_content'):
                reasoning_sink.append(chunk.additional_kwargs['reasoning_content'])
            if len(chunk.tool_calls) + len(collected) > MAX_TOOL_CALLS:
                raise ModelProviderError('模型工具调用数量超过本地限制。',
                                         reason='provider_outcome_uncertain', retryable=False)
            signatures = set()
            for call in chunk.tool_calls:
                if (not isinstance(call, dict) or not isinstance(call.get('name'), str)
                        or not 0 < len(call['name']) <= 200
                        or not isinstance(call.get('id'), str) or not 0 < len(call['id']) <= 120):
                    raise ModelProviderError('模型工具调用标识无效。',
                                             reason='provider_outcome_uncertain', retryable=False)
                _bounded_decoded_tool_arguments(call.get('args'), reason='provider_outcome_uncertain')
                try:
                    size = tool_argument_text_size(json.dumps(call['args'], ensure_ascii=False))
                except ToolArgumentsError:
                    raise ModelProviderError('模型工具参数超过本地限制。',
                                             reason='provider_outcome_uncertain', retryable=False) from None
                argument_bytes += size
                if argument_bytes > MAX_TOOL_ROUND_BYTES:
                    raise ModelProviderError('模型工具参数累计超过本地限制。',
                                             reason='provider_outcome_uncertain', retryable=False)
                signature = json.dumps([call['name'], call['args']], sort_keys=True, ensure_ascii=False)
                if signature in signatures:
                    raise ModelProviderError('模型重复返回工具响应，结果不确定。',
                                             reason='provider_outcome_uncertain', retryable=False)
                signatures.add(signature)
            if signatures:
                # SDK 每个 raw call 生成 UUID；重复响应无法区分新调用，整轮拒绝，禁止重复派发。
                if call_signatures.intersection(signatures):
                    raise ModelProviderError('模型重复返回工具响应，结果不确定。',
                                             reason='provider_outcome_uncertain', retryable=False)
                call_signatures.update(signatures)
            for call in chunk.tool_calls:
                call_id = call.get('id')
                if not isinstance(call_id, str) or not call_id or not isinstance(call.get('args'), dict):
                    raise ModelProviderError('工具调用标识或参数无效。', reason='provider_outcome_uncertain', retryable=False)
                item = {'id': call_id, 'type': 'function', 'function': {
                    'name': call['name'], 'arguments': dict(call['args'])}}
                if call_id in collected:
                    raise ModelProviderError('工具调用标识冲突。', reason='provider_outcome_uncertain', retryable=False)
                collected.setdefault(call_id, item)
            visible, nested = _split_think_content(_text_from_content(chunk.content))
            if nested and reasoning_sink is not None:
                reasoning_sink.append(nested)
            if visible:
                yield visible
        if not terminal:
            raise ModelProviderError('模型流缺少终态。', reason='provider_outcome_uncertain', retryable=False)
        completed = list(collected.values())
        if completed:
            if tool_choice == 'none':
                raise ModelProviderError('模型返回未请求的工具。', reason='provider_outcome_uncertain', retryable=False)
            _validate_tool_call_names(completed, _declared_tool_names(tools),
                                      _tool_choice_function_name(tool_choice))
            if tool_call_sink is None:
                raise ModelProviderError('工具调用缺少接收器。', reason='invalid_provider_configuration', retryable=False)
        elif _tool_choice_requires_call(tool_choice):
            raise ModelProviderError('模型未返回强制工具调用。', reason='provider_outcome_uncertain', retryable=False)
    except ResponseError as exc:
        status = exc.status_code
        raise ModelProviderError('模型服务拒绝请求。', reason='provider_rate_limited' if status == 429
            else 'provider_parameters_rejected' if status in {400,422}
            else 'provider_outcome_uncertain' if status >= 500 else 'provider_error',
            retryable=status == 429 and not received) from None
    except (httpx.HTTPError, ConnectionError, ValueError):
        raise ModelProviderError('模型调用结果不确定。', reason='provider_outcome_uncertain', retryable=False) from None
    finally:
        try:
            if source is not None:
                await complete_cleanup(source.aclose())
        finally:
            try:
                if replacement is not None:
                    await complete_cleanup(replacement._client.aclose())
            finally:
                if chat is not None:
                    try:
                        await complete_cleanup(chat._async_client._client.aclose())
                    finally:
                        chat._client._client.close()
    # 仅在所有清理成功且未取消时发布；任何异常都保留调用方 sink 原值。
    if completed:
        tool_call_sink.extend(completed)


@observe_model_stream
async def stream_chat_completion_langchain(
    model: AIModel,
    user_message: str,
    *,
    messages: list[dict[str, Any]] | None = None,
    client: httpx.AsyncClient | None = None,
    reasoning_sink: list[str] | None = None,
    temperature: float | None = None,
    tools: list[dict[str, object]] | None = None,
    tool_choice: object | None = None,
    tool_call_sink: list[dict[str, object]] | None = None,
    usage_sink: dict[str, Any] | None = None,
    compatible_terminal: bool = False,
    response_schema: dict[str, Any] | None = None,
    structured_sink: dict[str, Any] | None = None,
    model_tools: list[Any] | None = None,
    reasoning_selection: dict | None = None,
    protocol_continuation: dict | None = None,
    native_search: bool = False,
    citation_sink: list[dict] | None = None,
) -> AsyncIterator[str]:
    """通过 ChatOpenAI 生成字符串增量，只在完整终态后提交工具调用。"""
    normalized_messages = messages if messages is not None else [{"role": "user", "content": user_message}]
    if usage_sink is not None:
        usage_sink.clear()
    schema = None
    structured_format = None
    try:
        schema = validate_response_schema(response_schema)
        if schema is not None:
            # 不把业务工具静默丢弃，也不通过额外模型轮次补结构化结果。
            if tools or tool_choice not in (None, "none"):
                raise ResponseContractError("structured_response_configuration_conflict")
            structured_format = response_format(schema)
        requires_tool_call = _tool_choice_requires_call(tool_choice)
        forced_tool_name = _tool_choice_function_name(tool_choice)
        declared_tool_names = _declared_tool_names(tools)
        if requires_tool_call and not tools:
            raise ModelProviderError(
                "强制工具调用必须提供工具定义。",
                reason="invalid_provider_configuration",
                retryable=False,
            )
        if forced_tool_name is not None and forced_tool_name not in declared_tool_names:
            raise ModelProviderError(
                "强制工具选择与工具定义不匹配。",
                reason="invalid_provider_configuration",
                retryable=False,
            )
        if requires_tool_call and tool_call_sink is None:
            raise ModelProviderError(
                "强制工具调用必须提供结果接收器。",
                reason="invalid_provider_configuration",
                retryable=False,
            )
        provider = model.provider.strip().lower()
        if native_search and provider != 'openai_responses':
            raise ModelProviderError('当前协议不支持原生联网', reason='search_native_unsupported', retryable=False)
        if protocol_continuation and protocol_continuation.get('rounds') and provider != 'anthropic':
            raise ModelProviderError('思考工具回合不能切换模型或协议配置',
                                     reason='thinking_continuation_changed', retryable=False)
        if provider in {'ollama', '10'}:
            if schema is not None:
                raise ResponseContractError("unsupported_structured_response_provider")
            chat_parameters(model, temperature, reasoning_selection=reasoning_selection)
            source = _stream_ollama(model, normalized_messages, client=client,
                reasoning_sink=reasoning_sink, temperature=temperature, tools=tools,
                tool_choice=tool_choice, tool_call_sink=tool_call_sink, usage_sink=usage_sink,
                model_tools=model_tools, reasoning_selection=reasoning_selection)
            try:
                async for chunk in source:
                    yield chunk
            finally:
                await complete_cleanup(source.aclose())
            return
        if provider not in {"openai", "1", "openai_compatible", 'openai_responses', 'anthropic', 'azure', '2',
                            'zhipu', '3', 'dashscope', '12'}:
            raise ModelProviderError(
                "该供应商尚未接入 ChatOpenAI 适配器。",
                reason="unsupported_chat_provider",
                retryable=False,
            )
        parameters, profile = chat_parameters(model, temperature, reasoning_selection=reasoning_selection)
        if provider == 'anthropic':
            from eic.ai.protocol_bridge import continuation_usage
            private_usage = continuation_usage(model, normalized_messages, parameters,
                                               tools, tool_choice, protocol_continuation)
            if private_usage['inputEstimate'] > profile['inputLimit']:
                raise ModelProviderError('完整思考续轮超出输入预算',
                                         reason='input_budget_exceeded', retryable=False)
        if profile["profile"] == "ollama":
            raise ModelProviderError(
                "ChatOpenAI 适配器不处理 Ollama。",
                reason="unsupported_chat_provider",
                retryable=False,
            )
        schema_reserve = (len(json.dumps(structured_format, ensure_ascii=False).encode("utf-8"))
                          if structured_format is not None else 0)
        if (not normalized_messages
                or message_units(normalized_messages, tools) + schema_reserve > profile["inputLimit"]):
            raise ModelProviderError(
                "模型配置或输入预算不支持本次调用。",
                reason="input_budget_exceeded",
                retryable=False,
            )
        secret = decrypt_secret(model.secret_encrypted) if model.secret_encrypted else ""
        if not secret and (not compatible_terminal or schema is not None):
            raise ModelProviderError(
                "模型凭据未配置。", reason="invalid_provider_credentials", retryable=False
            )
        from eic.ai.protocol_bridge import base_url as protocol_base_url
        base_url = (protocol_base_url(model.endpoint) if provider in {'openai_responses', 'anthropic'}
                    else _chat_completions_base_url(model.endpoint))
        azure = _azure_configuration(model.endpoint) if provider in {'azure', '2'} else None
        if azure and not secret:
            raise ModelProviderError('Azure 模型凭据未配置。',
                                     reason='invalid_provider_credentials', retryable=False)
        langchain_messages = _langchain_messages(normalized_messages)
    except ResponseContractError as exc:
        raise ModelProviderError(
            "结构化响应配置无效或不受支持。", reason=str(exc), retryable=False,
        ) from None
    except ModelProviderError:
        raise
    except ChatConfigurationError as exc:
        raise ModelProviderError(
            "模型配置或输入预算不支持本次调用。",
            reason=exc.reason,
            retryable=False,
        ) from None
    except InvalidToken:
        raise ModelProviderError(
            "模型凭据配置无效。", reason="invalid_provider_credentials", retryable=False
        ) from None
    except (TypeError, ValueError):
        raise ModelProviderError(
            "模型配置无效。", reason="invalid_provider_configuration", retryable=False
        ) from None

    active_client = None
    sync_client = None
    source = None
    completed_calls = None
    structured_parts: list[str] = []
    structured_value = None
    try:
        transport = _IsolatedTransport(client, secret, azure=bool(azure),
                                       endpoint_query=httpx.URL(model.endpoint).query)
        if provider in {'openai_responses', 'anthropic'}:
            from eic.ai.protocol_bridge import ProtocolTransport
            transport = ProtocolTransport(transport, provider, secret, protocol_continuation,
                                          native_search=native_search, citation_sink=citation_sink)
        active_client = httpx.AsyncClient(
            transport=transport,
            timeout=httpx.Timeout(READ_TIMEOUT_SECONDS, connect=CONNECT_TIMEOUT_SECONDS),
            trust_env=False, follow_redirects=False)
        sync_client = httpx.Client(trust_env=False, follow_redirects=False)
        try:
            root_parameters = {'api_key': secret}
            if not secret:
                async def anonymous_key():
                    return ''
                root_parameters = {'api_key': anonymous_key, 'default_headers': {'Authorization': Omit()}}
            sdk_configuration = azure or {'base_url': base_url}
            root_class = AsyncAzureOpenAI if azure else AsyncOpenAI
            root = root_class(**root_parameters, **sdk_configuration, max_retries=0,
                              http_client=active_client, organization='', project='')
            chat_class = _EICAzureChatOpenAI if azure else _EICChatOpenAI
            chat = chat_class(
                model=model.model_name,
                api_key=secret if secret else lambda: '',
                **sdk_configuration,
                max_retries=0,
                stream_usage=True,
                use_responses_api=False,
                http_async_client=active_client,
                http_client=sync_client,
                root_async_client=root,
                async_client=root.chat.completions,
                eic_output_field=profile['outputField'],
                eic_parameters=parameters,
                **{key: value for key, value in parameters.items() if key != 'thinking'},
            )
            if compatible_terminal and schema is None:
                chat.async_client = _CompatibleCompletions(chat.async_client, anonymous=not secret)
            # 公开extra_body保留标准create流；顶层response_format会切换beta事件流。
            runnable = (chat.bind(extra_body={"response_format": structured_format})
                        if structured_format is not None else
                        chat.bind_tools(model_tools if model_tools is not None else tools,
                                        tool_choice=tool_choice) if tools else chat)
        except (TypeError, ValueError):
            raise ModelProviderError(
                "ChatOpenAI 模型或工具配置无效。",
                reason="invalid_provider_configuration",
                retryable=False,
            ) from None
        gate = VisibleStreamGate(user_message)
        collected: dict[int, dict[str, Any]] = {}
        terminal_reason: str | None = None
        pending_visible: list[str] = []
        received_chunk = False
        if usage_sink is not None:
            usage_sink["_invoked"] = True
        source = map_langchain_stream_errors(runnable.astream(langchain_messages))
        try:
            async for chunk in source:
                received_chunk = True
                additional = getattr(chunk, "additional_kwargs", None)
                if schema is not None and isinstance(additional, dict) and additional.get("refusal"):
                    raise ModelProviderError(
                        "模型拒绝生成结构化响应。",
                        reason="structured_response_refused", retryable=False,
                    )
                usage = _raw_usage(chunk)
                response_metadata = getattr(chunk, "response_metadata", None)
                generation_info = getattr(chunk, "generation_info", None)
                finish_reason = (
                    response_metadata.get("finish_reason")
                    if isinstance(response_metadata, dict)
                    else None
                )
                if finish_reason is None and isinstance(generation_info, dict):
                    finish_reason = generation_info.get("finish_reason")
                if finish_reason:
                    finish_reason = str(finish_reason)

                if terminal_reason is not None:
                    additional = getattr(chunk, "additional_kwargs", None)
                    post_terminal_reasoning = False
                    if isinstance(additional, dict):
                        post_terminal_reasoning = any(
                            _text_from_content(additional.get(key))
                            for key in ("reasoning_content", "reasoning", "thinking")
                        )
                    if (
                        _text_from_content(getattr(chunk, "content", ""))
                        or getattr(chunk, "tool_call_chunks", None)
                        or post_terminal_reasoning
                    ):
                        raise ModelProviderError(
                            "模型流在终止分片后仍返回内容。",
                            reason="provider_outcome_uncertain",
                            retryable=False,
                        )
                    if usage_sink is not None and usage is not None:
                        usage_sink.update(usage_fields({"usage": usage}))
                    if finish_reason and finish_reason != terminal_reason:
                        raise ModelProviderError(
                            "模型流返回冲突的终止原因。",
                            reason="provider_outcome_uncertain",
                            retryable=False,
                        )
                    continue

                if usage_sink is not None and usage is not None:
                    usage_sink.update(usage_fields({"usage": usage}))
                _record_tool_deltas(collected, getattr(chunk, "tool_call_chunks", None))
                additional = getattr(chunk, "additional_kwargs", None)
                if isinstance(additional, dict) and reasoning_sink is not None:
                    reasoning = _text_from_content(
                        additional.get("reasoning_content")
                        or additional.get("reasoning")
                        or additional.get("thinking")
                    )
                    if reasoning:
                        reasoning_sink.append(reasoning)
                raw_content = _text_from_content(getattr(chunk, "content", ""))
                # JSON字符串中的think标签属于业务数据，不能按普通聊天正文裁剪。
                content, nested = ((raw_content, "") if schema is not None
                                   else _split_think_content(raw_content))
                if nested and reasoning_sink is not None:
                    reasoning_sink.append(nested)
                if content:
                    if schema is not None:
                        structured_parts.append(content)
                    else:
                        visible_chunks = gate.push(content)
                        if requires_tool_call:
                            pending_visible.extend(visible_chunks)
                        else:
                            for visible in visible_chunks:
                                yield visible
                if finish_reason:
                    terminal_reason = finish_reason
        except ModelProviderError as exc:
            if terminal_reason is not None or (received_chunk and exc.retryable):
                raise ModelProviderError(
                    "模型流已开始但未能完成，调用结果不确定。",
                    reason="provider_outcome_uncertain",
                    retryable=False,
                ) from None
            raise

        if terminal_reason is None:
            raise ModelProviderError(
                "模型流结束但未提供终止原因。",
                reason="provider_outcome_uncertain",
                retryable=False,
            )
        if terminal_reason == "length":
            raise ModelProviderError(
                "模型输出达到长度上限。", reason="output_budget_exceeded", retryable=False
            )
        if (schema is None and compatible_terminal and terminal_reason == 'tool_calls' and tools == []
                and not collected and not requires_tool_call and forced_tool_name is None):
            terminal_reason = 'stop'
        if terminal_reason == "tool_calls":
            if not tools or tool_choice == "none":
                raise ModelProviderError(
                    "模型返回了未请求的工具调用。",
                    reason=("unsupported_model_finish" if compatible_terminal
                            and tools is None and not collected else "provider_outcome_uncertain"),
                    retryable=False,
                )
            completed_calls = _complete_tool_calls(collected)
            _validate_tool_call_names(completed_calls, declared_tool_names, forced_tool_name)
            if tool_call_sink is None:
                raise ModelProviderError(
                    "工具调用缺少结果接收器。",
                    reason="invalid_provider_configuration",
                    retryable=False,
                )
            final_visible = gate.flush()
            if requires_tool_call:
                pending_visible.extend(final_visible)
            else:
                for visible in final_visible:
                    yield visible
            for visible in pending_visible:
                yield visible
        elif terminal_reason == "stop" and requires_tool_call:
            raise ModelProviderError(
                "模型未按强制工具调用要求返回工具调用。",
                reason="provider_outcome_uncertain",
                retryable=False,
            )
        elif terminal_reason != "stop" or collected:
            raise ModelProviderError(
                "模型未以可接受的终态结束。",
                reason="provider_outcome_uncertain",
                retryable=False,
            )
        else:
            if schema is not None:
                try:
                    structured_value = parse_response("".join(structured_parts), schema)
                except ResponseContractError:
                    raise ModelProviderError(
                        "模型未返回符合业务契约的完整结构化响应。",
                        reason="invalid_structured_response", retryable=False,
                    ) from None
            else:
                for visible in gate.flush():
                    yield visible
    finally:
        await _close_openai_resources(source, active_client, sync_client)
    if completed_calls:
        tool_call_sink.extend(completed_calls)
    # 只有完整终态、Schema验证和资源清理全部成功，才发布正文及结构化值。
    if structured_value is not None:
        if structured_sink is not None:
            structured_sink.clear()
            structured_sink.update(structured_value)
        yield "".join(structured_parts)
