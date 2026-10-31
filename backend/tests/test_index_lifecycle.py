"""持久关系库与受控故障注入，不把合成 provider 当外部服务验收。"""
import asyncio
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import event, func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from eic.core import outbox
from eic.core.ids import initialize_allocator
from eic.knowledge import indexing, ops
from eic.models import AIModel, Base, KnowledgeBase, KnowledgeChunk, KnowledgeDocument, KnowledgeIngestionRun, Resource, Tenant


@asynccontextmanager
async def fixture(tmp_path, monkeypatch, *, embedding=True):
    # 原子切换场景固定每文档一批、逐文档执行；批量上限另有专门回归。
    monkeypatch.setattr(indexing.get_settings(), "embedding_provider_max_batch_size", 128)
    engine = create_async_engine("sqlite+aiosqlite:///" + str(tmp_path / "index.db"))
    @event.listens_for(engine.sync_engine, "connect")
    def fk(connection, _):
        connection.execute("PRAGMA foreign_keys=ON")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(indexing, "SessionLocal", sessions)
    monkeypatch.setattr(ops, "SessionLocal", sessions)
    monkeypatch.setattr(outbox, "dispatch_one", AsyncMock(return_value=None))
    async with engine.begin() as c:
        await c.run_sync(Base.metadata.create_all)
    async with sessions() as s:
        s.add(Tenant(id=1, code=1, name="fixture"))
        await s.flush()
        if embedding:
            s.add(AIModel(id=9, tenant_id=1, provider="openai", model_type="embedding",
                          model_name="fixture", endpoint="http://fixture.invalid", enabled=True, embedding_dimensions=2))
            await s.flush()
        s.add(KnowledgeBase(id=10, tenant_id=1, name="kb", embedding_model_id=9 if embedding else None,
                            retrieval_strategy="advanced", retrieval_config={"childSize": 30, "parentSize": 60,
                            "childOverlap": 0, "embeddingBatchSize": 128, "indexConcurrency": 1}))
        s.add(Resource(id=10, tenant_id=1, resource_type="knowledge_bases", name="kb", data={"fileIds": []}))
        await s.flush()
        s.add(KnowledgeDocument(id=20, tenant_id=1, knowledge_base_id=10, source_name="text",
                               content="obsolete foo " * 15, status="queued_text"))
        await s.flush()
        await initialize_allocator(s)
        await s.commit()
    try:
        yield sessions
    finally:
        await engine.dispose()


class Connection:
    def __init__(self):
        self.rows = {}
        self.before = {}
        self.closed = False
    def transaction(self):
        return self
    async def __aenter__(self):
        self.before = dict(self.rows)
    async def __aexit__(self, kind, exc, tb):
        if kind:
            self.rows = self.before
    async def close(self):
        self.closed = True
    async def fetchval(self, query, *args):
        assert 'format_type' in query
        return 'vector(2)'


def projection(monkeypatch, *, fail_after=None):
    from eic.vector import contracts
    connection = Connection()
    @asynccontextmanager
    async def vector_connection(_):
        try:
            yield connection
        finally:
            await connection.close()
    monkeypatch.setattr(indexing, "vector_connection", vector_connection)
    monkeypatch.setattr(contracts, "vector_connection", vector_connection)
    monkeypatch.setattr(indexing.PgVectorStore, "initialize", AsyncMock())
    async def upsert_many(_, c, chunks):
        for chunk in chunks:
            if fail_after is not None and len(c.rows) >= fail_after:
                raise RuntimeError("injected_partial_write")
            c.rows[chunk.chunk_id] = chunk
    monkeypatch.setattr(indexing.PgVectorStore, "upsert_many", upsert_many)
    monkeypatch.setattr(indexing, "embed_texts", AsyncMock(side_effect=lambda model, texts, **kw: [[1., 0.] for _ in texts]))
    return connection


def test_version_switch_idempotency_and_old_body_filter(tmp_path, monkeypatch):
    async def run():
        async with fixture(tmp_path, monkeypatch) as sessions:
            c = projection(monkeypatch)
            parsed = await indexing.process_knowledge_document({}, 20, 1)
            assert parsed["status"] == "parsed_pending_embedding"
            async with sessions() as s:
                d = await s.get(KnowledgeDocument, 20)
                assert d.active_index_version == 0 and d.parse_status == "ready"
                assert ops.display_document_status(d, 20) != "ready"
            assert (await indexing.process_document_embeddings({}, 20, 1))["status"] == "ready"
            first = parsed["index_version"]
            assert (await indexing.process_knowledge_document({}, 20, 1))["unchanged"]
            async with sessions() as s:
                d = await s.get(KnowledgeDocument, 20)
                d.source_revision += 1
                d.content = "replacement bar " * 15
                d.status = "queued_text"
                await s.commit()
            async with sessions() as s:
                kb = await s.get(KnowledgeBase, 10)
                monkeypatch.setattr(ops, "_search_dense", AsyncMock(return_value=([], "unavailable")))
                assert not (await ops.retrieve_knowledge(s, tenant_id=1, base=kb, query="obsolete"))["results"]
                assert await s.scalar(select(func.count()).select_from(KnowledgeChunk).where(KnowledgeChunk.index_version == first)) > 0
            next_version = (await indexing.process_knowledge_document({}, 20, 1))["index_version"]
            assert next_version != first
            assert (await indexing.process_document_embeddings({}, 20, 1))["status"] == "ready"
            async with sessions() as s:
                assert (await s.get(KnowledgeDocument, 20)).active_index_version == next_version
                assert await s.scalar(select(func.count()).select_from(KnowledgeIngestionRun)) == 2
            assert c.closed
    asyncio.run(run())


