import asyncio
from datetime import UTC, datetime, timedelta

from eic.ai.memory_context import recall_memory
from test_id_generation_safety import fixture_db


async def seed_ready_vector(session, memory_id=711):
    from eic.models import AgentMemory, AIModel, MemoryVector
    from eic.core.memory_index import fingerprint
    from eic.ai.memory_context import content_hash
    memory=await session.get(AgentMemory,memory_id)
    memory.content_hash=content_hash(memory.content)
    await session.flush()
    session.add(MemoryVector(id=901,tenant_id=1,owner_id=11,memory_id=memory_id,
        memory_version=memory.version,content_hash=memory.content_hash,
        model_fingerprint=fingerprint(await session.get(AIModel,801)),
        dimensions=2,vector=[1.,0.],status='ready'))
    await session.commit()


def test_hybrid_filters_before_embedding_and_resolves_all_conflicts(monkeypatch, tmp_path):
    from eic.models import AgentMemory, AIModel

    async def run():
        async with fixture_db(monkeypatch, tmp_path) as sessions:
            async with sessions() as session:
                session.add(AIModel(id=801, tenant_id=1, model_name="fixture",
                                    model_type="embedding", provider="openai", endpoint="http://fixture",
                                    enabled=True))
                session.add_all([
                    AgentMemory(id=710, tenant_id=1, owner_id=11, content="上海", memory_key="city"),
                    AgentMemory(id=711, tenant_id=1, owner_id=11, content="杭州", memory_key="city",
                                source_priority=10, confidence=0.4),
                    AgentMemory(id=712, tenant_id=1, owner_id=12, content="private"),
                    AgentMemory(id=713, tenant_id=2, owner_id=11, content="tenant-private"),
                    AgentMemory(id=714, tenant_id=1, owner_id=11, agent_id=42, content="agent-private"),
                    AgentMemory(id=715, tenant_id=1, owner_id=11, session_id=102,
                                memory_type="session", content="session-private"),
                    AgentMemory(id=716, tenant_id=1, owner_id=11, content="expired",
                                expires_at=datetime.now(UTC) - timedelta(seconds=1)),
                ])
                await session.commit()
                await seed_ready_vector(session)
                seen = []
                async def embed(model, texts, **kwargs):
                    seen.extend(texts)
                    return [[1., 0.] for _ in texts]
                rows, info = await recall_memory(
                    session, tenant_id=1, owner_id=11, session_id=101, agent_id=None,
                    query="住所", embedding_model_id=801, embedder=embed,
                )
                assert seen == ["住所"]
                assert [item["id"] for item in rows] == [711]
                assert rows[0]["conflictCount"] == 2
                assert rows[0]["lowConfidenceConflict"]
                assert info["mode"] == "hybrid"
    asyncio.run(run())


def test_bad_vectors_fall_back_to_keyword_without_scope_change(monkeypatch, tmp_path):
    from eic.models import AgentMemory, AIModel

    async def run():
        async with fixture_db(monkeypatch, tmp_path) as sessions:
            async with sessions() as session:
                session.add(AIModel(id=801, tenant_id=1, model_name="fixture",
                                    model_type="embedding", provider="openai", endpoint="http://fixture"))
                session.add(AgentMemory(id=711, tenant_id=1, owner_id=11, content="喜欢中文回答"))
                await session.commit()
                await seed_ready_vector(session)
                async def embed(*args, **kwargs):
                    return [[float("nan"),1.]]
                rows, info = await recall_memory(
                    session, tenant_id=1, owner_id=11, session_id=101, agent_id=None,
                    query="中文", embedding_model_id=801, embedder=embed,
                )
                assert [row["id"] for row in rows] == [711]
                assert info["errorCode"] == "memory_embedding_failed"
                assert info["mode"] == "keyword"
    asyncio.run(run())


def test_deleted_or_superseded_memory_invalidates_snapshot(monkeypatch, tmp_path):
    from eic.ai.memory_context import memory_snapshot_valid
    from eic.models import AgentMemory

    async def run():
        async with fixture_db(monkeypatch, tmp_path) as sessions:
            scope = dict(tenant_id=1, owner_id=11, session_id=101, agent_id=None)
            async with sessions() as session:
                session.add(AgentMemory(id=711, tenant_id=1, owner_id=11,
                                        content="中文偏好", memory_key="language"))
                await session.commit()
                rows, _ = await recall_memory(session, query="中文", **scope)
                assert await memory_snapshot_valid(session, rows, **scope)
                session.add(AgentMemory(id=712, tenant_id=1, owner_id=11,
                                        content="英文偏好", memory_key="language", source_priority=20))
                await session.commit()
                assert not await memory_snapshot_valid(session, rows, **scope)
                await session.delete(await session.get(AgentMemory, 711))
                await session.commit()
                assert not await memory_snapshot_valid(session, rows, **scope)
    asyncio.run(run())


def test_chat_producer_includes_scoped_memory(monkeypatch, tmp_path):
    from eic import main
    from eic.models import AgentMemory, AIModel, ChatMessage
    from test_chat_subscriptions import collect

    async def run():
        seen = []
        async def provider(model, question, **kwargs):
            seen.extend(kwargs["messages"])
            yield "中文回答"
        monkeypatch.setattr(main, "stream_chat_completion", provider)
        async with fixture_db(monkeypatch, tmp_path, is_super_admin=True) as sessions:
            async with sessions() as session:
                from memory_fixture import enable_memory
                await enable_memory(session)
                message = await session.get(ChatMessage, 301)
                message.content = "中文"
                session.add(AIModel(id=201, tenant_id=1, model_name="A", provider="openai",
                                    endpoint="http://fixture", model_type="chat"))
                session.add(AgentMemory(id=711, tenant_id=1, owner_id=11, agent_id=7,content="中文偏好"))
                session.add(AgentMemory(id=712, tenant_id=1, owner_id=12, agent_id=7,content="中文隐私"))
                await session.commit()
            chunks = await collect(main.sse_events("fixture", main.Principal(11, 1, "fixture", True)))
            assert any("中文回答" in chunk for chunk in chunks)
            assert any("中文偏好" in str(item) for item in seen)
            assert not any("中文隐私" in str(item) for item in seen)
    asyncio.run(run())
