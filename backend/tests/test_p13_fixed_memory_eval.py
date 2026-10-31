"""固定合成集，不是 LongMemEval 官方结果或真实模型质量证明。"""
import asyncio
from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.exc import SQLAlchemyError

from eic.ai.memory_context import recall_memory
from test_id_generation_safety import fixture_db

CASES = json.loads((Path(__file__).parent / "fixtures/long_memory_cases.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("case", CASES, ids=[item["id"] for item in CASES])
def test_fixed_memory_case(monkeypatch, tmp_path, case):
    from eic.models import AgentMemory

    async def run():
        async with fixture_db(monkeypatch, tmp_path) as sessions:
            async with sessions() as session:
                for item in case["rows"]:
                    values = {**item}
                    expired, future = values.pop("expired", False), values.pop("future", False)
                    if expired:
                        values["expires_at"] = datetime.now(UTC) - timedelta(days=1)
                    if future:
                        values["valid_from"] = datetime.now(UTC) + timedelta(days=1)
                    session.add(AgentMemory(**{"tenant_id": 1, "owner_id": 11, **values}))
                await session.commit()
                rows, _ = await recall_memory(session, tenant_id=1, owner_id=11, session_id=101,
                                               agent_id=None, query=case["query"])
                assert [item["id"] for item in rows] == case["expected"]
    asyncio.run(run())


def test_memory_store_failure_is_empty_and_rolls_back(monkeypatch, tmp_path):
    async def run():
        async with fixture_db(monkeypatch, tmp_path) as sessions:
            async with sessions() as session:
                monkeypatch.setattr(session, "execute", AsyncMock(side_effect=SQLAlchemyError("PRIVATE_DB_ERROR")))
                rollback = AsyncMock()
                monkeypatch.setattr(session, "rollback", rollback)
                rows, info = await recall_memory(session, tenant_id=1, owner_id=11, session_id=101,
                                                 agent_id=None, query="中文")
                assert rows == [] and info["errorCode"] == "memory_store_unavailable"
                rollback.assert_awaited_once()
    asyncio.run(run())
