"""验证保护器实际拒绝外部 DNS、socket、HTTP，同时允许回环套接字。"""
import asyncio
import json
from pathlib import Path
import socket
import sys
import threading

import offline_network_guard as guard
import b4_run_guard as transport
import httpx

assert socket.socket.connect is guard._guard_connect
assert httpx.HTTPTransport.handle_request is transport.deny_sync
for operation in (lambda: socket.getaddrinfo('negative-control.invalid', 443),
                  lambda: socket.socket().connect(('203.0.113.1', 443)),
                  lambda: socket.socket().connect_ex(('203.0.113.1', 443))):
    try:
        operation()
    except OSError:
        pass
    else:
        raise AssertionError('external_network_not_denied')
try:
    with httpx.Client(trust_env=False) as client:
        client.get('https://negative-control.invalid')
except httpx.ConnectError:
    pass
else:
    raise AssertionError('real_sync_http_not_denied')


async def check_async():
    try:
        async with httpx.AsyncClient(trust_env=False) as client:
            await client.get('https://negative-control.invalid')
    except httpx.ConnectError:
        return
    raise AssertionError('real_async_http_not_denied')


asyncio.run(check_async())
with socket.socket() as listener:
    listener.bind(('127.0.0.1', 0))
    listener.listen(1)
    listener.settimeout(5)
    def echo():
        with listener.accept()[0] as peer:
            peer.settimeout(5)
            assert peer.recv(4) == b'ping'
            peer.sendall(b'pong')
    thread = threading.Thread(target=echo, daemon=True)
    thread.start()
    with socket.create_connection(listener.getsockname(), timeout=5) as connection:
        connection.sendall(b'ping')
        assert connection.recv(4) == b'pong'
    thread.join(5)
    assert not thread.is_alive()
assert dict(guard._network_attempts) == {'dns': 1, 'connect': 1, 'connect_ex': 1}
assert [row['transport'] for row in transport.real_http_attempts] == ['sync', 'async']
result = {'guardLoaded': True, 'deniedNetworkAttempts': dict(guard._network_attempts),
    'deniedHttpTransports': [row['transport'] for row in transport.real_http_attempts], 'loopbackExchange': True}
with Path(sys.argv[1]).open('x', encoding='utf-8') as stream:
    json.dump(result, stream, indent=2)
