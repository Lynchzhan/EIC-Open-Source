"""产品 MCP 边界：官方 SDK 负责 initialize/tools/list/call 和 HTTP/SSE。"""
from __future__ import annotations

import asyncio
import math
from typing import Any

import httpx
from mcp.shared.exceptions import McpError
from pydantic import ValidationError

from eic.ai.tool_center import validate_mcp_config
from eic.ai.mcp_sdk_adapter import session_scope, origin, dump, types, TransportBoundaryError

MCP_PROTOCOL_VERSION = '2025-03-26'
SUPPORTED_PROTOCOL_VERSIONS = {'2024-11-05', '2025-03-26', '2025-06-18', '2025-11-25'}


class McpClientError(RuntimeError):
    def __init__(self, message, *, code='mcp_error', retryable=False, rpc_code=None):
        super().__init__(message)
        self.code, self.retryable, self.rpc_code = code, retryable, rpc_code


def _leaves(exc):
    if isinstance(exc, BaseExceptionGroup):
        for child in exc.exceptions:
            yield from _leaves(child)
    else:
        yield exc


class HttpMcpClient:
    """兼容原 async 方法；一个有总时限的 owner task 管理完整 SDK session。

    initialize/list/call 共用同一会话和分页 cursor，call 完成自动关闭；派发前
    服务拒绝时 owner 到期关闭。无需初始化的旧 call_tool API 仍执行标准握手。
    不自动回退非标准单 RPC 服务，防止工具已执行后尝试第二种 transport。
    """
    def __init__(self, config: dict[str, Any], *, client: httpx.AsyncClient | None = None, timeout=30.0):
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError('MCP timeout must be a positive finite number')
        self.config = validate_mcp_config(config)
        if origin(self.config['endpoint']) is None:
            raise ValueError('MCP endpoint must be an HTTP(S) URL without credentials or fragment')
        self._client, self._timeout = client, float(timeout)
        self._initialize_result = None
        self._session_id = None
        self._lock = asyncio.Lock()
        self._worker = None
        self._queue = None
        self._boundary = None
        self._call_started = False
        self._cleanup_pending = False
        self._closed = False

    async def _serve(self):
        pending = None
        call_result = None
        try:
            async with asyncio.timeout(self._timeout):
                async with session_scope(self.config, self._timeout, self._client, lambda value: setattr(self, '_boundary', value)) as (session, boundary):
                    self._boundary = boundary
                    initialized = await session.initialize()
                    # SDK通知写入内存队列后即返回；必须等待initialized实际HTTP提交，
                    # 避免401/redirect/挂起被当成完成握手。
                    while boundary.completed_posts < 2:
                        boundary.post_finished.clear()
                        await boundary.post_finished.wait()
                    if boundary.http_failure is not None or boundary.rejected:
                        raise TransportBoundaryError('MCP initialized notification failed')
                    self._initialize_result = dump(initialized)
                    while True:
                        method, kwargs, pending = await self._queue.get()
                        if pending.cancelled():
                            continue
                        if boundary.response_id_failed:
                            raise TransportBoundaryError('MCP response ID rejected')
                        if method == 'list':
                            cursor = kwargs.get('cursor')
                            result = await session.list_tools(params=types.PaginatedRequestParams(cursor=cursor) if cursor is not None else None)
                        elif method == 'call':
                            self._call_started = True
                            result = await session.call_tool(kwargs['name'], kwargs['arguments'])
                        else:
                            result = initialized
                        if boundary.response_id_failed:
                            raise TransportBoundaryError('MCP response ID rejected')
                        self._session_id = boundary.session_id
                        if method == 'call':
                            call_result = dump(result)
                            self._cleanup_pending = True
                            break
                        pending.set_result(dump(result))
                        pending = None
                # 工具成功仅在完整SDK session/transport清理成功后发布。
                if pending is not None and not pending.done():
                    if boundary.response_id_failed:
                        raise TransportBoundaryError('MCP response ID rejected')
                    self._cleanup_pending = False
                    pending.set_result(call_result)
                    pending = None
        except BaseException as exc:
            if not self._call_started:
                self._initialize_result = None
                self._session_id = None
            if pending is not None and not pending.done():
                pending.set_exception(exc)
            while not self._queue.empty():
                _, _, future = self._queue.get_nowait()
                if not future.done():
                    future.set_exception(exc)
            # Future 消费错误；owner 不遗留未读取异常，也不重连。
        finally:
            self._closed = True

    async def aclose(self):
        self._closed = True
        if self._worker is not None:
            self._worker.cancel()
            await self._worker
            self._worker = None

    @property
    def initialized(self):
        return self._initialize_result is not None

    @property
    def session_id(self):
        return self._session_id

    async def _operate(self, method, **kwargs):
        failure = None
        try:
            async with self._lock:
                async with asyncio.timeout(self._timeout):
                    if self._closed or self._call_started or (self._initialize_result is not None and (self._worker is None or self._worker.done())):
                        raise McpClientError('MCP session cannot be replayed; create a new authorized invocation', code='session_expired')
                    if self._boundary is not None and self._boundary.response_id_failed:
                        raise McpClientError('MCP session protocol failed', code='protocol_error')
                    if self._worker is None or self._worker.done():
                        self._queue = asyncio.Queue()
                        self._worker = asyncio.create_task(self._serve(), name='eic-mcp-sdk-session')
                    future = asyncio.get_running_loop().create_future()
                    await self._queue.put((method, kwargs, future))
                    result = await future
                    if method == 'call':
                        await self._worker
                    if self._boundary is not None and self._boundary.response_id_failed:
                        raise TransportBoundaryError('MCP response ID rejected')
                    return result
        except asyncio.CancelledError:
            await self.aclose()
            raise
        except Exception as exc:
            await self.aclose()
            leaves = list(_leaves(exc))
            rpc = next((e for e in leaves if isinstance(e, McpError)), None)
            http = next((e for e in leaves if isinstance(e, httpx.HTTPStatusError)), None)
            boundary_status = self._boundary.http_failure if self._boundary is not None else None
            if self._cleanup_pending:
                failure = McpClientError('MCP tool result received but session cleanup failed; outcome must not be replayed', code='outcome_uncertain')
            elif isinstance(exc, McpClientError):
                failure = exc
            elif self._boundary is not None and self._boundary.rejected:
                failure = McpClientError('MCP protocol boundary rejected the response', code='protocol_error')
            elif boundary_status is not None:
                code = 'authentication_error' if boundary_status in {401, 403} else 'session_expired' if boundary_status == 404 and self._boundary.session_id else 'http_error'
                failure = McpClientError('MCP endpoint returned an HTTP error', code=code)
            elif any(isinstance(e, (TimeoutError, httpx.TimeoutException)) for e in leaves):
                failure = McpClientError('MCP request timed out', code='timeout')
            elif http is not None:
                status = http.response.status_code
                code = 'authentication_error' if status in {401, 403} else 'session_expired' if status == 404 and self._session_id else 'http_error'
                failure = McpClientError('MCP endpoint returned an HTTP error', code=code)
            elif rpc is not None:
                code = 'session_expired' if self._boundary is not None and self._boundary.last_status == 404 and self._boundary.session_id else 'remote_error'
                failure = McpClientError('MCP server returned a JSON-RPC error', code=code, rpc_code=rpc.error.code)
            elif any(isinstance(e, (ValidationError, ValueError, RuntimeError, TransportBoundaryError)) for e in leaves):
                failure = McpClientError('MCP protocol boundary rejected the response', code='protocol_error')
            else:
                failure = McpClientError('MCP transport failed', code='transport_error')
        # 不保留 SDK 原始 exception context，避免 token、URL 查询参数和 RPC data 泄露。
        raise failure from None

    async def initialize(self):
        if self._closed:
            raise McpClientError('MCP session is closed', code='session_expired')
        if self._boundary is not None and self._boundary.response_id_failed:
            raise McpClientError('MCP session protocol failed', code='protocol_error')
        if self._initialize_result is None or self._worker is None or self._worker.done():
            self._initialize_result = await self._operate('initialize')
        return dict(self._initialize_result)

    async def list_tools(self, *, cursor=None):
        if self._closed:
            raise McpClientError('MCP session is closed', code='session_expired')
        if not self.initialized:
            raise McpClientError('initialize must complete before tools/list', code='not_initialized')
        return await self._operate('list', cursor=cursor)

    async def call_tool(self, name, arguments):
        if not isinstance(name, str) or not name.strip() or not isinstance(arguments, dict):
            raise McpClientError('MCP tool name and arguments are invalid', code='invalid_request')
        return await self._operate('call', name=name, arguments=arguments)
