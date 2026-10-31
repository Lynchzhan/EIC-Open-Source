"""P13-03 规模排序、外键清理和非法输入的独立回归。"""
import asyncio
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from sqlalchemy import text

from test_id_generation_safety import fixture_db


def test_conflict_winner_is_not_lost_after_two_hundred_candidates(monkeypatch, tmp_path):
    from eic import main
    from eic.models import AgentMemory

    async def run():
        monkeypatch.setattr(main, "ensure_permission", AsyncMock())
        async with fixture_db(monkeypatch, tmp_path) as sessions:
            async with sessions() as session:
                session.add_all([
                    AgentMemory(
                        id=1000 + index, tenant_id=1, owner_id=11, content="规模事实旧值",
                        memory_key="department", source_priority=1, confidence=0.9,
                    )
                    for index in range(201)
                ])
                session.add_all([
                    AgentMemory(
                        id=2000, tenant_id=1, owner_id=11, content="规模事实最新值",
                        memory_key="department", source_priority=90, confidence=0.95,
                    ),
                    AgentMemory(
                        id=2001, tenant_id=1, owner_id=11, content="规模事实独立值",
                        memory_key="office", source_priority=0,
                    ),
                ])
                await session.commit()
                rows = await main.memory_search(
                    "规模事实", main.Principal(11, 1, "fixture", False), session,
                )
                assert [row["id"] for row in rows] == [2000, 2001]
                conflict = rows[0]["conflict"]
                assert conflict["candidateCount"] == 202
                assert conflict["candidateIds"][0] == 2000
                assert len(conflict["candidateIds"]) <= 200
                assert conflict["candidateIdsTruncated"] is True

    asyncio.run(run())


def test_clear_mine_handles_memory_foreign_keys_and_preserves_other_users(monkeypatch, tmp_path):
    from eic import main
    from eic.models import AgentMemory, AgentRun, ChatSession, User

    async def run():
        monkeypatch.setattr(main, "ensure_permission", AsyncMock())
        async with fixture_db(monkeypatch, tmp_path) as sessions:
            async with sessions() as session:
                await session.execute(text("PRAGMA foreign_keys=ON"))
                assert await session.scalar(text("PRAGMA foreign_keys")) == 1
                session.add(User(
                    id=12, tenant_id=1, username="memory-review-peer", password_hash="unused",
                ))
                await session.flush()
                session.add(ChatSession(id=102, tenant_id=1, owner_id=12, title="peer"))
                await session.flush()
                session.add_all([
                    AgentMemory(
                        id=701, tenant_id=1, owner_id=11, session_id=101,
                        memory_type="short_term", content="temporary",
                    ),
                    AgentMemory(
                        id=702, tenant_id=1, owner_id=11, session_id=101,
                        memory_type="long_term", content="persistent",
                    ),
                    AgentMemory(
                        id=703, tenant_id=1, owner_id=12, session_id=102,
                        memory_type="session", content="peer memory",
                    ),
                    AgentRun(
                        run_id="memory-review-run", tenant_id=1, session_id=101,
                        trace_id="memory-review-trace", graph_version="fixture",
                        status="completed",
                    ),
                ])
                await session.commit()
                principal = main.Principal(11, 1, "fixture", False)
                result = await main.chats_clear_mine(
                    {"confirmText": "确认一键删除"}, principal, session,
                )
                assert result["ok"] is True and result["sessions"] == 1
                session.expire_all()
                assert await session.get(ChatSession, 101) is None
                assert await session.get(AgentMemory, 701) is None
                assert (await session.get(AgentMemory, 702)).session_id is None
                assert (await session.get(AgentRun, "memory-review-run")).session_id is None
                assert (await session.get(AgentMemory, 703)).session_id == 102
                assert await session.get(ChatSession, 102) is not None
                assert await session.scalar(text("PRAGMA foreign_keys")) == 1
                assert (await session.execute(text("PRAGMA foreign_key_check"))).all() == []
                assert (await main.chats_clear_mine(
                    {"confirmText": "确认一键删除"}, principal, session,
                ))["sessions"] == 0

    asyncio.run(run())


@pytest.mark.parametrize("priority", [1.5, -0.1, float("inf"), float("nan")])
def test_source_priority_rejects_non_integral_and_non_finite_values(priority):
    from eic import main

    with pytest.raises(HTTPException) as error:
        main._memory_priority(priority)
    assert error.value.status_code == 400
    assert error.value.detail["errMsg"] == "来源优先级必须介于 0 和 100 之间"


def test_all_rank_ties_are_deterministic_and_standalone_keys_do_not_collide(monkeypatch, tmp_path):
    from eic import main
    from eic.models import AgentMemory

    async def run():
        monkeypatch.setattr(main, "ensure_permission", AsyncMock())
        async with fixture_db(monkeypatch, tmp_path) as sessions:
            async with sessions() as session:
                created = main._memory_datetime("2026-01-01T00:00:00Z", "创建时间")
                session.add_all([
                    AgentMemory(
                        id=801 + index, tenant_id=1, owner_id=11, content="同级事实",
                        memory_key="same", source_priority=0, created_at=created,
                    )
                    for index in range(2)
                ] + [
                    AgentMemory(
                        id=803, tenant_id=1, owner_id=11, content="同级事实",
                        memory_key=None, created_at=created,
                    ),
                    AgentMemory(
                        id=804, tenant_id=1, owner_id=11, content="同级事实",
                        memory_key="803", created_at=created,
                    ),
                ])
                await session.commit()
                principal = main.Principal(11, 1, "fixture", False)
                first = await main.memory_search("同级事实", principal, session)
                second = await main.memory_search("同级事实", principal, session)
                assert first == second
                assert [row["id"] for row in first] == [804, 803, 802]
                assert first[-1]["conflict"]["candidateIds"] == [802, 801]

    asyncio.run(run())
