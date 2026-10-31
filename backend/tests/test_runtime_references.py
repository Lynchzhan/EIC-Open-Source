"""Real SQLite checks for tombstones, dedicated-only compatibility and queued work."""
import asyncio
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from eic.core.ids import initialize_allocator
from eic.core.runtime_refs import RuntimeReferenceUnavailable, require_runtime_resource
from eic.models import (AIModel, Base, KnowledgeBase, KnowledgeChunk,
                        KnowledgeDocument, KnowledgeIngestionRun, Resource, Tenant)


@pytest.mark.parametrize("state", ["active", "dedicated_only", "deleted", "disabled", "foreign", "wrong_type"])
def test_runtime_kb_state_real_database(tmp_path, state):
    async def run():
        engine = create_async_engine("sqlite+aiosqlite:///" + str(tmp_path / "refs.db"))
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with engine.begin() as c:
                await c.run_sync(Base.metadata.create_all)
            async with sessions() as s:
                s.add_all([Tenant(id=1, code=1, name="a"), Tenant(id=2, code=2, name="b")])
                await s.flush()
                s.add(KnowledgeBase(id=10, tenant_id=1, name="kb"))
                if state != "dedicated_only":
                    s.add(Resource(id=10, tenant_id=2 if state == "foreign" else 1,
                                   resource_type="skills" if state == "wrong_type" else "knowledge_bases",
                                   name="kb", is_deleted=state == "deleted",
                                   data={"enabled": False} if state == "disabled" else {}))
                await s.commit()
            async with sessions() as s:
                from fastapi import HTTPException
                from eic.knowledge.ops import retrieve_knowledge
                base = await s.get(KnowledgeBase, 10)
                if state in {"active", "dedicated_only"}:
                    _, base = await require_runtime_resource(s, 1, 10, "knowledge_bases",
                                                             KnowledgeBase, require_dedicated=True)
                    assert base.id == 10
                    assert (await retrieve_knowledge(s, tenant_id=1, base=base, query="fixture"))["results"] == []
                else:
                    with pytest.raises(RuntimeReferenceUnavailable):
                        await require_runtime_resource(s, 1, 10, "knowledge_bases",
                                                       KnowledgeBase, require_dedicated=True)
                    with pytest.raises(HTTPException) as error:
                        await retrieve_knowledge(s, tenant_id=1, base=base, query="fixture")
                    assert error.value.status_code == 404
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_revocation_between_dense_and_rerank_blocks_second_external_call(tmp_path,monkeypatch):
    from eic import main
    from eic.knowledge import ops
    from eic.knowledge.indexing import parameter_snapshot
    from eic.models import User
    from test_id_generation_safety import fixture_db
    calls=[]
    async def run():
        async with fixture_db(monkeypatch,tmp_path,is_super_admin=True) as sessions:
            monkeypatch.setattr(ops,"SessionLocal",sessions)
            async with sessions() as s:
                s.add_all([
                    AIModel(id=9,tenant_id=1,model_type="embedding",provider="openai",
                            model_name="embedding",endpoint="http://fixture.invalid",embedding_dimensions=1,enabled=True),
                    AIModel(id=8,tenant_id=1,model_type="rerank",provider="openai",
                            model_name="rerank",endpoint="http://fixture.invalid",enabled=True),
                ])
                s.add(KnowledgeBase(id=10,tenant_id=1,name="kb",embedding_model_id=9,rerank_model_id=8,
                                    retrieval_strategy="full"))
                s.add(Resource(id=10,tenant_id=1,resource_type="knowledge_bases",name="kb",data={}))
                s.add(KnowledgeDocument(id=20,tenant_id=1,knowledge_base_id=10,source_name="doc",content="foo",
                                        active_index_version=31, status="ready", index_status="ready"))
                s.add(KnowledgeChunk(id=30,tenant_id=1,knowledge_base_id=10,document_id=20,
                                    chunk_index=0,index_version=31,content="foo",content_hash="foo",attributes={}))
                await s.flush()
                s.add(KnowledgeIngestionRun(id=31,tenant_id=1,document_id=20,status="ready",
                                           parameters=parameter_snapshot(await s.get(KnowledgeBase,10),
                                                                         await s.get(AIModel,9))))
                await s.commit()
            async def embedding(*args,**kwargs):
                calls.append("embedding")
                async with sessions() as s:
                    user=await s.get(User,11)
                    user.is_active=False
                    await s.commit()
                return []
            async def rerank(*args,**kwargs):
                calls.append("rerank")
                raise AssertionError("revoked user must not call rerank")
            monkeypatch.setattr(ops,"embed_texts",embedding)
            monkeypatch.setattr(ops,"rerank_hits",rerank)
            async with sessions() as s:
                with pytest.raises(RuntimeReferenceUnavailable):
                    await main.retrieve_chat_documents(s,main.Principal(11,1,"fixture",True),10,"foo")
            assert calls==["embedding"]
    asyncio.run(run())


