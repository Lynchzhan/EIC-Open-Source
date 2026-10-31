"""标准 MCP 合成服务；真实 SDK 完成握手、分页和流解析。"""
import asyncio
import json

import httpx
from mcp import types


def rpc(request, result):
    return {'jsonrpc': '2.0', 'id': request['id'], 'result': result}


class QueueStream(httpx.AsyncByteStream):
    def __init__(self, queue):
        self.queue, self.closed = queue, False

    async def __aiter__(self):
        while True:
            item = await self.queue.get()
            if item is None:
                return
            if isinstance(item, Exception):
                raise item
            yield item

    async def aclose(self):
        self.closed = True


class StandardServer:
    def __init__(self, transport='http', *, wire='json', endpoint='/messages', hook=None, ack=202):
        self.transport, self.wire, self.endpoint = transport, wire, endpoint
        self.hook, self.ack = hook, ack
        self.requests, self.payloads, self.streams = [], [], []
        self.queue = asyncio.Queue()

    @staticmethod
    def frame(value):
        return ('event: message\ndata: ' + json.dumps(value, ensure_ascii=False) + '\n\n').encode()

    async def __call__(self, request):
        self.requests.append(request)
        if request.method == 'GET':
            if self.transport == 'http':
                return httpx.Response(405)
            await self.queue.put(('event: endpoint\ndata: ' + self.endpoint + '\n\n').encode())
            stream = QueueStream(self.queue)
            self.streams.append(stream)
            return httpx.Response(200, stream=stream, headers={'content-type': 'text/event-stream'})
        body = json.loads(request.content)
        self.payloads.append(body)
        method = body.get('method', '')
        if self.hook is not None:
            response = await self.hook(request, body, self)
            if response is not None:
                return response
        if method.startswith('notifications/') or not method:
            return httpx.Response(self.ack)
        if method == 'initialize':
            result = {'protocolVersion': types.LATEST_PROTOCOL_VERSION, 'capabilities': {},
                      'serverInfo': {'name': 'synthetic-server', 'version': '1'}}
        elif method == 'tools/list':
            result = {'tools': [{'name': 'lookup', 'inputSchema': {'type': 'object'}}]}
            if body.get('params', {}).get('cursor') is None:
                result['nextCursor'] = 'page-2'
        else:
            result = {'content': [{'type': 'text', 'text': '中文结果'}], 'isError': False}
        return await self.respond(body, result)

    async def respond(self, body, result=None, *, envelope=None):
        value = envelope if envelope is not None else rpc(body, result)
        headers = {'Mcp-Session-Id': 'synthetic-session'}
        if self.transport == 'sse':
            await self.queue.put(self.frame(value))
            return httpx.Response(self.ack, headers=headers)
        if self.wire == 'post_sse':
            return httpx.Response(200, content=self.frame(value),
                headers={**headers, 'content-type': 'text/event-stream'})
        return httpx.Response(200, json=value, headers=headers)

    def methods(self):
        return [p.get('method') for p in self.payloads]

    def calls(self):
        return sum(p.get('method') == 'tools/call' for p in self.payloads)
