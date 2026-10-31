from collections.abc import AsyncIterator

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from eic.core.config import get_settings

settings = get_settings()
connect_args = {"timeout": 30} if "sqlite" in settings.database_url else {}
engine = create_async_engine(settings.database_url, pool_pre_ping=True, connect_args=connect_args)


if "sqlite" in settings.database_url:
    @event.listens_for(engine.sync_engine, "connect")
    def _sqlite_on_connect(dbapi_connection, _connection_record) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        # 持久 journal-mode 转换需要全库锁，不应在并发普通建连时执行。
        cursor.execute("PRAGMA busy_timeout=30000")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.close()


SessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


async def get_session() -> AsyncIterator[AsyncSession]:
    async with SessionLocal() as session:
        yield session
