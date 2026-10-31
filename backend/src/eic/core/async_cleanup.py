"""取消期间必须完成的异步资源清理，与模型/流式协议无关。"""
import asyncio
from collections.abc import Awaitable
from typing import TypeVar

T = TypeVar("T")


async def complete_cleanup(awaitable: Awaitable[T]) -> T:
    """等待清理完成再传播 Task.cancel；shield 保护唯一清理任务。"""
    task = asyncio.ensure_future(awaitable)
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
    result = task.result()
    if cancelled:
        raise asyncio.CancelledError
    return result
