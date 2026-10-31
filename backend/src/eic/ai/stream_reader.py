"""Provider reads and SSE heartbeats have independent lifecycles."""
import asyncio
from collections.abc import AsyncGenerator, AsyncIterator

from eic.core.async_cleanup import complete_cleanup as complete_cleanup


async def coalesce_text_stream(
    stream: AsyncGenerator[str, None], *, max_delay: float = 0.05,
    max_chars: int = 2048, max_chunks: int = 64,
) -> AsyncGenerator[str, None]:
    """首段立即交付，随后有界合并；超时刷新不取消唯一的上游读取。"""
    if max_delay <= 0 or max_chars <= 0 or max_chunks <= 0:
        raise ValueError("stream batch limits must be positive")
    pending = None
    buffered, size, deadline = [], 0, None
    first_text = True
    loop = asyncio.get_running_loop()
    try:
        while True:
            if pending is None:
                pending = asyncio.create_task(anext(stream), name="eic-provider-batch-read")
            timeout = max(0, deadline - loop.time()) if deadline is not None else None
            done, _ = await asyncio.wait({pending}, timeout=timeout)
            if not done:
                content = "".join(buffered)
                buffered, size, deadline = [], 0, None
                yield content
                continue
            try:
                content = pending.result()
            except StopAsyncIteration:
                if buffered:
                    yield "".join(buffered)
                return
            except Exception:
                # 保留已收到的部分正文，随后传播原异常，不自动重发模型调用。
                if buffered:
                    yield "".join(buffered)
                raise
            pending = None
            if not content:
                continue
            if first_text:
                first_text = False
                yield content
                continue
            if not buffered:
                deadline = loop.time() + max_delay
            buffered.append(content)
            size += len(content)
            if size >= max_chars or len(buffered) >= max_chunks or loop.time() >= deadline:
                content = "".join(buffered)
                buffered, size, deadline = [], 0, None
                yield content
    finally:
        async def close():
            if pending is not None:
                if not pending.done():
                    pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
            await stream.aclose()
        await complete_cleanup(close())


async def stream_with_heartbeats(
    stream: AsyncGenerator[str, None], *, heartbeat_interval: float = 8.0,
) -> AsyncIterator[str | None]:
    """Yield None for a heartbeat without replacing or cancelling the pending read."""
    if heartbeat_interval <= 0:
        raise ValueError("heartbeat_interval must be positive")
    pending: asyncio.Task[str] | None = None
    try:
        while True:
            if pending is None:
                pending = asyncio.create_task(anext(stream), name="eic-provider-read")
            done, _ = await asyncio.wait({pending}, timeout=heartbeat_interval)
            if not done:
                yield None
                continue
            try:
                content = pending.result()
            except StopAsyncIteration:
                return
            pending = None
            yield content
    finally:
        # 先结束唯一读取任务再关闭 provider，避免 aclose 与 anext 并行。
        async def close():
            if pending is not None:
                if not pending.done():
                    pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
            await stream.aclose()
        await complete_cleanup(close())
