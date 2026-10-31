"""离线业务 seam、SQLite 范围与标准接口契约。"""
import asyncio
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace

import httpx
import pytest
from fastapi import HTTPException
from langchain_core.documents import Document
from langchain_core.documents.compressor import BaseDocumentCompressor
from langchain_core.embeddings import Embeddings
from langchain_core.retrievers import BaseRetriever
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from eic.ai import embeddings, rerank
from eic.ai.providers import ModelProviderError
from eic.knowledge import langchain_entrypoint, ops
from eic.knowledge.retrieval import EICRetriever
from eic.models import AIModel, Base, KnowledgeBase, KnowledgeChunk, KnowledgeDocument, Resource
from eic.vector.qdrant_reader import VectorHit


def model(kind="embedding", **extra):
    return AIModel(id=1, tenant_id=7, provider="openai_compatible", model_type=kind,
                   endpoint="https://fixture.invalid/v1", model_name=kind,
                   enabled=True, embedding_dimensions=2 if kind == "embedding" else None,
                   **extra)


def test_public_embedding_uses_standard_interface_and_one_guard(monkeypatch):
    calls = []
    method = embeddings.EICEmbeddings.aembed_documents

    async def standard(self, texts):
        assert isinstance(self, Embeddings)
        calls.append(("standard", texts))
        return await method(self, texts)

    @asynccontextmanager
    async def guard(*args, **kwargs):
        calls.append(("guard", kwargs["model_id"]))
        yield

    monkeypatch.setattr(embeddings.EICEmbeddings, "aembed_documents", standard)
    monkeypatch.setattr("eic.core.dependency_controls.guard", guard)

    def provider(request):
        calls.append(("provider", json.loads(request.content)["input"]))
        return httpx.Response(200, json={"usage": {"prompt_tokens": 3, "total_tokens": 3}, "data": [
            {"index": 1, "embedding": [0, 1]}, {"index": 0, "embedding": [1, 0]}]})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as client:
            sink = {}
            result = await embeddings.embed_texts(model(), ["a", "b"], tenant_id=7,
                                                   client=client, usage_sink=sink)
            assert result == [[1, 0], [0, 1]]
            assert sink["input_tokens"] == 3
            assert "_invoked" not in sink
            with pytest.raises(ModelProviderError):
                await embeddings.embed_texts(model(), ["a"], tenant_id=8, client=client)
    asyncio.run(run())
    assert calls == [("standard", ["a", "b"]), ("guard", 1), ("provider", ["a", "b"])]


def test_indexing_imported_embedding_function_uses_adapter(monkeypatch):
    from eic.knowledge import indexing
    called = []

    async def standard(self, texts):
        called.append(texts)
        return [[1., 0.] for _ in texts]

    monkeypatch.setattr(embeddings.EICEmbeddings, "aembed_documents", standard)
    assert asyncio.run(indexing.embed_texts(model(), ["child"], tenant_id=7)) == [[1., 0.]]
    assert called == [["child"]]


def test_public_rerank_uses_compressor_preserves_identity_and_score_space(monkeypatch):
    calls = []
    method = rerank.EICDocumentReranker.acompress_documents

    async def standard(self, docs, query, callbacks=None):
        assert isinstance(self, BaseDocumentCompressor)
        assert all(isinstance(doc, Document) for doc in docs)
        calls.append("compressor")
        return await method(self, docs, query, callbacks)

    monkeypatch.setattr(rerank.EICDocumentReranker, "acompress_documents", standard)
    original = [VectorHit("a", .01, {"content": "a", "fusionScore": .01, "parentChunkId": 5}),
                VectorHit("b", .02, {"content": "b", "pageNumbers": [2], "scoreSpace": "fusion"})]

    def provider(request):
        calls.append(json.loads(request.content)["documents"])
        return httpx.Response(200, json={"usage": {"prompt_tokens": 4, "total_tokens": 4}, "results": [
            {"index": 0, "relevance_score": .2}, {"index": 1, "relevance_score": .9}]})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as client:
            sink = {}
            result = await ops.rerank_hits(model("rerank"), "q", original, limit=2,
                                            client=client, usage_sink=sink)
            assert [hit.point_id for hit in result] == ["b", "a"]
            assert [hit.score for hit in result] == [.9, .2]
            assert result[1].payload["parentChunkId"] == 5
            assert result[1].payload["fusionScore"] == .01
            assert result[0].payload["pageNumbers"] == [2]
            assert result[0].payload["scoreSpace"] == "rerank"
            assert sink["input_tokens"] == 4
            assert "_invoked" not in sink
    asyncio.run(run())
    assert calls == ["compressor", ["a", "b"]]
    assert original[0].payload == {"content": "a", "fusionScore": .01, "parentChunkId": 5}


