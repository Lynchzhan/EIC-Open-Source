import asyncio

import httpx

from eic.ai.embeddings import embed_texts
from eic.core.security import encrypt_secret
from eic.knowledge.documents import chunk_text, extract_text
from eic.models import AIModel
from eic.vector.comparison import compare_recall
from eic.vector.contracts import ProjectionContractError
from eic.vector.pgvector_store import ChunkRecord, PgVectorStore
from eic.vector.qdrant_reader import VectorHit


def embedding_model(provider: str = "openai_compatible") -> AIModel:
    return AIModel(
        id=1, tenant_id=1, provider=provider, model_type="embedding", endpoint="http://model.example/v1",
        model_name="embedding-test", secret_encrypted=encrypt_secret("test-key"), enabled=True,
    )


def test_document_text_and_html_extraction_and_overlap(tmp_path) -> None:
    text = tmp_path / "source.txt"
    text.write_text("第一段内容。第二段内容。第三段内容。", encoding="utf-8")
    html = tmp_path / "source.html"
    html.write_text("<h1>企智</h1><p>中心文档</p>", encoding="utf-8")

    assert "第二段" in extract_text(text)
    assert extract_text(html) == "企智 中心文档"
    chunks = chunk_text("abcdefghi", chunk_size=5, overlap=2)
    assert chunks == ["abcde", "defgh", "ghi"]
    from eic.knowledge.documents import page_numbers_in
    from eic.knowledge.ingestion import build_ingestion_chunks

    marked = "[第1页]\nhello\n[图 第2页]\nworld"
    assert page_numbers_in(marked) == [1, 2]
    drafted = build_ingestion_chunks(marked, source_name="demo.pdf", parent_size=80, child_size=80, child_overlap=0)
    assert drafted
    assert drafted[0].metadata["pageNumbers"] == [1, 2]


def test_openai_and_ollama_embedding_protocols_are_normalized() -> None:
    async def run() -> tuple[list[list[float]], list[list[float]]]:
        openai_client = httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"data": [{"index": 0, "embedding": [1, 2]}]})))
        ollama_client = httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"embeddings": [[3, 4]]})))
        try:
            return (
                await embed_texts(embedding_model(), ["hello"], client=openai_client),
                await embed_texts(embedding_model("ollama"), ["hello"], client=ollama_client),
            )
        finally:
            await openai_client.aclose()
            await ollama_client.aclose()

    assert asyncio.run(run()) == ([[1.0, 2.0]], [[3.0, 4.0]])


class FakePgConnection:
    def __init__(self) -> None:
        self.executed: list[tuple] = []
        self.fetchval_result = None

    async def execute(self, query, *args):
        self.executed.append((query, args))

    async def fetchval(self, query, *args):
        self.executed.append((query, args))
        if query.lstrip().startswith("INSERT INTO rag_vectors"):
            return 1
        return self.fetchval_result

    async def fetch(self, query, *args):
        self.executed.append((query, args))
        return [{"chunk_id": "chunk-1", "content": "企智中心", "metadata": {"source": "demo"}, "score": 0.88}]


def test_pgvector_store_parameterizes_tenant_and_vector_queries() -> None:
    async def run() -> tuple[FakePgConnection, list[VectorHit]]:
        connection = FakePgConnection()
        store = PgVectorStore()
        await store.initialize(connection, dimensions=2)
        await store.upsert(connection, ChunkRecord("chunk-1", 1, 2, 3, "企智中心", [0.1, 0.2], {"source": "demo"}))
        return connection, await store.search(connection, tenant_id=1, knowledge_base_id=2, vector=[0.1, 0.2])

    connection, hits = asyncio.run(run())
    assert hits == [VectorHit("chunk-1", 0.88, {"content": "企智中心", "source": "demo"})]
    assert any("$1" in query for query, _ in connection.executed)


def test_pgvector_store_infers_and_enforces_embedding_dimensions() -> None:
    async def run() -> FakePgConnection:
        connection = FakePgConnection()
        store = PgVectorStore()
        await store.initialize(connection, dimensions=2)
        await store.upsert(connection, ChunkRecord("chunk-1", 1, 2, 3, "企智中心", [0.1, 0.2], {}))
        try:
            await store.upsert(connection, ChunkRecord("chunk-2", 1, 2, 3, "维度错误", [0.1, 0.2, 0.3], {}))
        except ValueError as exc:
            assert "dimension mismatch" in str(exc)
        else:
            raise AssertionError("dimension mismatch was accepted")
        return connection

    connection = asyncio.run(run())
    assert "vector(2)" in connection.executed[1][0]


def test_pgvector_store_rejects_an_existing_target_with_wrong_dimensions() -> None:
    async def run() -> None:
        connection = FakePgConnection()
        connection.fetchval_result = "vector(1536)"
        try:
            await PgVectorStore().initialize(connection, dimensions=1024)
        except ProjectionContractError as exc:
            assert str(exc) == "vector_dimension_mismatch"
        else:
            raise AssertionError("existing PGVector dimension mismatch was accepted")

    asyncio.run(run())


def test_recall_comparison_reports_overlap_and_differences() -> None:
    result = compare_recall([VectorHit("a", 1, {}), VectorHit("b", 1, {})], [VectorHit("b", 1, {}), VectorHit("c", 1, {})])
    assert result.overlap_count == 1
    assert result.jaccard == 1 / 3
    assert result.pgvector_only == ["a"]
    assert result.qdrant_only == ["c"]