@pytest.mark.parametrize("state", ["active", "deleted", "disabled", "foreign", "wrong_type"])
def test_queued_document_rechecks_kb_before_side_effects(tmp_path, monkeypatch, state):
    from eic import workers
    from eic.knowledge import indexing

    async def run():
        engine = create_async_engine("sqlite+aiosqlite:///" + str(tmp_path / "worker.db"))
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        monkeypatch.setattr(indexing, "SessionLocal", sessions)
        monkeypatch.setattr(workers, "SessionLocal", sessions)
        embed = AsyncMock(side_effect=AssertionError("invalid queued work must not call a provider"))
        monkeypatch.setattr(indexing, "embed_texts", embed)
        try:
            async with engine.begin() as c:
                await c.run_sync(Base.metadata.create_all)
            async with sessions() as s:
                await initialize_allocator(s)
                s.add_all([Tenant(id=1, code=1, name="a"), Tenant(id=2, code=2, name="b")])
                await s.flush()
                s.add(AIModel(id=9, tenant_id=1, model_type="embedding", model_name="fixture",
                              endpoint="http://fixture.invalid", enabled=True, embedding_dimensions=1))
                s.add(KnowledgeBase(id=10, tenant_id=1, name="kb", embedding_model_id=9))
                s.add(Resource(id=10, tenant_id=2 if state == "foreign" else 1,
                               resource_type="skills" if state == "wrong_type" else "knowledge_bases",
                               name="kb", is_deleted=state == "deleted",
                               data={"enabled": False} if state == "disabled" else {}))
                s.add(KnowledgeDocument(id=20, tenant_id=1, knowledge_base_id=10, source_name="fixture",
                                       content="old content", status="queued_text"))
                s.add(KnowledgeChunk(id=30, tenant_id=1, knowledge_base_id=10, document_id=20,
                                    chunk_index=0, content="ORIGINAL_CHUNK", content_hash="original", attributes={}))
                await s.commit()
            if state == "active":
                # 正例只验证解析；embedding 的向量写入依赖由其他专项负责。
                monkeypatch.setattr(workers.outbox, "dispatch_one", AsyncMock(return_value=None))
                parsed = await workers.process_knowledge_document({}, 20, 1)
                assert parsed["status"] == "parsed_pending_embedding" and parsed["chunk_count"] > 0
            else:
                assert (await workers.process_knowledge_base({}, 10, 1))["status"] == "failed"
                assert (await workers.process_knowledge_document({}, 20, 1))["status"] in {"failed", "embedding_failed"}
                assert (await workers.process_document_embeddings({}, 20, 1))["status"] in {"failed", "embedding_failed"}
                async with sessions() as s:
                    # 当前排队文档应记录可见失败，不能永久停在queued；原索引与上游保持不动。
                    document = await s.get(KnowledgeDocument, 20)
                    assert document.status == document.index_status == "failed"
                    assert document.parse_status == "failed"
                    assert document.index_error == {"stage": "parse",
                        "code": "parse_failed:RuntimeReferenceUnavailable", "automaticRetry": False}
                    assert document.pending_index_version is None
                    resource = await s.get(Resource, 10)
                    assert resource.tenant_id == (2 if state == "foreign" else 1)
                    assert resource.is_deleted == (state == "deleted")
                    assert resource.resource_type == ("skills" if state == "wrong_type" else "knowledge_bases")
                    assert (await s.get(KnowledgeChunk, 30)).content == "ORIGINAL_CHUNK"
                    assert await s.scalar(select(func.count()).select_from(KnowledgeIngestionRun)) == 0
            assert embed.await_count == 0
        finally:
            await engine.dispose()
    asyncio.run(run())
