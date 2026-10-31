import asyncio
import json
from sqlalchemy import select
from test_id_generation_safety import fixture_db


def test_disconnect_reconnect_only_one_producer(monkeypatch, tmp_path):
    from eic import main
    from eic.core import generation
    from eic.models import AIModel, ChatMessage, ChatEvent
    calls, closes = [], []
    async def run():
        ready, release = asyncio.Event(), asyncio.Event()
        async def provider(model, question, **kwargs):
            calls.append(model.id)
            try:
                yield "PART"
                ready.set()
                await release.wait()
                yield "FINAL"
            finally:
                closes.append(model.id)
        monkeypatch.setattr(main, "stream_chat_completion", provider)
        async with fixture_db(monkeypatch, tmp_path, is_super_admin=True) as sessions:
            async with sessions() as s:
                s.add(AIModel(id=201, tenant_id=1, model_type="chat", provider="openai",
                              model_name="A", endpoint="http://fixture.invalid", enabled=True))
                await s.commit()
            principal = main.Principal(11, 1, "fixture", True)
            source = main.sse_events("fixture", principal)
            await anext(source)
            await asyncio.wait_for(ready.wait(), 3)
            await source.aclose()
            async with sessions() as s:
                assert (await s.get(ChatMessage,301)).status == "generating"
            assert calls == [201] and not closes
            subscribers = [asyncio.create_task(collect(main.sse_events("fixture",principal))) for _ in range(2)]
            release.set()
            outputs = await asyncio.gather(*subscribers)
            assert calls == [201] and closes == [201]
            assert all(sum("event: done\n" in c for c in chunks) == 1 for chunks in outputs)
            async with sessions() as s:
                assert (await s.get(ChatMessage,301)).status == "completed"
                events = list(await s.scalars(select(ChatEvent).order_by(ChatEvent.sequence)))
                assert sum(e.event_type == "done" for e in events) == 1
                assert "".join(json.loads(e.payload)["text"] for e in events if e.event_type=="aimsg") == "PARTFINAL"
                cursor = next(e.sequence for e in events if e.event_type == "aimsg")
            resumed = await collect(main.sse_events("fixture",principal,cursor))
            assert not any('\"text\": \"PART\"' in c for c in resumed)
            assert any('\"text\": \"FINAL\"' in c for c in resumed)
            await asyncio.sleep(0)
            assert not generation.PRODUCERS
    asyncio.run(run())


def test_legacy_partial_replay_keeps_failed_status(monkeypatch,tmp_path):
    from eic import main
    from eic.models import ChatMessage
    async def run():
        async with fixture_db(monkeypatch,tmp_path,is_super_admin=True) as sessions:
            async with sessions() as s:
                user=await s.get(ChatMessage,301)
                user.status="failed"
                user.token_usage={"partial":True,"status":"completed"}
                s.add(ChatMessage(id=302,tenant_id=1,session_id=user.session_id,role="assistant",
                                  content="OLD_PART",request_id="fixture",status="failed",token_usage={}))
                await s.commit()
            output=await collect(main.sse_events("fixture",main.Principal(11,1,"fixture",True)))
            terminal=next(c for c in output if "event: done\n" in c)
            payload=json.loads(terminal.split("data: ",1)[1])
            assert payload["status"]=="failed" and payload["partial"] is True
            assert any("OLD_PART" in c for c in output)
    asyncio.run(run())


async def collect(source):
    return [c async for c in source]


def test_shutdown_uncertain_is_not_cancelled_or_retried(monkeypatch,tmp_path):
    from eic import main
    from eic.core import generation
    from eic.models import AIModel, ChatMessage
    calls = []
    async def run():
        ready = asyncio.Event()
        async def provider(model,question,**kwargs):
            calls.append(model.id)
            ready.set()
            await asyncio.Event().wait()
            yield "never"
        monkeypatch.setattr(main,"stream_chat_completion",provider)
        async with fixture_db(monkeypatch,tmp_path,is_super_admin=True) as sessions:
            async with sessions() as s:
                s.add(AIModel(id=201,tenant_id=1,model_type="chat",provider="openai",
                              model_name="A",endpoint="http://fixture.invalid",enabled=True))
                await s.commit()
            principal=main.Principal(11,1,"fixture",True)
            subscriber=asyncio.create_task(collect(main.sse_events("fixture",principal)))
            await asyncio.wait_for(ready.wait(),3)
            await generation.stop_producers()
            chunks=await asyncio.wait_for(subscriber,3)
            async with sessions() as s:
                message=await s.get(ChatMessage,301)
                assert message.status=="failed"
                assert message.token_usage["providerOutcome"]=="uncertain"
                assert message.token_usage["automaticRetry"] is False
            assert sum("event: done\n" in c for c in chunks)==1
            await collect(main.sse_events("fixture",principal))
            assert calls==[201] and not generation.PRODUCERS
    asyncio.run(run())


def test_uncertain_error_before_first_token_never_falls_back(monkeypatch,tmp_path):
    from eic import main
    from eic.models import AIModel, ChatMessage
    from eic.ai.providers import ModelProviderError
    calls=[]
    async def run():
        async def provider(model,question,**kwargs):
            calls.append(model.id)
            raise ModelProviderError("SECRET",reason="provider_outcome_uncertain",retryable=False)
            yield "never"
        monkeypatch.setattr(main,"stream_chat_completion",provider)
        async with fixture_db(monkeypatch,tmp_path,is_super_admin=True) as sessions:
            async with sessions() as s:
                for mid in (201,202):
                    s.add(AIModel(id=mid,tenant_id=1,model_type="chat",provider="openai",
                                  model_name=str(mid),endpoint="http://fixture.invalid",enabled=True))
                await s.commit()
            chunks=await collect(main.sse_events("fixture",main.Principal(11,1,"fixture",True)))
            assert len(calls)==1 and "SECRET" not in "".join(chunks)
            async with sessions() as s:
                message=await s.get(ChatMessage,301)
                assert message.status=="failed"
                assert message.token_usage["reason"]=="provider_outcome_uncertain"
    asyncio.run(run())
