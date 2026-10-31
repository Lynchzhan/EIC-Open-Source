"""真实 SQLite、模型循环与终态原子性回归，provider 全部合成。"""
import asyncio
import json

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from test_id_generation_safety import fixture_db, claim


@pytest.mark.parametrize("scenario,expected,answer,models", [
    ("normal", "completed", "A_FULL", ["A"]),
    ("pre-fail", "completed", "B_FULL", ["A", "B"]),
    ("partial-provider", "failed", "A_PART", ["A"]),
    ("partial-runtime", "failed", "A_PART", ["A"]),
    ("empty", "failed", "", ["A"]),
    ("whitespace", "failed", "", ["A"]),
    ("all-fail", "failed", "", ["A", "B"]),
    ("pre-runtime", "failed", "", ["A"]),
    ("close-fail", "failed", "A_FULL", ["A"]),
])
def test_model_terminal(monkeypatch, tmp_path, scenario, expected, answer, models):
    from datetime import UTC, datetime, timedelta
    from eic import main
    from eic.ai.providers import ModelProviderError
    from eic.models import AIModel, ChatMessage, ChatEvent
    calls, closes = [], []

    async def provider(model, question, **kwargs):
        calls.append(model.model_name)
        try:
            if scenario == "all-fail":
                raise ModelProviderError("SECRET")
            if model.model_name == "B":
                yield "B_FULL"
            elif scenario == "pre-fail":
                raise ModelProviderError("SECRET")
            elif scenario == "pre-runtime":
                raise RuntimeError("SECRET")
            elif scenario.startswith("partial"):
                yield "A_PART"
                if scenario == "partial-provider":
                    raise ModelProviderError("SECRET")
                raise RuntimeError("SECRET")
            elif scenario == "empty":
                return
            elif scenario == "whitespace":
                yield " \n "
            else:
                yield "A_FULL"
        finally:
            closes.append(model.model_name)
            if scenario == "close-fail":
                raise RuntimeError("SECRET")

    monkeypatch.setattr(main, "stream_chat_completion", provider)

    async def run():
        async with fixture_db(monkeypatch, tmp_path, is_super_admin=True) as sessions:
            async with sessions() as session:
                for mid, name, offset in ((201, "A", 1), (202, "B", 0)):
                    session.add(AIModel(
                        id=mid, tenant_id=1, model_type="chat", provider="openai",
                        model_name=name, endpoint="http://fixture.invalid", enabled=True,
                        updated_at=datetime.now(UTC) + timedelta(seconds=offset),
                    ))
                await session.commit()
            principal = main.Principal(11, 1, "fixture", True)
            chunks = [chunk async for chunk in main.sse_events("fixture", principal)]
            assert sum("event: done\n" in chunk for chunk in chunks) == 1
            assert "SECRET" not in "".join(chunks)
            assert calls == models and closes == models
            async with sessions() as session:
                rows = list(await session.scalars(select(ChatMessage).order_by(ChatMessage.id)))
                user = next(row for row in rows if row.role == "user")
                assistants = [row for row in rows if row.role == "assistant"]
                assert user.status == expected
                assert bool(user.token_usage["partial"]) == (expected == "failed" and bool(answer))
                assert len(assistants) == bool(answer)
                if answer:
                    assert assistants[0].content == answer and assistants[0].status == expected
                    assert assistants[0].token_usage == user.token_usage
                events = list(await session.scalars(select(ChatEvent).order_by(ChatEvent.sequence)))
                assert json.loads(events[-1].payload)["status"] == expected
                assert events[-1].event_type == "done"
                assert sum(row.event_type == "generationerror" for row in events) == (expected == "failed")
                assert "".join(json.loads(e.payload)["text"] for e in events if e.event_type == "aimsg").strip() == answer
            replay = [chunk async for chunk in main.sse_events("fixture", principal)]
            assert sum("event: done\n" in chunk for chunk in replay) == 1 and calls == models
    asyncio.run(run())


def test_terminal_commit_failure_rolls_back_all_roles(monkeypatch, tmp_path):
    from eic import main
    from eic.core import generation
    from eic.models import ChatMessage, ChatEvent

    async def run():
        async with fixture_db(monkeypatch, tmp_path, is_super_admin=True) as sessions:
            token = await claim(sessions)
            owner = generation.OWNER.set((301, token))
            original = AsyncSession.commit

            async def fail_commit(session):
                raise RuntimeError("injected_commit_failure")

            try:
                monkeypatch.setattr(AsyncSession, "commit", fail_commit)
                with pytest.raises(RuntimeError, match="injected_commit_failure"):
                    await main.finalize_chat_response(
                        "fixture", main.Principal(11, 1, "fixture", True), status="failed",
                        answer="A_PART", token_usage={}, reason="provider_error",
                    )
                monkeypatch.setattr(AsyncSession, "commit", original)
                async with sessions() as session:
                    assert (await session.get(ChatMessage, 301)).status == "generating"
                    assert len(list(await session.scalars(select(ChatMessage)))) == 1
                    assert not list(await session.scalars(select(ChatEvent)))
                chunks = await main.finalize_chat_response(
                    "fixture", main.Principal(11, 1, "fixture", True), status="failed",
                    answer="A_PART", token_usage={}, reason="provider_error",
                )
                assert len(chunks) == 2
                with pytest.raises(generation.LeaseLost):
                    await main.finalize_chat_response(
                        "fixture", main.Principal(11, 1, "fixture", True), status="failed",
                        answer="duplicate", token_usage={},
                    )
                async with sessions() as session:
                    assert len(list(await session.scalars(select(ChatMessage)))) == 2
                    assert [e.event_type for e in await session.scalars(select(ChatEvent).order_by(ChatEvent.sequence))] == ["generationerror", "done"]
            finally:
                generation.OWNER.reset(owner)
    asyncio.run(run())


