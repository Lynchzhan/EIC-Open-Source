"""跨进程用例的真实网络阻断器；计数来自实际拒绝的调用。"""
from collections import Counter
import ipaddress
import socket
import httpx

_network_attempts = Counter()
_original_connect = socket.socket.connect
_original_connect_ex = socket.socket.connect_ex
_original_getaddrinfo = socket.getaddrinfo


def _loopback(host):
    if host in ('localhost', b'localhost', None, ''):
        return True
    try:
        return ipaddress.ip_address(host.decode() if isinstance(host, bytes) else host).is_loopback
    except ValueError:
        return False


def _guard_connect(connection, address):
    if isinstance(address, tuple) and not _loopback(address[0]):
        _network_attempts['connect'] += 1
        raise OSError('EIC_OFFLINE_EXTERNAL_CONNECT_DENIED')
    return _original_connect(connection, address)


def _guard_connect_ex(connection, address):
    if isinstance(address, tuple) and not _loopback(address[0]):
        _network_attempts['connect_ex'] += 1
        raise OSError('EIC_OFFLINE_EXTERNAL_CONNECT_DENIED')
    return _original_connect_ex(connection, address)


def _guard_getaddrinfo(host, *args, **kwargs):
    if not _loopback(host):
        _network_attempts['dns'] += 1
        raise OSError('EIC_OFFLINE_EXTERNAL_DNS_DENIED')
    return _original_getaddrinfo(host, *args, **kwargs)


socket.socket.connect = _guard_connect
socket.socket.connect_ex = _guard_connect_ex
socket.getaddrinfo = _guard_getaddrinfo
