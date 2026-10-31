"""进程生命周期内复用连接；HTTP 凭据、Cookie 不成为共享客户端状态。"""
import asyncio
from contextlib import asynccontextmanager

import asyncpg
import httpx

from eic.core.async_cleanup import complete_cleanup


class _BorrowedTransport(httpx.AsyncBaseTransport):
    def __init__(self, transport):
        self.transport = transport

    async def handle_async_request(self, request):
        return await self.transport.handle_async_request(request)

    async def aclose(self):
        # 单次请求只关闭包装器，共享池由进程生命周期关闭。
        pass


class _RuntimeIO:
    def __init__(self):
        self.loop = asyncio.get_running_loop()
        self.http = httpx.AsyncHTTPTransport(
            trust_env=False, retries=0,
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10, keepalive_expiry=30),
        )
        self.vector_pool = None
        self.vector_dsn = None
        self.vector_lock = asyncio.Lock()
        self.references = 1
        self.closing = False

    async def pool(self, dsn):
        async with self.vector_lock:
            if self.closing:
                raise RuntimeError("runtime_io_closing")
            if self.vector_pool is None:
                pool = asyncpg.create_pool(dsn, min_size=0, max_size=4, timeout=10,
                    max_inactive_connection_lifetime=60)
                try:
                    await pool
                except BaseException:
                    try:
                        pool.terminate()
                    except asyncpg.InterfaceError:
                        pass
                    raise
                self.vector_pool, self.vector_dsn = pool, dsn
            elif self.vector_dsn != dsn:
                raise RuntimeError("vector_pool_configuration_changed")
            return self.vector_pool

    async def close(self):
        self.closing = True
        async with self.vector_lock:
            pool = self.vector_pool
        try:
            if pool is not None:
                try:
                    async with asyncio.timeout(5):
                        await pool.close()
                except BaseException:
                    pool.terminate()
                    raise
        finally:
            async with asyncio.timeout(5):
                await self.http.aclose()


_runtime = None


def start_runtime_io():
    """仅从 API/worker 启动钩子启用；启动时不建网络连接。"""
    global _runtime
    loop = asyncio.get_running_loop()
    if _runtime is not None:
        if _runtime.loop is not loop:
            raise RuntimeError("runtime_io_event_loop_mismatch")
        _runtime.references += 1
    else:
        _runtime = _RuntimeIO()


def _current():
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return None
    if _runtime is not None and _runtime.loop is loop and not _runtime.closing:
        return _runtime
    return None


def borrowed_http_transport():
    runtime = _current()
    return _BorrowedTransport(runtime.http) if runtime is not None else None


async def stop_runtime_io():
    global _runtime
    runtime = _current()
    if runtime is None:
        return
    runtime.references -= 1
    if runtime.references:
        return
    _runtime = None
    await complete_cleanup(runtime.close())


@asynccontextmanager
async def vector_connection(dsn):
    runtime = _current()
    if runtime is None:
        # 独立脚本/同步适配器不创建跨 asyncio.run 存活的全局池。
        connection = await asyncpg.connect(dsn)
        try:
            yield connection
        finally:
            await complete_cleanup(connection.close())
        return
    pool = await runtime.pool(dsn)
    connection = await pool.acquire(timeout=10)
    try:
        yield connection
    finally:
        await complete_cleanup(pool.release(connection, timeout=10))
