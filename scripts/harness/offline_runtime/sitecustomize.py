"""离线候选验证保护：允许回环测试服务与 IPC，拒绝外部 DNS 和套接字连接。"""
import ipaddress
import json
import os
import sys
import time


def record(kind):
    destination = os.environ.get("EIC_CANDIDATE_NETWORK_LOG")
    if destination:
        descriptor = os.open(destination, os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o600)
        try:
            os.write(descriptor, (json.dumps({"pid": os.getpid(), "kind": kind, "time": time.time()}) + "\n").encode())
        finally:
            os.close(descriptor)


def loopback(host):
    if host in (None, "", "localhost", b"localhost"):
        return True
    try:
        return ipaddress.ip_address(host.decode() if isinstance(host, bytes) else host).is_loopback
    except ValueError:
        return False


def audit(event, arguments):
    if event == "socket.getaddrinfo" and not loopback(arguments[0]):
        record("blocked_dns")
        raise OSError("EIC_CANDIDATE_OFFLINE_DNS_DENIED")
    if event in {"socket.connect", "socket.sendto"}:
        address = arguments[1] if event == "socket.connect" else arguments[-1]
        if isinstance(address, tuple) and not loopback(address[0]):
            record("blocked_socket")
            raise OSError("EIC_CANDIDATE_OFFLINE_SOCKET_DENIED")


sys.addaudithook(audit)
record("guard_loaded")
