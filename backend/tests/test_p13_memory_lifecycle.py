"""P13-02 记忆生命周期；云端隔离数据库验证。"""
import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from test_id_generation_safety import fixture_db


def at_offset(**kwargs):
    return datetime.now(UTC) + timedelta(**kwargs)


async def seed_lifecycle_memories(session):
    from eic.models import AgentMemory, Resource, Tenant, User

    session.add(Tenant(id=2, code=2, name="memory-lifecycle-other-tenant"))
    await session.flush()
    session.add(Resource(id=7, tenant_id=1, owner_id=11, resource_type="ai_apps",
                         name="memory-lifecycle-agent", data={"enabled": True, "memorySettings": {"mode": "manual"}}))
    session.add_all([
        User(id=12, tenant_id=1, username="memory-lifecycle-peer", password_hash="unused"),
        User(id=21, tenant_id=2, username="memory-lifecycle-foreign", password_hash="unused"),
    ])
    await session.flush()
    session.add_all([
        AgentMemory(id=901, tenant_id=1, owner_id=11, agent_id=7,
                    content="lifecycle-active", memory_type="long_term",
                    importance=0.9, confidence=0.95, status="active"),
        AgentMemory(id=902, tenant_id=1, owner_id=11, agent_id=7,
                    content="lifecycle-expired", expires_at=at_offset(seconds=-30),
                    status="active"),
        AgentMemory(id=903, tenant_id=1, owner_id=11, agent_id=7,
                    content="lifecycle-not-yet-valid", valid_from=at_offset(seconds=30),
                    status="active"),
        AgentMemory(id=904, tenant_id=1, owner_id=11, agent_id=7,
                    content="lifecycle-archived", status="archived"),
        AgentMemory(id=905, tenant_id=1, owner_id=12, agent_id=7,
                    content="lifecycle-other-user", status="active"),
        AgentMemory(id=906, tenant_id=2, owner_id=21, agent_id=7,
                    content="lifecycle-other-tenant", status="active"),
    ])
    await session.commit()


def test_search_excludes_expired_future_archived_and_foreign_memories(monkeypatch, tmp_path):
    from eic import main

    async def run():
        monkeypatch.setattr(main, "ensure_permission", AsyncMock())
        async with fixture_db(monkeypatch, tmp_path) as sessions:
            principal = main.Principal(11, 1, "fixture", False)
            async with sessions() as session:
                await seed_lifecycle_memories(session)
                rows = await main.memory_search("lifecycle-", principal, session, agentId=7)
                assert [row["id"] for row in rows] == [901]
                assert rows[0]["memoryType"] == "long_term"
                assert rows[0]["importance"] == pytest.approx(0.9)
                assert rows[0]["confidence"] == pytest.approx(0.95)
                assert rows[0]["status"] == "active"

    asyncio.run(run())


def test_archive_and_restore_are_owner_scoped_and_searchable_after_restore(monkeypatch, tmp_path):
    from eic import main
    from eic.models import AgentMemory

    async def run():
        monkeypatch.setattr(main, "ensure_permission", AsyncMock())
        async with fixture_db(monkeypatch, tmp_path) as sessions:
            principal = main.Principal(11, 1, "fixture", False)
            async with sessions() as session:
                await seed_lifecycle_memories(session)
                assert await main.memory_archive(901, principal, session) is True
                assert await main.memory_search("lifecycle-active", principal, session) == []
                row = await session.get(AgentMemory, 901)
                assert row.status == "archived"
                assert await main.memory_restore(901, principal, session) is True
                rows = await main.memory_search("lifecycle-active", principal, session)
                assert [item["id"] for item in rows] == [901]
                assert (await session.get(AgentMemory, 901)).status == "active"

    asyncio.run(run())


@pytest.mark.parametrize("memory_id", [905, 906, 999])
def test_archive_and_restore_reject_foreign_or_missing_memory(monkeypatch, tmp_path, memory_id):
    from eic import main

    async def run():
        monkeypatch.setattr(main, "ensure_permission", AsyncMock())
        async with fixture_db(monkeypatch, tmp_path, is_super_admin=True) as sessions:
            principal = main.Principal(11, 1, "fixture", True)
            async with sessions() as session:
                await seed_lifecycle_memories(session)
                with pytest.raises(main.HTTPException) as archive_error:
                    await main.memory_archive(memory_id, principal, session)
                assert archive_error.value.status_code == 404
                with pytest.raises(main.HTTPException) as restore_error:
                    await main.memory_restore(memory_id, principal, session)
                assert restore_error.value.status_code == 404

    asyncio.run(run())


def test_save_persists_lifecycle_fields_and_never_accepts_status_spoof(monkeypatch, tmp_path):
    from eic import main
    from eic.models import AgentMemory, ChatMessage, ChatSession

    async def run():
        monkeypatch.setattr(main, "ensure_permission", AsyncMock())
        async with fixture_db(monkeypatch, tmp_path) as sessions:
            principal = main.Principal(11, 1, "fixture", False)
            async with sessions() as session:
                await seed_lifecycle_memories(session)
                (await session.get(ChatSession, 101)).app_id = 7
                (await session.get(ChatMessage, 301)).status = "completed"
                await session.commit()
                result = await main.memory_save({
                    "content": "lifecycle-written",
                    "agentId": 7,
                    "sessionId": 101,
                    "memoryType": "preference",
                    "importance": 0.8,
                    "confidence": 0.7,
                    "sourceMessageId": 301,
                    "validFrom": at_offset(seconds=-5).isoformat(),
                    "validUntil": at_offset(seconds=300).isoformat(),
                    "expiresAt": at_offset(seconds=600).isoformat(),
                    "status": "archived",
                    "ownerId": 12,
                    "tenantId": 2,
                }, principal, session)
                row = await session.get(AgentMemory, result["id"])
                assert (row.tenant_id, row.owner_id, row.status) == (1, 11, "active")
                assert (row.memory_type, row.importance, row.confidence) == ("preference", 0.8, 0.7)
                assert row.source_message_id == 301
                assert row.valid_from is not None and row.valid_until is not None and row.expires_at is not None

    asyncio.run(run())


@pytest.mark.parametrize("payload", [
    {"content": "bad-importance", "importance": 2},
    {"content": "bad-confidence", "confidence": -0.1},
    {"content": "bad-time", "expiresAt": "not-a-date"},
])
def test_save_rejects_invalid_lifecycle_values(monkeypatch, tmp_path, payload):
    from eic import main

    async def run():
        monkeypatch.setattr(main, "ensure_permission", AsyncMock())
        async with fixture_db(monkeypatch, tmp_path) as sessions:
            principal = main.Principal(11, 1, "fixture", False)
            async with sessions() as session:
                with pytest.raises(main.HTTPException) as error:
                    await main.memory_save(payload, principal, session)
                assert error.value.status_code == 400
                expected_labels = {
                    "importance": "重要性",
                    "confidence": "置信度",
                    "expiresAt": "过期时间",
                }
                assert any(label in error.value.detail["errMsg"]
                           for key, label in expected_labels.items() if key in payload)

    asyncio.run(run())
