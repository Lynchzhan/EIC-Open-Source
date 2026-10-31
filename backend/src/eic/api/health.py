"""health 领域；通过显式装配提供跨域依赖。"""
from dataclasses import dataclass
from types import SimpleNamespace as _RouteExports
from typing import Any
from eic.api.runtime import RuntimeBindings
from sqlalchemy.ext.asyncio import AsyncSession
from fastapi import Depends
from fastapi.responses import JSONResponse
from redis.exceptions import RedisError
from sqlalchemy.exc import SQLAlchemyError
import asyncio
import asyncpg
from eic.core.database import get_session
import redis.asyncio as redis_async
from sqlalchemy import select
from eic.core import queue as task_queue


@dataclass(frozen=True)
class Dependencies:
    pass


def bind_routes(app, *, dependencies: Dependencies, runtime: RuntimeBindings):
    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "service": "enterprise-intelligence-center"}


    @app.get("/api/Health/Ready")
    async def readiness(session: AsyncSession = Depends(get_session)) -> Any:
        """必需依赖失败返回 503；各探针有总时限，liveness 独立。"""
        try:
            async with asyncio.timeout(3):
                await session.execute(select(1))
            database = "ok"
        except (SQLAlchemyError, OSError, TimeoutError):
            database = "error"

        async def vector_probe() -> str:
            vector_url = runtime.settings.vector_database_url.replace("postgresql+asyncpg://", "postgresql://", 1)
            connection = None
            try:
                async with asyncio.timeout(3):
                    connection = await asyncpg.connect(vector_url, timeout=3)
                    extension = await connection.fetchval("SELECT extname FROM pg_extension WHERE extname = 'vector'")
                    return "ok" if extension == "vector" else "missing_extension"
            except (OSError, TimeoutError, ValueError, asyncpg.PostgresError, asyncpg.InterfaceError):
                return "unavailable"
            finally:
                if connection is not None:
                    try:
                        async with asyncio.timeout(1):
                            await connection.close()
                    except (OSError, TimeoutError, asyncpg.PostgresError, asyncpg.InterfaceError):
                        connection.terminate()

        async def queue_probe() -> str:
            client = None
            try:
                async with asyncio.timeout(3):
                    client = redis_async.from_url(runtime.settings.redis_url, socket_connect_timeout=3, socket_timeout=3)
                    return "ok" if await client.ping() else "unavailable"
            except (OSError, TimeoutError, ValueError, RedisError):
                return "unavailable"
            finally:
                if client is not None:
                    try:
                        async with asyncio.timeout(1):
                            await client.aclose()
                    except (OSError, TimeoutError, RedisError):
                        pass

        vector, queue = "skipped", "skipped"
        if runtime.settings.require_runtime_dependencies:
            vector, queue = await asyncio.gather(vector_probe(), queue_probe())
        worker = "skipped"
        if runtime.settings.task_execution_enabled:
            try:
                async with asyncio.timeout(3):
                    capabilities = await task_queue.task_capabilities()
                    worker = "ok" if capabilities["execution"] else "unavailable"
            except (OSError, TimeoutError, ValueError, RedisError):
                worker = "unavailable"
        ready = (database == "ok"
                 and (not runtime.settings.require_runtime_dependencies or vector == "ok" and queue == "ok")
                 and worker in {"ok", "skipped"})
        body = {
            "status": "ready" if ready else "not_ready",
            "database": database,
            "vector": vector,
            "queue": queue,
            "worker": worker,
            "service": runtime.settings.app_name,
        }
        return body if ready else JSONResponse(body, status_code=503)

    return _RouteExports(
        health=health,
        readiness=readiness,
    )
