"""P13-03 会话记忆分层、跨 session 召回和冲突事实排序。"""
import asyncio
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from test_id_generation_safety import fixture_db


def test_session_memory_requires_owned_session_and_search_is_scoped(monkeypatch, tmp_path):
    from eic import main
    from eic.models import AgentMemory, ChatSession

    async def run():
        monkeypatch.setattr(main, "ensure_permission", AsyncMock())
        async with fixture_db(monkeypatch, tmp_path) as sessions:
            principal = main.Principal(11, 1, "fixture", False)
            async with sessions() as session:
                from memory_fixture import enable_memory
                await enable_memory(session)
                with pytest.raises(HTTPException) as error:
                    await main.memory_save({"content": "short-without-session", "memoryType": "short_term"}, principal, session)
                assert error.value.status_code == 400
                own = await main.memory_save({
                    "content": "short-current-session", "memoryType": "short_term", "sessionId": 101,
                    "memoryKey": "preference:theme",
                }, principal, session)
                row = await session.get(AgentMemory, own["id"])
                assert (row.session_id, row.memory_type, row.memory_key) == (101, "short_term", "preference:theme")
                session.add(ChatSession(id=102, tenant_id=1, owner_id=11, title="other fixture session"))
                await session.flush()
                session.add(AgentMemory(
                    id=await main.new_id(session), tenant_id=1, owner_id=11, session_id=102,
                    content="short-other-session", memory_type="session",
                ))
                session.add(AgentMemory(
                    id=await main.new_id(session), tenant_id=1, owner_id=11,
                    content="long-cross-session", memory_type="long_term",
                ))
                await session.commit()
                assert [item["content"] for item in await main.memory_search("short-", principal, session)] == []
                rows = await main.memory_search("", principal, session, sessionId=101)
                contents = {item["content"] for item in rows}
                assert "short-current-session" in contents
                assert "short-other-session" not in contents
                assert "long-cross-session" in contents

    asyncio.run(run())


def test_long_term_conflict_uses_priority_time_confidence_and_emits_warning(monkeypatch, tmp_path):
    from eic import main
    from eic.models import AgentMemory

    async def run():
        monkeypatch.setattr(main, "ensure_permission", AsyncMock())
        async with fixture_db(monkeypatch, tmp_path) as sessions:
            principal = main.Principal(11, 1, "fixture", False)
            async with sessions() as session:
                older = main._memory_datetime("2026-01-01T00:00:00+00:00", "生效时间")
                newer = main._memory_datetime("2026-02-01T00:00:00+00:00", "生效时间")
                session.add_all([
                    AgentMemory(id=701, tenant_id=1, owner_id=11, content="事实旧值", memory_type="fact",
                                memory_key="employee:department", source_priority=10, confidence=0.99,
                                importance=0.9, valid_from=older),
                    AgentMemory(id=702, tenant_id=1, owner_id=11, content="事实新值", memory_type="fact",
                                memory_key="employee:department", source_priority=20, confidence=0.4,
                                importance=0.8, valid_from=newer),
                ])
                await session.commit()
                rows = await main.memory_search("事实", principal, session)
                assert [item["id"] for item in rows] == [702]
                assert rows[0]["conflict"]["candidateIds"] == [702, 701]
                assert rows[0]["conflict"]["lowConfidence"] is True
                assert rows[0]["conflict"]["warning"] == "存在低置信度冲突，建议人工确认"

    asyncio.run(run())


def test_memory_types_and_scope_parameters_reject_invalid_values(monkeypatch, tmp_path):
    from eic import main

    async def run():
        monkeypatch.setattr(main, "ensure_permission", AsyncMock())
        async with fixture_db(monkeypatch, tmp_path) as sessions:
            principal = main.Principal(11, 1, "fixture", False)
            async with sessions() as session:
                from eic.models import AgentMemory
                result = await main.memory_save({'content':'explicit-user-save','sourcePriority':101},principal,session)
                assert (await session.get(AgentMemory,result['id'])).source_priority == 100
                with pytest.raises(HTTPException) as type_error:
                    await main.memory_search("", principal, session, memoryTypes="long_term,unknown")
                assert type_error.value.status_code == 400
                with pytest.raises(HTTPException) as session_error:
                    await main.memory_search("", principal, session, sessionId=999)
                assert session_error.value.status_code == 404

    asyncio.run(run())