def test_sqlite_retrieval_entrypoint_preserves_domain_contract_and_scope(tmp_path, monkeypatch):
    calls = []
    original_method = EICRetriever.ainvoke

    async def standard(self, query, config=None, **kwargs):
        assert isinstance(self, BaseRetriever)
        calls.append((self.tenant_id, self.knowledge_base_id))
        return await original_method(self, query, config, **kwargs)

    monkeypatch.setattr(EICRetriever, "ainvoke", standard)

    async def forbidden_embedding(*args, **kwargs):
        raise AssertionError("legacy lexical records must not be re-embedded")

    monkeypatch.setattr(ops, "embed_texts", forbidden_embedding)

    async def run():
        engine = create_async_engine("sqlite+aiosqlite:///" + (tmp_path / "retrieval.db").as_posix())
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        monkeypatch.setattr(ops, "SessionLocal", sessions)
        async with sessions() as session:
            for kb, tenant in [(17, 7), (18, 7), (19, 8)]:
                session.add(KnowledgeBase(id=kb, tenant_id=tenant, name="kb",
                                          retrieval_strategy="full", retrieval_config={"thresholdVersion": 2}))
                session.add(Resource(id=kb, tenant_id=tenant, resource_type="knowledge_bases", name="kb"))
                doc = kb * 10
                session.add(KnowledgeDocument(id=doc, tenant_id=tenant, knowledge_base_id=kb,
                    source_name="policy.pdf", active_index_version=0, source_revision=0,
                    active_source_revision=0))
                session.add(KnowledgeChunk(id=doc+1, tenant_id=tenant, knowledge_base_id=kb,
                    document_id=doc, chunk_index=0, content="parent policy", content_hash="parent",
                    attributes={"chunk_kind": "parent"}))
                session.add(KnowledgeChunk(id=doc+2, tenant_id=tenant, knowledge_base_id=kb,
                    document_id=doc, parent_chunk_id=doc+1, chunk_index=1,
                    content="policy refund", content_hash="child", attributes={"pageNumbers": [2]}))
            await session.commit()
            base = await session.get(KnowledgeBase, 17)
            before = await ops.retrieve_knowledge(session, tenant_id=7, base=base, query="policy", limit=6)
            after = await langchain_entrypoint.retrieve_knowledge(session, tenant_id=7, base=base,
                                                                  query="policy", limit=6)
            assert after == before
            assert [row["documentId"] for row in after["results"]] == [170]
            row = after["results"][0]
            assert row["parentContent"] == "parent policy"
            assert row["content"] == "parent policy"
            assert row["childContent"] == "policy refund"
            assert row["pageNumbers"] == [2]
            assert row["scoreSpace"] == "fusion"
            from eic import main
            permissions = []

            async def permit(name, principal, session):
                permissions.append(name)

            monkeypatch.setattr(main, "ensure_permission", permit)
            monkeypatch.setattr(main, "retrieve_knowledge", langchain_entrypoint.retrieve_knowledge)
            actual_entry = await main.knowledge_retrieval_trace(
                17, "policy", limit=6, principal=SimpleNamespace(tenant_id=7), session=session)
            assert actual_entry == before
            assert permissions == ["knowledge.read"]
            with pytest.raises(HTTPException) as error:
                await langchain_entrypoint.retrieve_knowledge(session, tenant_id=8, base=base, query="policy")
            assert error.value.status_code == 404
            resource = await session.get(Resource, 17)
            resource.data = {"enabled": False}
            await session.commit()
            with pytest.raises(HTTPException):
                await langchain_entrypoint.retrieve_knowledge(session, tenant_id=7, base=base, query="policy")
        await engine.dispose()
    asyncio.run(run())
    # 三个成功入口各经过 dense/sparse 两个标准检索器；拒绝入口不应进入召回。
    assert calls == [(7, 17)] * 6


def test_adapter_rejects_empty_wrong_tenant_and_cross_batch_dimensions():
    async def run():
        with pytest.raises(ModelProviderError):
            await embeddings.EICEmbeddings(model(), tenant_id=8).aembed_documents([])
        item = model()
        item.embedding_dimensions = None
        count = 0

        def provider(request):
            nonlocal count
            count += 1
            return httpx.Response(200, json={"data": [{"index": 0, "embedding": [1.] * count}]})

        async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as client:
            with pytest.raises(ModelProviderError, match="batch dimensions"):
                await embeddings.EICEmbeddings(item, tenant_id=7, batch_size=1,
                                                client=client).aembed_documents(["a", "b"])
        assert count == 2
    asyncio.run(run())


def test_main_entrypoint_proposed_import_seam_only(monkeypatch):
    from eic import main
    calls = []

    async def permissions(name, principal, session):
        calls.append(name)

    async def replacement(session, **kwargs):
        calls.append((kwargs["tenant_id"], kwargs["base"].id))
        return {"results": []}

    class Session:
        async def get(self, cls, identifier):
            return SimpleNamespace(id=identifier, tenant_id=7)

    monkeypatch.setattr(main, "ensure_permission", permissions)
    # 仅在测试中模拟协调者的导入替换，不改 main 源码。
    monkeypatch.setattr(main, "retrieve_knowledge", replacement)
    result = asyncio.run(main.knowledge_retrieval_trace(
        17, "policy", principal=SimpleNamespace(tenant_id=7), session=Session()))
    assert result == {"results": []}
    assert calls == ["knowledge.read", (7, 17)]
