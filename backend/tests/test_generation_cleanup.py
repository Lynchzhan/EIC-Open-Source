"""原生 Task.cancel、嵌套生成器关闭和心跳事务退出的确定性反例。"""
import asyncio

from test_id_generation_safety import fixture_db, claim


def test_cleanup_waits_through_repeated_native_cancellation():
    from eic.ai.stream_reader import complete_cleanup

    async def run():
        entered, release, closed = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def cleanup():
            entered.set()
            await release.wait()
            closed.set()

        task = asyncio.create_task(complete_cleanup(cleanup()))
        await entered.wait()
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done() and not closed.is_set()
        release.set()
        result = await asyncio.gather(task, return_exceptions=True)
        assert closed.is_set() and isinstance(result[0], asyncio.CancelledError)
    asyncio.run(run())


def test_leased_wrapper_closes_suspended_inner_before_return(monkeypatch):
    from eic import main
    closed = []

    async def inner(*args):
        try:
            yield "fixture"
        finally:
            await asyncio.sleep(.01)
            closed.append(True)

    monkeypatch.setattr(main, "_sse_events_inner", inner)
    async def cancel(*args):
        pass
    monkeypatch.setattr(main, "mark_chat_cancelled", cancel)

    async def run():
        stream = main._leased_sse_events("fixture", main.Principal(11, 1, "fixture", True))
        assert await anext(stream) == "fixture"
        await stream.aclose()
        assert closed == [True]
    asyncio.run(run())


def test_heartbeat_stop_does_not_cancel_inflight_transaction(monkeypatch, tmp_path):
    from eic.core import generation

    async def run():
        async with fixture_db(monkeypatch, tmp_path) as sessions:
            token = await claim(sessions)
            owner = generation.OWNER.set((301, token))
            entered, release, stop = asyncio.Event(), asyncio.Event(), asyncio.Event()
            original = generation.fence

            async def fence(session):
                entered.set()
                await release.wait()
                await original(session)

            monkeypatch.setattr(generation, "fence", fence)
            monkeypatch.setattr(generation, "HEARTBEAT_SECONDS", .01)
            task = asyncio.create_task(generation.heartbeat(asyncio.current_task(), stop))
            try:
                await asyncio.wait_for(entered.wait(), 2)
                stop.set()
                await asyncio.sleep(.02)
                assert not task.done()
                release.set()
                await asyncio.wait_for(task, 2)
                assert await generation.recover_expired() == 0
            finally:
                release.set()
                stop.set()
                await task
                generation.OWNER.reset(owner)
    asyncio.run(run())


def test_replay_cancel_commit_between_message_and_events_waits_for_provider_close(monkeypatch, tmp_path):
    from eic import main
    from eic.core import generation
    from eic.models import ChatMessage, GenerationLease
    from eic.core.ids import new_id

    async def run():
        async with fixture_db(monkeypatch, tmp_path, is_super_admin=True) as sessions:
            await claim(sessions)
            original = main.require_owned_chat_request
            raced = False
            async def race(session, *args, **kwargs):
                nonlocal raced
                result = await original(session, *args, **kwargs)
                if not raced:
                    raced = True
                    assert result[1].status == "generating"
                    async with sessions() as other:
                        await generation.cancel_request(other, await other.get(ChatMessage, 301))
                        await other.commit()
                return result
            monkeypatch.setattr(main, "require_owned_chat_request", race)
            replay = main.replay_chat_events("fixture", main.Principal(11, 1, "fixture", True))
            assert await anext(replay) == ": keepalive\n\n"
            async with sessions() as s:
                await new_id(s)
                (await s.get(GenerationLease, 301)).state = "cancelled_closed"
                await s.commit()
            chunks = [chunk async for chunk in replay]
            assert sum("event: done\n" in chunk for chunk in chunks) == 1
    asyncio.run(run())
