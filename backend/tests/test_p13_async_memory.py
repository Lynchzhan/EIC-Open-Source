import asyncio
from unittest.mock import AsyncMock
from contextlib import asynccontextmanager

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select
from eic.models import AgentMemory, OutboxEvent
from test_id_generation_safety import fixture_db as chat_fixture_db


@asynccontextmanager
async def fixture_db(monkeypatch, tmp_path, **kwargs):
    from eic.models import Permission, RolePermission
    async with chat_fixture_db(monkeypatch, tmp_path, **kwargs) as sessions:
        async with sessions() as session:
            session.add(Permission(id="memory.write", code="memory.write", name="fixture", category="ai"))
            await session.flush()
            session.add(RolePermission(role_id=599, permission_id="memory.write"))
            await session.commit()
        yield sessions


def test_pending_memory_is_invisible_and_receipt_is_atomic_idempotent(monkeypatch, tmp_path):
    from eic import main
    from eic.core import outbox

    async def run():
        monkeypatch.setattr(main, "ensure_permission", AsyncMock())
        async with fixture_db(monkeypatch, tmp_path) as sessions:
            monkeypatch.setattr(outbox, "SessionLocal", sessions)
            principal = main.Principal(11, 1, "fixture", False)
            async with sessions() as session:
                from memory_fixture import enable_memory
                from eic.models import ChatMessage
                await enable_memory(session)
                (await session.get(ChatMessage,301)).status='completed'
                queued = await main.memory_save(
                    {"content": "持久记忆", "asyncWrite": True, "sourceMessageId": 301, 'sessionId':101}, principal, session,
                )
                assert await main.memory_search("持久", principal, session) == []
            results = await asyncio.gather(*(
                outbox.consume_event({}, queued["writeJobId"], 1) for _ in range(3)
            ))
            assert all(item["status"] == "completed" for item in results)
            async with sessions() as session:
                assert await session.scalar(select(func.count()).select_from(AgentMemory)) == 1
                memory = await session.get(AgentMemory, queued["id"])
                event = await session.get(OutboxEvent, queued["writeJobId"])
                assert memory.status == "active" and event.consume_status == "completed"
                await session.delete(memory)
                await session.commit()
            assert await outbox.consume_event({}, queued["writeJobId"], 1) == results[0]
            async with sessions() as session:
                assert await session.scalar(select(func.count()).select_from(AgentMemory)) == 0
    asyncio.run(run())


def test_idempotency_binds_owner_payload_and_prevents_recreation(monkeypatch, tmp_path):
    from eic import main
    from eic.core.memory_writes import consume_memory_write

    async def run():
        monkeypatch.setattr(main, "ensure_permission", AsyncMock())
        async with fixture_db(monkeypatch, tmp_path) as sessions:
            principal = main.Principal(11, 1, "fixture", False)
            payload = {"content": "中文偏好", "asyncWrite": True, "idempotencyKey": "stable"}
            async with sessions() as session:
                first = await main.memory_save(payload, principal, session)
            async with sessions() as session:
                assert await main.memory_save(payload, principal, session) == first
            async with sessions() as session:
                with pytest.raises(HTTPException) as error:
                    await main.memory_save({**payload, "content": "不同内容"}, principal, session)
                assert error.value.status_code == 409
            await consume_memory_write(sessions, first["writeJobId"], 1)
            async with sessions() as session:
                await session.delete(await session.get(AgentMemory, first["id"]))
                await session.commit()
            async with sessions() as session:
                with pytest.raises(HTTPException) as error:
                    await main.memory_save(payload, principal, session)
                assert error.value.status_code == 409
    asyncio.run(run())


def test_interrupted_commit_preserves_pending_job_for_safe_retry(monkeypatch, tmp_path):
    from eic import main
    from eic.core.memory_writes import consume_memory_write

    async def run():
        monkeypatch.setattr(main, "ensure_permission", AsyncMock())
        async with fixture_db(monkeypatch, tmp_path) as sessions:
            async with sessions() as session:
                queued = await main.memory_save({"content": "可恢复记忆", "asyncWrite": True},
                                                main.Principal(11, 1, "fixture", False), session)
            @asynccontextmanager
            async def broken_sessions():
                async with sessions() as session:
                    session.commit = AsyncMock(side_effect=RuntimeError("COMMIT_FAULT"))
                    yield session
            with pytest.raises(RuntimeError, match="COMMIT_FAULT"):
                await consume_memory_write(broken_sessions, queued["writeJobId"], 1)
            async with sessions() as session:
                assert (await session.get(AgentMemory, queued["id"])).status == "pending"
                assert (await session.get(OutboxEvent, queued["writeJobId"])).consume_status == "pending"
            assert (await consume_memory_write(sessions, queued["writeJobId"], 1))["status"] == "completed"
    asyncio.run(run())


def test_deleted_pending_memory_not_resurrected(monkeypatch, tmp_path):
    from eic import main
    from eic.core.memory_writes import consume_memory_write

    async def run():
        monkeypatch.setattr(main, "ensure_permission", AsyncMock())
        async with fixture_db(monkeypatch, tmp_path) as sessions:
            async with sessions() as session:
                queued = await main.memory_save({"content": "待写入", "asyncWrite": True},
                                                main.Principal(11, 1, "fixture", False), session)
                await session.delete(await session.get(AgentMemory, queued["id"]))
                await session.commit()
            assert (await consume_memory_write(sessions, queued["writeJobId"], 1))["status"] == "cancelled"
    asyncio.run(run())


def test_memory_queue_failure_does_not_invalidate_completed_answer(monkeypatch, tmp_path):
    from eic import main
    from eic.models import AIModel, ChatMessage, Resource
    from test_chat_subscriptions import collect

    async def run():
        async def provider(*args, **kwargs):
            yield "答案已完成"
        monkeypatch.setattr(main, "stream_chat_completion", provider)
        monkeypatch.setattr(main, "memory_save", AsyncMock(side_effect=RuntimeError("PRIVATE_STORAGE_ERROR")))
        async with fixture_db(monkeypatch, tmp_path, is_super_admin=True) as sessions:
            async with sessions() as session:
                from memory_fixture import enable_memory
                await enable_memory(session)
                session.add(AIModel(id=201, tenant_id=1, model_type="chat", provider="openai",
                                    model_name="A", endpoint="http://fixture"))
                resource = await session.get(Resource, 301)
                resource.data = {"memoryWrite": {"content": "中文偏好"}}
                await session.commit()
            output = await collect(main.sse_events("fixture", main.Principal(11, 1, "fixture", True)))
            assert sum("event: done\n" in chunk for chunk in output) == 1
            await asyncio.sleep(0.05)
            async with sessions() as session:
                assert (await session.get(ChatMessage, 301)).status == "completed"
            assert not any("PRIVATE_STORAGE_ERROR" in chunk for chunk in output)
            main.memory_save.assert_awaited_once()
    asyncio.run(run())