def test_revoked_lease_never_falls_back_or_rewrites_terminal(monkeypatch, tmp_path):
    from eic import main
    from eic.core import generation
    from eic.models import AIModel, ChatMessage, ChatEvent
    calls = []

    async def run():
        async with fixture_db(monkeypatch, tmp_path, is_super_admin=True) as sessions:
            async with sessions() as session:
                for mid in (201, 202):
                    session.add(AIModel(id=mid, tenant_id=1, model_type="chat", provider="openai",
                                        model_name=str(mid), endpoint="http://fixture.invalid", enabled=True))
                await session.commit()

            async def provider(model, question, **kwargs):
                calls.append(model.id)
                yield "A_PART"
                async with sessions() as session:
                    await generation.cancel_request(session, await session.get(ChatMessage, 301))
                    await session.commit()
                yield "LATE"

            monkeypatch.setattr(main, "stream_chat_completion", provider)
            chunks = [c async for c in main.sse_events("fixture", main.Principal(11, 1, "fixture", True))]
            assert len(calls) == 1 and "LATE" not in "".join(chunks)
            async with sessions() as session:
                assert (await session.get(ChatMessage, 301)).status == "cancelled"
                events = list(await session.scalars(select(ChatEvent)))
                assert sum(e.event_type == "done" for e in events) == 1
                assert not any(e.event_type == "generationerror" for e in events)
    asyncio.run(run())


@pytest.mark.parametrize("with_partial", [False, True])
def test_cancel_stalled_read_replays_terminal(monkeypatch, tmp_path, with_partial):
    from eic import main
    from eic.core import generation
    from eic.models import AIModel, ChatMessage, ChatEvent
    calls, closes = [], []
    monkeypatch.setattr(generation, "HEARTBEAT_SECONDS", .05)

    async def run():
        started = asyncio.Event()
        never_released = asyncio.Event()
        async with fixture_db(monkeypatch, tmp_path, is_super_admin=True) as sessions:
            async with sessions() as session:
                session.add(AIModel(id=201, tenant_id=1, model_type="chat", provider="openai",
                                    model_name="A", endpoint="http://fixture.invalid", enabled=True))
                await session.commit()

            async def provider(model, question, **kwargs):
                calls.append(model.id)
                try:
                    if with_partial:
                        yield "A_PART"
                    started.set()
                    await never_released.wait()
                    yield "LATE"
                finally:
                    closes.append(model.id)

            monkeypatch.setattr(main, "stream_chat_completion", provider)
            async def consume():
                return [c async for c in main.sse_events("fixture", main.Principal(11, 1, "fixture", True))]
            task = asyncio.create_task(consume())
            await asyncio.wait_for(started.wait(), 3)
            async with sessions() as session:
                await generation.cancel_request(session, await session.get(ChatMessage, 301))
                await session.commit()
            chunks = await asyncio.wait_for(task, 3)
            assert calls == closes == [201] and "LATE" not in "".join(chunks)
            assert sum("event: done\n" in c for c in chunks) == 1
            async with sessions() as session:
                assert (await session.get(ChatMessage, 301)).status == "cancelled"
                assert sum(e.event_type == "done" for e in await session.scalars(select(ChatEvent))) == 1
    asyncio.run(run())


def test_fence_renewal_with_loaded_sqlite_lease(monkeypatch, tmp_path):
    """SQLite 返回无时区值；租约条件必须由数据库判断，而非 ORM 内存比较。"""
    from eic.core import generation
    from eic.models import GenerationLease

    async def run():
        async with fixture_db(monkeypatch, tmp_path, is_super_admin=True) as sessions:
            token = await claim(sessions)
            owner = generation.OWNER.set((301, token))
            try:
                async with sessions() as session:
                    lease = await session.get(GenerationLease, 301)
                    assert lease is not None and lease.expires_at.tzinfo is None
                    await generation.fence(session)
                    await session.commit()
                    await session.refresh(lease)
                    assert lease.state == "active" and lease.owner_token == token
            finally:
                generation.OWNER.reset(owner)
    asyncio.run(run())
