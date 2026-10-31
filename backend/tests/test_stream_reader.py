"""No application import, database, network, or paid provider."""
import asyncio

import anyio
import pytest

from eic.ai.stream_reader import stream_with_heartbeats


def assert_no_provider_tasks():
    assert not [
        task for task in asyncio.all_tasks()
        if task.get_name() == "eic-provider-read" and not task.done()
    ]


def test_fast_stream_keeps_content_and_closes():
    async def run():
        closed = []
        async def provider():
            try:
                yield "first"
                yield "second"
            finally:
                closed.append(True)
        assert [part async for part in stream_with_heartbeats(provider())] == ["first", "second"]
        assert closed == [True]
        assert_no_provider_tasks()
    asyncio.run(run())


@pytest.mark.parametrize("slow_position", ["first", "gap"])
def test_real_eight_second_heartbeat_does_not_cancel_token(slow_position):
    async def run():
        cancelled = []
        closed = []
        async def provider():
            try:
                if slow_position == "gap":
                    yield "first"
                await asyncio.sleep(8.25)
                yield "late"
            except asyncio.CancelledError:
                cancelled.append(True)
                raise
            finally:
                closed.append(True)
        parts = [part async for part in stream_with_heartbeats(provider())]
        assert parts == (["first", None, "late"] if slow_position == "gap" else [None, "late"])
        assert not cancelled and closed == [True]
        assert_no_provider_tasks()
    asyncio.run(run())


def test_repeated_heartbeats_keep_one_pending_read():
    async def run():
        entered = []
        reader_tasks = []
        release = asyncio.Event()
        async def provider():
            entered.append(True)
            await release.wait()
            yield "late"
        reader = stream_with_heartbeats(provider(), heartbeat_interval=0.01)
        for _ in range(3):
            assert await anext(reader) is None
            tasks = [t for t in asyncio.all_tasks() if t.get_name() == "eic-provider-read"]
            assert len(tasks) == 1
            reader_tasks.append(tasks[0])
        assert len(entered) == 1 and all(t is reader_tasks[0] for t in reader_tasks)
        release.set()
        assert await anext(reader) == "late"
        await reader.aclose()
        assert_no_provider_tasks()
    asyncio.run(run())


def test_provider_error_propagates_and_closes():
    async def run():
        closed = []
        async def provider():
            try:
                yield "first"
                raise RuntimeError("synthetic provider error")
            finally:
                closed.append(True)
        reader = stream_with_heartbeats(provider())
        assert await anext(reader) == "first"
        with pytest.raises(RuntimeError, match="synthetic provider error"):
            await anext(reader)
        assert closed == [True]
        assert_no_provider_tasks()
    asyncio.run(run())


def test_consumer_cancel_cancels_pending_and_closes():
    async def run():
        entered = asyncio.Event()
        cancelled = []
        closed = []
        async def provider():
            try:
                entered.set()
                await asyncio.sleep(100)
                yield "unused"
            except asyncio.CancelledError:
                cancelled.append(True)
                raise
            finally:
                closed.append(True)
        reader = stream_with_heartbeats(provider())
        task = asyncio.create_task(anext(reader))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await reader.aclose()
        assert cancelled == [True] and closed == [True]
        assert_no_provider_tasks()
    asyncio.run(run())


def test_close_after_token_does_not_start_another_read():
    async def run():
        second_read = []
        closed = []
        async def provider():
            try:
                yield "first"
                second_read.append(True)
                await asyncio.sleep(100)
                yield "unused"
            finally:
                closed.append(True)
        reader = stream_with_heartbeats(provider())
        assert await anext(reader) == "first"
        await reader.aclose()
        assert not second_read and closed == [True]
        assert_no_provider_tasks()
    asyncio.run(run())


def test_close_after_heartbeat_finishes_async_provider_cleanup():
    async def run():
        closed = []
        async def provider():
            try:
                await asyncio.sleep(100)
                yield "unused"
            finally:
                await asyncio.sleep(0.01)
                closed.append(True)
        reader = stream_with_heartbeats(provider(), heartbeat_interval=0.01)
        assert await anext(reader) is None
        await reader.aclose()
        assert closed == [True]
        assert_no_provider_tasks()
    asyncio.run(run())


def test_anyio_cancel_scope_finishes_provider_cleanup():
    async def run():
        entered = anyio.Event()
        closed = []
        async def provider():
            try:
                entered.set()
                await anyio.sleep(100)
                yield "unused"
            finally:
                await anyio.sleep(0.01)
                closed.append(True)
        async def consume():
            async for _ in stream_with_heartbeats(provider()):
                pass
        async with anyio.create_task_group() as group:
            group.start_soon(consume)
            await entered.wait()
            group.cancel_scope.cancel()
        assert closed == [True]
        assert_no_provider_tasks()
    anyio.run(run, backend="asyncio")


@pytest.mark.parametrize("interval", [0, -1])
def test_nonpositive_heartbeat_interval_rejected(interval):
    async def run():
        async def provider():
            yield "unused"
        source = provider()
        reader = stream_with_heartbeats(source, heartbeat_interval=interval)
        with pytest.raises(ValueError, match="must be positive"):
            await anext(reader)
        await source.aclose()
        assert_no_provider_tasks()
    asyncio.run(run())
