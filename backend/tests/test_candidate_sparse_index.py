"""隔离 SQLite 候选验证，不代表真实 PGVector 容量验收。"""
import asyncio
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import delete, event, select

from eic.knowledge import indexing, ops, sparse_backfill, sparse_index
from eic.knowledge.retrieval import Bm25Index
from eic.knowledge.sparse_index import search_index, scope_query
from eic.models import KnowledgeBase, KnowledgeChunk, KnowledgeDocument, KnowledgeSparseChunk, Resource
from eic.vector.pgvector_store import PgVectorStore
from eic.vector.qdrant_reader import VectorHit
from test_index_lifecycle import fixture, projection


def test_indexed_bm25_equals_reference_and_no_full_body_scan(tmp_path, monkeypatch):
    async def run():
        async with fixture(tmp_path, monkeypatch) as sessions:
            projection(monkeypatch)
            assert (await indexing.process_knowledge_document({}, 20, 1))["status"] == "parsed_pending_embedding"
            assert (await indexing.process_document_embeddings({}, 20, 1))["status"] == "ready"
            monkeypatch.setattr(ops, "_search_dense", AsyncMock(return_value=([], "unavailable")))
            async with sessions() as session:
                chunks = list(await session.scalars(select(KnowledgeChunk).where(KnowledgeChunk.chunk_index >= 0)))
                reference = Bm25Index([VectorHit(str(c.id), 0., {"content": c.content}) for c in chunks]).search("obsolete obsolete foo", limit=7)
                rows, status = await search_index(session, scope_query(1, 10, [20]), "obsolete obsolete foo", limit=7)
                assert status == "ok" and rows
                assert [h.point_id for h in rows] == [h.point_id for h in reference]
                assert [h.score for h in rows] == pytest.approx([h.score for h in reference])
                base = await session.get(KnowledgeBase, 10)
                assert (await ops.retrieve_knowledge(session, tenant_id=1, base=base, query="obsolete"))["sparseStatus"] == "ok"
                # ORM 不应被 authorize 阶段填入整个知识库正文。
                session.expunge_all()
                base = await session.get(KnowledgeBase, 10)
                state = await ops._authorize_retrieval(session, tenant_id=1, base=base, query="obsolete", extra=None, filters=None, limit=None, before_external=None)
                assert "chunks" not in state and "content" not in state["documents"][20].__dict__
    asyncio.run(run())


def test_backfill_resume_idempotency_and_large_legacy_is_explicit(tmp_path, monkeypatch):
    async def run():
        async with fixture(tmp_path, monkeypatch, embedding=False) as sessions:
            monkeypatch.setattr(sparse_backfill, "SessionLocal", sessions)
            async with sessions() as s:
                for number in range(300):
                    s.add(KnowledgeChunk(id=1000 + number, tenant_id=1, knowledge_base_id=10,
                        document_id=20, index_version=0, chunk_index=number, content="alpha beta", content_hash="synthetic"))
                await s.commit()
            async with sessions() as s:
                assert await search_index(s, scope_query(1, 10, [20]), "alpha", limit=5) == ([], "backfill_required")
            planned = await sparse_backfill.backfill(tenant_id=1, knowledge_base_id=10, max_chunks=100)
            assert planned["wouldIndex"] == 100
            cursor = 0
            for _ in range(3):
                result = await sparse_backfill.backfill(tenant_id=1, knowledge_base_id=10, after_id=cursor, max_chunks=100, apply=True)
                cursor = result["afterId"]
            assert (await sparse_backfill.backfill(tenant_id=1, knowledge_base_id=10, apply=True))["indexed"] == 0
            async with sessions() as s:
                assert (await search_index(s, scope_query(1, 10, [20]), "alpha", limit=5))[1] == "ok"
                await s.execute(delete(KnowledgeSparseChunk).where(KnowledgeSparseChunk.chunk_id == 1000))
                await s.rollback()
    asyncio.run(run())


def test_permissions_version_filters_and_parent_recheck(tmp_path, monkeypatch):
    async def run():
        async with fixture(tmp_path, monkeypatch) as sessions:
            projection(monkeypatch)
            assert (await indexing.process_knowledge_document({}, 20, 1))["status"] == "parsed_pending_embedding"
            assert (await indexing.process_document_embeddings({}, 20, 1))["status"] == "ready"
            monkeypatch.setattr(ops, "_search_dense", AsyncMock(return_value=([], "unavailable")))
            async with sessions() as s:
                base = await s.get(KnowledgeBase, 10)
                assert (await ops.retrieve_knowledge(s, tenant_id=1, base=base, query="foo"))["results"]
                state = await ops._authorize_retrieval(s, tenant_id=1, base=base, query="foo", extra=None, filters=None, limit=None, before_external=None)
                assert (await ops.retrieve_knowledge(s, tenant_id=1, base=base, query="foo", filters={"documentId": 999}))["results"] == []
                document = await s.get(KnowledgeDocument, 20)
                document.source_revision += 1
                await s.commit()
                assert not (await ops._recall_sparse(state, session=s, tenant_id=1, base=base, query="foo", filters=None))["sparse_hits"]
                document.source_revision -= 1
                s.add(Resource(id=77, tenant_id=1, resource_type="files", name="private", data={"accessMode": "private"}))
                document.file_id = 77
                await s.commit()
                assert not (await ops.retrieve_knowledge(s, tenant_id=1, base=base, query="foo"))["results"]
    asyncio.run(run())


