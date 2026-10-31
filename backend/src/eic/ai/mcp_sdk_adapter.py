"""官方 mcp==1.30.0 transport/session 边界；没有本地 JSON-RPC/SSE parser。

完整公开操作序列在同一 task 内建立并关闭 SDK context，适配原服务没有 close
钩子的调用方式。禁止重发相同 RPC、redirect、异源凭据转发和后台无限 reconnect。
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from contextvars import ContextVar
from datetime import timedelta
from pathlib import Path
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx
import mcp
from httpx_sse import EventSource
from mcp import ClientSession, types
from mcp.client.sse import sse_client
from mcp.client.streamable_http import streamable_http_client

_SDK_LOG_SCOPE = ContextVar('eic_mcp_sdk_log_scope', default=False)
_SDK_SOURCE_ROOT = str(Path(mcp.__file__).parent).replace('\\', '/') + '/'
_SDK_SOURCE_ROOT_RESOLVED = str(Path(mcp.__file__).resolve().parent).replace('\\', '/') + '/'


def _is_sdk_source(path):
    raw = str(path).replace('\\', '/')
    if raw.startswith(_SDK_SOURCE_ROOT):
        return True
    try:
        return str(Path(path).resolve()).replace('\\', '/').startswith(_SDK_SOURCE_ROOT_RESOLVED)
    except (OSError, RuntimeError):
        return False


class _SdkLogBoundary(logging.Filter):
    def filter(self, record):
        if not _SDK_LOG_SCOPE.get():
            return True
        # SDK 会把完整 RPC data、URL 及异常写进 debug/error；仅保留事件层级。
        record.msg, record.args, record.exc_info, record.exc_text = 'MCP SDK event (details omitted)', (), None, None
        return True


class _SdkRootLogBoundary(_SdkLogBoundary):
    def filter(self, record):
        # SDK 接收循环可能在独立 task 中运行，不能依赖调用方 ContextVar。
        # 仅按固定 SDK 源路径脱敏，因此业务代码的 root 日志仍保持原文。
        if not _is_sdk_source(record.pathname):
            return True
        record.msg, record.args, record.exc_info, record.exc_text = 'MCP SDK event (details omitted)', (), None, None
        return True


logging.getLogger().addFilter(_SdkRootLogBoundary())


@asynccontextmanager
async def _sdk_log_scope():
    token = _SDK_LOG_SCOPE.set(True)
    try:
        yield
    finally:
        _SDK_LOG_SCOPE.reset(token)


class _HttpLogBoundary(_SdkLogBoundary):
    def __init__(self, endpoint):
        super().__init__()
        self.endpoint = endpoint

    def filter(self, record):
        if isinstance(record.args, tuple) and any(origin(arg) == origin(self.endpoint) for arg in record.args):
            return super().filter(record)
        return True


for _module in ('mcp.client.sse', 'mcp.client.session', 'mcp.client.streamable_http', 'mcp.shared.session'):
    logging.getLogger(_module).addFilter(_SdkLogBoundary())


class TransportBoundaryError(RuntimeError):
    pass


class StrictClientSession(ClientSession):
    """保留官方typed session，仅禁止官方将数字字符串ID强制匹配整数请求。"""
    def __init__(self, *args, on_invalid_response=None, **kwargs):
        super().__init__(*args, **kwargs)
        self._on_invalid_response = on_invalid_response
        self._protocol_failed = False

    def _normalize_request_id(self, response_id):
        return response_id

    async def _handle_response(self, message):
        # 仅响应进入此hook；合法server request/notification仍由SDK分派。
        root = message.message.root
        if not isinstance(root, (types.JSONRPCResponse, types.JSONRPCError)):
            return await super()._handle_response(message)
        # 官方模型extra=allow保留原额外字段；互斥检查无需读取或重解码wire。
        extras = root.model_extra or {}
        conflicting = (isinstance(root, types.JSONRPCResponse) and 'error' in extras) or (isinstance(root, types.JSONRPCError) and 'result' in extras)
        if self._protocol_failed or conflicting or type(root.id) is not int or root.id not in self._response_streams:
            self._protocol_failed = True
            if self._on_invalid_response is not None:
                self._on_invalid_response()
            # SDK receive loop关闭所有pending请求；边界标志保证映射protocol_error。
            raise TransportBoundaryError('MCP response envelope rejected')
        return await super()._handle_response(message)

    async def send_request(self, *args, **kwargs):
        if self._protocol_failed:
            raise TransportBoundaryError('MCP session protocol failed')
        result = await super().send_request(*args, **kwargs)
        if self._protocol_failed:
            raise TransportBoundaryError('MCP session protocol failed')
        return result


def origin(url):
    try:
        if any(ord(c) <= 32 or ord(c) == 127 for c in str(url)):
            return None
        value = urlsplit(str(url))
        if value.scheme not in {'http', 'https'} or not value.hostname or value.username is not None or value.password is not None or value.fragment:
            return None
        return value.scheme, value.hostname.lower(), value.port or (443 if value.scheme == 'https' else 80)
    except ValueError:
        return None


def canonical_endpoint(url):
    parts = urlsplit(str(url))
    port = parts.port
    default = (parts.scheme == 'https' and port == 443) or (parts.scheme == 'http' and port == 80)
    host = parts.hostname.lower()
    if ':' in host:
        host = f'[{host}]'
    netloc = host if default or port is None else f'{host}:{port}'
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, ''))


class BoundaryTransport(httpx.AsyncBaseTransport):
    """只搬运 HTTP bytes；协议解析和 request correlation 完全属于官方 SDK。"""

    def __init__(self, client, endpoint, headers, transport_name):
        self.client, self.endpoint, self.headers = client, endpoint, headers
        self.transport_name = transport_name
        self.posts = set()
        self.gets = 0
        self.session_id = None
        self.last_status = None
        self.rejected = False
        self.http_failure = None
        self.completed_posts = 0
        self.response_id_failed = False
        self.post_finished = asyncio.Event()

    async def handle_async_request(self, request):
        if 'last-event-id' in request.headers:
            self.rejected = True
            raise TransportBoundaryError('MCP automatic stream resumption is disabled')
        if origin(request.url) != origin(self.endpoint):
            self.rejected = True
            raise TransportBoundaryError('MCP endpoint must remain same-origin')
        if request.method == 'GET':
            self.gets += 1
            if self.gets > 1:
                self.rejected = True
                raise TransportBoundaryError('MCP automatic reconnection is disabled')
        if request.method == 'POST':
            body = await request.aread()
            if body in self.posts:
                raise TransportBoundaryError('MCP automatic request replay is disabled')
            self.posts.add(body)
        # 不携带借入 client 的 cookies/default headers/auth；仅显式配置凭据。
        headers = dict(request.headers)
        headers.update(self.headers)
        headers.pop('host', None)
        forwarded = httpx.Request(request.method, request.url, headers=headers,
            content=request.content if request.method == 'POST' else b'', extensions=request.extensions)
        response = await self.client.send(forwarded, stream=True, auth=None, follow_redirects=False)
        if request.method == 'POST':
            self.completed_posts += 1
            self.post_finished.set()
        # 精确遗留例外：HTTP 200 POST 缺媒体类型时交给 SDK 按 JSON 校验，
        # 不解析/修复 envelope，不在失败后重发，不影响标准 SSE。
        if self.transport_name == 'http' and request.method == 'POST' and response.status_code == 200 and 'content-type' not in response.headers:
            response.headers['content-type'] = 'application/json'
        self.last_status = response.status_code
        if 300 <= response.status_code < 400:
            self.rejected = True
            await response.aclose()
            raise TransportBoundaryError('MCP redirects are disabled')
        if response.status_code >= 400 and not (request.method == 'GET' and response.status_code == 405):
            self.http_failure = response.status_code
        if self.transport_name == 'sse' and request.method == 'GET' and response.status_code == 200:
            response = httpx.Response(response.status_code, headers=response.headers,
                stream=_EndpointStream(response, self), extensions=response.extensions)
        identifier = response.headers.get('Mcp-Session-Id')
        if identifier is not None:
            if not identifier or len(identifier) > 512 or any(not 33 <= ord(c) <= 126 for c in identifier):
                await response.aclose()
                raise TransportBoundaryError('MCP invalid session identifier')
            self.session_id = identifier
        return response


class _EndpointStream(httpx.AsyncByteStream):
    """仅 endpoint URL 等价正规化；事件解码由官方 httpx-sse 完成，RPC不读取。"""
    def __init__(self, response, boundary):
        self.response, self.boundary = response, boundary
        self.endpoint = None

    async def __aiter__(self):
        async for event in EventSource(self.response).aiter_sse():
            data = event.data
            if event.event == 'endpoint':
                resolved = urljoin(self.boundary.endpoint, data)
                if not data or any(ord(c) <= 32 or ord(c) == 127 for c in data) or origin(resolved) != origin(self.boundary.endpoint):
                    self.boundary.rejected = True
                    raise TransportBoundaryError('MCP SSE endpoint rejected')
                data = canonical_endpoint(resolved)
                if self.endpoint is not None:
                    if data != self.endpoint:
                        self.boundary.rejected = True
                        raise TransportBoundaryError('MCP SSE endpoint changed')
                    continue
                self.endpoint = data
            lines = ['event: ' + event.event, *('data: ' + line for line in data.split('\n'))]
            if event.id:
                lines.append('id: ' + event.id)
            if event.retry is not None:
                lines.append('retry: ' + str(event.retry))
            yield ('\n'.join(lines) + '\n\n').encode('utf-8')

    async def aclose(self):
        await self.response.aclose()


@asynccontextmanager
async def session_scope(config, timeout, borrowed=None, on_boundary=None):
    """SDK tasks 在该 context 内终止；不使用 resumption metadata 或副作用重试。"""
    @asynccontextmanager
    async def client_scope():
        if borrowed is not None:
            yield borrowed
        else:
            async with httpx.AsyncClient(timeout=timeout, trust_env=False, follow_redirects=False) as client:
                yield client
    async with _sdk_log_scope(), client_scope() as underlying:
        log_filter = _HttpLogBoundary(config['endpoint'])
        logging.getLogger('httpx').addFilter(log_filter)
        boundary = BoundaryTransport(underlying, config['endpoint'], config['headers'], config['transport'])
        def reject_response_id():
            boundary.rejected = True
            boundary.response_id_failed = True
        if on_boundary is not None:
            on_boundary(boundary)
        endpoint = canonical_endpoint(config['endpoint'])
        @asynccontextmanager
        async def factory(**kwargs):
            async with httpx.AsyncClient(transport=boundary, timeout=timeout, trust_env=False) as client:
                yield client
        if config['transport'] == 'sse':
            transport = sse_client(endpoint, timeout=timeout,
                sse_read_timeout=timeout, httpx_client_factory=factory)
        else:
            # 禁止恢复后自动 DELETE 产生额外状态副作用；会话遵循服务端 TTL。
            client = httpx.AsyncClient(transport=boundary, timeout=timeout, trust_env=False)
            transport = streamable_http_client(endpoint, http_client=client, terminate_on_close=False)
        try:
            async with transport as streams, StrictClientSession(
                streams[0], streams[1], read_timeout_seconds=timedelta(seconds=timeout),
                on_invalid_response=reject_response_id,
            ) as session:
                yield session, boundary
        finally:
            logging.getLogger('httpx').removeFilter(log_filter)
            if config['transport'] != 'sse':
                await client.aclose()


def dump(value):
    return value.model_dump(mode='json', by_alias=True, exclude_none=True, exclude_unset=True)