@pytest.mark.parametrize("failure", ["provider", "dimensions", "nonfinite", "connection", "partial", "disabled"])
def test_embedding_failures_do_not_switch_or_report_ready(tmp_path, monkeypatch, failure):
    async def run():
        async with fixture(tmp_path, monkeypatch) as sessions:
            c = projection(monkeypatch)
            await indexing.process_knowledge_document({}, 20, 1)
            await indexing.process_document_embeddings({}, 20, 1)
            async with sessions() as s:
                d = await s.get(KnowledgeDocument, 20)
                old = d.active_index_version
                kb = await s.get(KnowledgeBase, 10)
                kb.retrieval_config = {**kb.retrieval_config, "childSize": 20}
                await s.commit()
            parsed = await indexing.process_knowledge_document({}, 20, 1)
            version = parsed["index_version"]
            if failure == "provider":
                monkeypatch.setattr(indexing, "embed_texts", AsyncMock(side_effect=RuntimeError("TOKEN_MUST_NOT_LEAK")))
            elif failure in {"dimensions", "nonfinite"}:
                monkeypatch.setattr(indexing, "embed_texts", AsyncMock(
                    side_effect=lambda m, texts, **kw: [[1.] if failure == "dimensions" else [float("nan"), 0.] for _ in texts]))
            elif failure == "connection":
                @asynccontextmanager
                async def unavailable(_):
                    raise OSError("injected")
                    yield
                monkeypatch.setattr(indexing, "vector_connection", unavailable)
            elif failure == "partial":
                c = projection(monkeypatch, fail_after=1)
            else:
                async with sessions() as s:
                    (await s.get(AIModel, 9)).enabled = False
                    await s.commit()
            assert (await indexing.process_document_embeddings({}, 20, 1))["status"] == "embedding_failed"
            async with sessions() as s:
                d = await s.get(KnowledgeDocument, 20)
                r = await s.get(KnowledgeIngestionRun, version)
                assert d.active_index_version == old and d.status != "ready" and d.index_error
                assert "TOKEN_MUST_NOT_LEAK" not in str(d.index_error)
                assert r.status == "failed"
            if failure == "partial":
                assert not c.rows and c.closed
            # 同一失败版本不会自动重发收费模型调用。
            provider = indexing.embed_texts
            before = provider.await_count
            assert (await indexing.process_knowledge_document({}, 20, 1))["status"] == "failed"
            assert provider.await_count == before
    asyncio.run(run())


@pytest.mark.parametrize("mode", ["success", "failure", "cancel", "parameter_change", "inventory_change"])
def test_full_rebuild_atomic_inventory_and_cancellation(tmp_path, monkeypatch, mode):
    async def run():
        async with fixture(tmp_path, monkeypatch) as sessions:
            projection(monkeypatch)
            async with sessions() as s:
                s.add(KnowledgeDocument(id=21, tenant_id=1, knowledge_base_id=10, source_name="second",
                                       content="replacement bar " * 10, status="queued_text"))
                await s.commit()
            assert (await indexing.process_knowledge_base({}, 10, 1))["status"] == "ready"
            async with sessions() as s:
                before = {d.id: d.active_index_version for d in await s.scalars(select(KnowledgeDocument))}
                kb = await s.get(KnowledgeBase, 10)
                kb.retrieval_config = {**kb.retrieval_config, "childSize": 20}
                await s.commit()
            calls = []
            async def embedding(model, texts, **kw):
                calls.append(len(texts))
                if len(calls) == 2:
                    if mode == "failure":
                        raise RuntimeError("injected")
                    async with sessions() as s:
                        if mode == "cancel":
                            r = await s.get(Resource, 10)
                            r.data = {**r.data, "indexStatus": "cancelled"}
                        elif mode == "parameter_change":
                            kb = await s.get(KnowledgeBase, 10)
                            kb.chunk_size += 1
                            kb.retrieval_config = {**kb.retrieval_config, "childSize": 19}
                        elif mode == "inventory_change":
                            (await s.get(KnowledgeDocument, 20)).source_revision += 1
                        await s.commit()
                return [[1., 0.] for _ in texts]
            monkeypatch.setattr(indexing, "embed_texts", embedding)
            result = await indexing.process_knowledge_base({}, 10, 1)
            async with sessions() as s:
                after = {d.id: d.active_index_version for d in await s.scalars(select(KnowledgeDocument))}
                if mode == "success":
                    assert result["status"] == "ready" and all(before[k] != after[k] for k in before)
                    again = await indexing.process_knowledge_base({}, 10, 1)
                    assert again["status"] == "ready" and again["versions"] == result["versions"]
                else:
                    assert result["status"] == "failed" and before == after
                    if mode == "cancel":
                        assert (await s.get(Resource, 10)).data["indexStatus"] == "cancelled"
            assert len(calls) == 2
    asyncio.run(run())


def test_missing_required_embedding_model_never_reports_ready(tmp_path, monkeypatch):
    async def run():
        async with fixture(tmp_path, monkeypatch, embedding=False) as sessions:
            result = await indexing.process_knowledge_base({}, 10, 1)
            assert result["status"] == "failed"
            async with sessions() as s:
                d = await s.get(KnowledgeDocument, 20)
                payload = ops.serialize_knowledge_document(d, chunk_count=10)
                assert payload["statusKey"] != "ready" and payload["status"] != 2
                assert d.active_index_version == 0
                assert await s.scalar(select(func.count()).select_from(KnowledgeChunk)) == 0
    asyncio.run(run())