def test_pg_document_version_filter_is_bound_and_empty_scope_is_not_global():
    async def run():
        connection = type("Connection", (), {"fetch": AsyncMock(return_value=[])})()
        await PgVectorStore(2).search(connection, tenant_id=1, knowledge_base_id=10, vector=[1., 0.], document_versions={20: 30})
        query, *args = connection.fetch.call_args.args
        assert "$6::jsonb" in query and "indexVersion" in query and args[-1] == '{"20": "30"}'
        assert "30" not in query
        await PgVectorStore(2).search(connection, tenant_id=1, knowledge_base_id=10, vector=[1., 0.], document_versions={})
        assert connection.fetch.call_args.args[-1] == "{}"
    asyncio.run(run())


def test_final_authorization_uses_new_session_after_rerank(tmp_path, monkeypatch):
    async def run():
        async with fixture(tmp_path, monkeypatch) as sessions:
            projection(monkeypatch)
            await indexing.process_knowledge_document({}, 20, 1)
            await indexing.process_document_embeddings({}, 20, 1)
            monkeypatch.setattr(ops, "_search_dense", AsyncMock(return_value=([], "unavailable")))
            async with sessions() as s:
                base = await s.get(KnowledgeBase, 10)
                assert (await ops.retrieve_knowledge(s, tenant_id=1, base=base, query="foo"))["results"]
                async def revoke(state, **kwargs):
                    assert state["fusion_hits"]
                    async with sessions() as other:
                        document = await other.get(KnowledgeDocument, 20)
                        other.add(Resource(id=77, tenant_id=1, resource_type="files", name="private",
                                           data={"accessMode": "private"}))
                        document.file_id = 77
                        await other.commit()
                    return {"reranked_hits": state["fusion_hits"], "rerank_status": "ok"}
                monkeypatch.setattr(ops, "_rerank_retrieval", revoke)
                assert not (await ops.retrieve_knowledge(s, tenant_id=1, base=base, query="foo"))["results"]
    asyncio.run(run())


def test_large_legacy_probe_does_not_select_body_and_query_limit_is_explicit(tmp_path, monkeypatch):
    async def run():
        async with fixture(tmp_path, monkeypatch, embedding=False) as sessions:
            async with sessions() as s:
                s.add_all(KnowledgeChunk(id=1000 + n, tenant_id=1, knowledge_base_id=10,
                    document_id=20, index_version=0, chunk_index=n, content="alpha beta", content_hash="fixture")
                    for n in range(300))
                await s.commit()
                statements = []
                def capture(_, __, statement, ___, ____, _____):
                    statements.append(statement)
                event.listen(s.bind.sync_engine, "before_cursor_execute", capture)
                try:
                    assert await search_index(s, scope_query(1, 10, [20]), "alpha", limit=5) == ([], "backfill_required")
                finally:
                    event.remove(s.bind.sync_engine, "before_cursor_execute", capture)
                assert statements and not any("knowledge_chunks.content," in q.lower() for q in statements)
                monkeypatch.setattr(sparse_index, "tokenize", lambda _: [str(n) for n in range(513)])
                assert await search_index(s, scope_query(1, 10, [20]), "large", limit=5) == ([], "query_too_large")
    asyncio.run(run())


def test_sparse_write_rolls_back_and_backfill_repairs_stale_version(tmp_path, monkeypatch):
    async def run():
        async with fixture(tmp_path, monkeypatch, embedding=False) as sessions:
            monkeypatch.setattr(sparse_backfill, "SessionLocal", sessions)
            async with sessions() as s:
                chunk = KnowledgeChunk(id=1000, tenant_id=1, knowledge_base_id=10, document_id=20,
                    index_version=0, chunk_index=0, content="alpha beta", content_hash="fixture")
                s.add(chunk)
                await s.flush()
                await sparse_index.index_chunk(s, chunk)
                await s.rollback()
                assert await s.get(KnowledgeSparseChunk, 1000) is None
                assert await s.get(KnowledgeChunk, 1000) is None
                chunk = KnowledgeChunk(id=1000, tenant_id=1, knowledge_base_id=10, document_id=20,
                    index_version=0, chunk_index=0, content="alpha beta", content_hash="fixture")
                s.add(chunk)
                await s.flush()
                await sparse_index.index_chunk(s, chunk)
                await s.commit()
                stats = await s.get(KnowledgeSparseChunk, 1000)
                stats.tokenizer_version = "old-tokenizer"
                await s.commit()
            assert (await sparse_backfill.backfill(tenant_id=1, knowledge_base_id=10, apply=True))["indexed"] == 1
            async with sessions() as s:
                assert (await s.get(KnowledgeSparseChunk, 1000)).tokenizer_version == sparse_index.tokenizer_version()
    asyncio.run(run())
