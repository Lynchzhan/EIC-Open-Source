"""P13-01 记忆所有权边界；只使用独立合成库，不调用模型。"""
import asyncio
from unittest.mock import AsyncMock

from fastapi import HTTPException
import pytest
from sqlalchemy import select

from test_id_generation_safety import fixture_db


async def seed_memories(session):
    from eic.models import AgentMemory, Tenant, User

    session.add(Tenant(id=2, code=2, name="memory-other-tenant"))
    await session.flush()
    session.add_all([
        User(id=12, tenant_id=1, username="memory-peer", password_hash="unused"),
        User(id=21, tenant_id=2, username="memory-foreign", password_hash="unused"),
    ])
    await session.flush()
    session.add_all([
        AgentMemory(id=801, tenant_id=1, owner_id=11, agent_id=7,
                    content="scope-fixture-own-agent7"),
        AgentMemory(id=802, tenant_id=1, owner_id=12, agent_id=7,
                    content="scope-fixture-other-user"),
        AgentMemory(id=803, tenant_id=2, owner_id=21, agent_id=7,
                    content="scope-fixture-other-tenant"),
        AgentMemory(id=804, tenant_id=1, owner_id=11, agent_id=8,
                    content="scope-fixture-own-agent8"),
        AgentMemory(id=805, tenant_id=1, owner_id=11, agent_id=None,
                    content="scope-fixture-unscoped"),
    ])
    await session.commit()


@pytest.mark.parametrize("is_admin", [False, True])
def test_memory_search_enforces_owner_even_for_admin(monkeypatch, tmp_path, is_admin):
    from eic import main

    async def run():
        monkeypatch.setattr(main, "ensure_permission", AsyncMock())
        async with fixture_db(monkeypatch, tmp_path, is_super_admin=is_admin) as sessions:
            principal = main.Principal(11, 1, "fixture", is_admin)
            async with sessions() as session:
                await seed_memories(session)
                rows = await main.memory_search("scope-fixture", principal, session)
                assert {row["id"] for row in rows} == {801, 804, 805}
                assert all("other" not in row["content"] for row in rows)
                main.ensure_permission.assert_awaited_once_with("ai.chat.read", principal, session)

    asyncio.run(run())


def test_explicit_agent_filter_keeps_owner_and_tenant_boundaries(monkeypatch, tmp_path):
    from eic import main

    async def run():
        monkeypatch.setattr(main, "ensure_permission", AsyncMock())
        async with fixture_db(monkeypatch, tmp_path) as sessions:
            principal = main.Principal(11, 1, "fixture", False)
            async with sessions() as session:
                await seed_memories(session)
                rows = await main.memory_search("scope-fixture", principal, session, agentId=7)
                assert [row["id"] for row in rows] == [801]

    asyncio.run(run())


@pytest.mark.parametrize("memory_id", [802, 803, 999])
def test_memory_delete_rejects_foreign_or_missing_ids(monkeypatch, tmp_path, memory_id):
    from eic import main
    from eic.models import AgentMemory

    async def run():
        monkeypatch.setattr(main, "ensure_permission", AsyncMock())
        async with fixture_db(monkeypatch, tmp_path, is_super_admin=True) as sessions:
            principal = main.Principal(11, 1, "fixture", True)
            async with sessions() as session:
                await seed_memories(session)
                with pytest.raises(HTTPException) as error:
                    await main.memory_delete(memory_id, principal, session)
                assert error.value.status_code == 404
                assert error.value.detail["errMsg"] == "记忆不存在或不属于当前用户"
                assert set(await session.scalars(select(AgentMemory.id))) == {801, 802, 803, 804, 805}
                main.ensure_permission.assert_awaited_once_with("memory.write", principal, session)

    asyncio.run(run())


def test_memory_delete_removes_only_owned_row_and_search_result(monkeypatch, tmp_path):
    from eic import main
    from eic.models import AgentMemory

    async def run():
        monkeypatch.setattr(main, "ensure_permission", AsyncMock())
        async with fixture_db(monkeypatch, tmp_path) as sessions:
            principal = main.Principal(11, 1, "fixture", False)
            async with sessions() as session:
                await seed_memories(session)
                assert await main.memory_delete(801, principal, session) is True
                assert (await session.get(AgentMemory, 801)).status == 'forgotten'
                from eic.models import MemoryTombstone
                assert await session.scalar(select(MemoryTombstone).where(MemoryTombstone.memory_id==801)) is not None
                rows = await main.memory_search("scope-fixture", principal, session)
                assert {row["id"] for row in rows} == {804, 805}
                assert set(await session.scalars(select(AgentMemory.id).where(AgentMemory.status=='active'))) == {802, 803, 804, 805}
                tombstone = await session.scalar(select(MemoryTombstone).where(MemoryTombstone.memory_id==801))
                assert await main.memory_delete(801, principal, session) is True
                assert list(await session.scalars(select(MemoryTombstone.id).where(MemoryTombstone.memory_id==801))) == [tombstone.id]
                assert 801 not in {row['id'] for row in await main.memory_search('scope-fixture', principal, session)}

    asyncio.run(run())


def test_memory_save_ignores_spoofed_owner_and_tenant(monkeypatch, tmp_path):
    from eic import main
    from eic.models import AgentMemory

    async def run():
        monkeypatch.setattr(main, "ensure_permission", AsyncMock())
        async with fixture_db(monkeypatch, tmp_path) as sessions:
            principal = main.Principal(11, 1, "fixture", False)
            async with sessions() as session:
                from memory_fixture import enable_memory
                await enable_memory(session)
                result = await main.memory_save({
                    "content": "scope-fixture-written", "ownerId": 12, "tenantId": 2,
                    "agentId": 7,
                }, principal, session)
                row = await session.get(AgentMemory, result["id"])
                assert (row.tenant_id, row.owner_id, row.agent_id) == (1, 11, 7)

    asyncio.run(run())
