"""仅供显式导入的跨进程离线用例；拒绝 HTTPX 默认真实传输。"""
import httpx

real_http_attempts = []


def deny_sync(transport, request):
    real_http_attempts.append({'transport': 'sync', 'method': request.method})
    raise httpx.ConnectError('EIC_OFFLINE_REAL_HTTP_DENIED', request=request)


async def deny_async(transport, request):
    real_http_attempts.append({'transport': 'async', 'method': request.method})
    raise httpx.ConnectError('EIC_OFFLINE_REAL_HTTP_DENIED', request=request)


httpx.HTTPTransport.handle_request = deny_sync
httpx.AsyncHTTPTransport.handle_async_request = deny_async
