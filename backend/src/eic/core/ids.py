"""Transactional, shared-database ID allocation; no process or wall clock state."""
from sqlalchemy import Integer, func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from eic.models import Base, IdAllocator

MAX_ID = 2**63 - 1
INITIAL_ID = 2_000_000_000_000_000


async def initialize_allocator(session: AsyncSession) -> None:
    """Explicit bootstrap/migration only; all old writers must be stopped."""
    if await session.get(IdAllocator, "global") is not None:
        return
    high = INITIAL_ID - 1
    for table in Base.metadata.sorted_tables:
        if table.name == IdAllocator.__tablename__:
            continue
        column = table.c.get("id")
        if column is not None and isinstance(column.type, Integer):
            value = await session.scalar(select(func.max(column)))
            high = max(high, value or 0)
    if high >= MAX_ID:
        raise RuntimeError("ID allocation exhausted")
    await session.execute(insert(IdAllocator).values(name="global", value=high))


async def new_id(session: AsyncSession) -> int:
    """Return an ID in the caller's transaction; publish only after commit.

    Rollback may reuse an uncommitted number, never a committed allocation.
    UPDATE takes the allocator row lock before SELECT on every SQL dialect.
    """
    return (await new_ids(session, 1))[0]


async def new_ids(session: AsyncSession, count: int) -> list[int]:
    """在调用者事务内一次分配有界 ID 段，减少逐实体 UPDATE/SELECT。"""
    if type(count) is not int or not 1 <= count <= 4096:
        raise ValueError('ID batch size must be between 1 and 4096')
    with session.no_autoflush:
        result = await session.execute(
            update(IdAllocator).where(
                IdAllocator.name == "global", IdAllocator.value <= MAX_ID - count,
            ).values(value=IdAllocator.value + count)
        )
        if result.rowcount != 1:
            raise RuntimeError("ID allocator missing or exhausted; explicit migration required")
        value = await session.scalar(select(IdAllocator.value).where(IdAllocator.name == "global"))
    return list(range(int(value) - count + 1, int(value) + 1))


async def supplied_id(session: AsyncSession, value: int) -> int:
    """保留历史 ID；新范围必须先由 GetSnowflakeId 的事务提交分配。"""
    if isinstance(value, bool) or not 0 < value <= MAX_ID:
        raise ValueError("Invalid signed 64-bit ID")
    with session.no_autoflush:
        high = await session.scalar(select(IdAllocator.value).where(IdAllocator.name == "global"))
    if high is None:
        raise RuntimeError("ID allocator missing; explicit migration required")
    if value >= INITIAL_ID and value > high:
        raise ValueError("ID not allocated by the shared allocator")
    return value
