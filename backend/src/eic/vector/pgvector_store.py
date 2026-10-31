from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Protocol

from eic.vector.qdrant_reader import VectorHit


class PgVectorConnection(Protocol):
    async def execute(self, query: str, *args: Any) -> Any:
        ...

    async def fetchval(self, query: str, *args: Any) -> Any:
        ...

    async def fetch(self, query: str, *args: Any) -> list[Any]:
        ...


@dataclass(frozen=True)
class ChunkRecord:
    chunk_id: str
    tenant_id: int
    knowledge_base_id: int
    document_id: int
    content: str
    vector: list[float]
    metadata: dict[str, Any]


class PgVectorScopeConflict(ValueError):
    """A chunk ID already belongs to a different tenant, KB or document."""


class PgVectorStore:
    """PostgreSQL/PGVector persistence boundary; Qdrant is never written here.

    Parsed chunks and vector rows intentionally use different tables.  The ORM
    table ``knowledge_chunks`` stores the parent/child document graph, while
    ``rag_vectors`` is the PGVector projection.  This prevents a raw vector
    bootstrap from silently colliding with the relational ingestion schema.
    """

    def __init__(self, dimensions: int | None = None) -> None:
        if dimensions is not None and not 1 <= dimensions <= 16000:
            raise ValueError("embedding dimensions must be between 1 and 16000")
        self.dimensions = dimensions

    async def initialize(self, connection: PgVectorConnection, dimensions: int | None = None) -> None:
        if dimensions is not None:
            if not 1 <= dimensions <= 16000:
                raise ValueError("embedding dimensions must be between 1 and 16000")
            self.dimensions = dimensions
        from eic.vector.contracts import validate_projection_dimensions
        validate_projection_dimensions(self.dimensions)
        await connection.execute("CREATE EXTENSION IF NOT EXISTS vector")
        vector_type = f"vector({self.dimensions})" if self.dimensions else "vector"
        await connection.execute(
            f"""CREATE TABLE IF NOT EXISTS rag_vectors (
                chunk_id TEXT PRIMARY KEY, tenant_id BIGINT NOT NULL, knowledge_base_id BIGINT NOT NULL,
                document_id BIGINT NOT NULL, content TEXT NOT NULL, embedding {vector_type} NOT NULL,
                metadata JSONB NOT NULL DEFAULT '{{}}'::jsonb
            )"""
        )
        await connection.execute(
            "CREATE INDEX IF NOT EXISTS ix_rag_vectors_tenant_kb ON rag_vectors (tenant_id, knowledge_base_id)"
        )
        if self.dimensions:
            actual_type = await connection.fetchval(
                "SELECT format_type(a.atttypid, a.atttypmod) "
                "FROM pg_attribute a "
                "WHERE a.attrelid = 'public.rag_vectors'::regclass AND a.attname = 'embedding'"
            )
            expected_type = f"vector({self.dimensions})"
            if actual_type and actual_type != expected_type:
                from eic.vector.contracts import ProjectionContractError
                raise ProjectionContractError("vector_dimension_mismatch")
            await connection.execute(
                "CREATE INDEX IF NOT EXISTS ix_rag_vectors_embedding_hnsw "
                "ON rag_vectors USING hnsw (embedding vector_cosine_ops)"
            )

    async def upsert(self, connection: PgVectorConnection, chunk: ChunkRecord) -> None:
        if self.dimensions is None:
            self.dimensions = len(chunk.vector)
        if len(chunk.vector) != self.dimensions:
            raise ValueError(f"embedding dimension mismatch: expected {self.dimensions}, got {len(chunk.vector)}")
        accepted = await connection.fetchval(
            """INSERT INTO rag_vectors (chunk_id, tenant_id, knowledge_base_id, document_id, content, embedding, metadata)
            VALUES ($1, $2, $3, $4, $5, $6::vector, $7::jsonb)
            ON CONFLICT (chunk_id) DO UPDATE SET content = EXCLUDED.content, embedding = EXCLUDED.embedding, metadata = EXCLUDED.metadata
            WHERE rag_vectors.tenant_id = EXCLUDED.tenant_id
              AND rag_vectors.knowledge_base_id = EXCLUDED.knowledge_base_id
              AND rag_vectors.document_id = EXCLUDED.document_id
            RETURNING 1""",
            chunk.chunk_id, chunk.tenant_id, chunk.knowledge_base_id, chunk.document_id,
            chunk.content, str(chunk.vector), json.dumps(chunk.metadata, ensure_ascii=False),
        )
        if accepted is None:
            raise PgVectorScopeConflict("pgvector_chunk_scope_conflict")

    async def upsert_many(self, connection: PgVectorConnection, chunks: list[ChunkRecord]) -> None:
        """在调用方事务中写入一个有界批次；作用域冲突必须回滚整个批次。"""
        if not chunks:
            return
        if len(chunks) > 128 or len({chunk.chunk_id for chunk in chunks}) != len(chunks):
            raise ValueError("invalid_vector_write_batch")
        if self.dimensions is None:
            self.dimensions = len(chunks[0].vector)
        rows = []
        for chunk in chunks:
            if len(chunk.vector) != self.dimensions:
                raise ValueError("embedding_dimension_mismatch")
            rows.append({
                "chunk_id": chunk.chunk_id, "tenant_id": chunk.tenant_id,
                "knowledge_base_id": chunk.knowledge_base_id, "document_id": chunk.document_id,
                "content": chunk.content, "embedding": str(chunk.vector), "metadata": chunk.metadata,
            })
        accepted = await connection.fetchval(
            """WITH accepted AS (
                INSERT INTO rag_vectors (chunk_id, tenant_id, knowledge_base_id, document_id, content, embedding, metadata)
                SELECT chunk_id, tenant_id, knowledge_base_id, document_id, content, embedding::vector, metadata
                FROM jsonb_to_recordset($1::jsonb) AS incoming(
                    chunk_id text, tenant_id bigint, knowledge_base_id bigint,
                    document_id bigint, content text, embedding text, metadata jsonb)
                WHERE TRUE
                ON CONFLICT (chunk_id) DO UPDATE SET
                    content = EXCLUDED.content, embedding = EXCLUDED.embedding, metadata = EXCLUDED.metadata
                WHERE rag_vectors.tenant_id = EXCLUDED.tenant_id
                  AND rag_vectors.knowledge_base_id = EXCLUDED.knowledge_base_id
                  AND rag_vectors.document_id = EXCLUDED.document_id
                RETURNING 1
            ) SELECT count(*) FROM accepted""",
            json.dumps(rows, ensure_ascii=False, allow_nan=False),
        )
        if accepted != len(chunks):
            raise PgVectorScopeConflict("pgvector_chunk_scope_conflict")

    async def search(self, connection: PgVectorConnection, *, tenant_id: int, knowledge_base_id: int, vector: list[float], limit: int = 5, chunk_ids: list[str] | None = None, document_versions: dict[int, int] | None = None) -> list[VectorHit]:
        if not vector:
            return []
        scope = ""
        arguments = [tenant_id, knowledge_base_id, str(vector), limit, chunk_ids]
        if document_versions is not None:
            scope = " AND metadata->>'indexVersion' = ($6::jsonb ->> document_id::text)"
            arguments.append(json.dumps({str(key): str(value) for key, value in document_versions.items()}))
        rows = await connection.fetch(
            """SELECT chunk_id, content, metadata, 1 - (embedding <=> $3::vector) AS score
            FROM rag_vectors WHERE tenant_id = $1 AND knowledge_base_id = $2
            AND ($5::text[] IS NULL OR chunk_id=ANY($5::text[]))""" + scope +
            " ORDER BY embedding <=> $3::vector LIMIT $4",
            *arguments,
        )
        hits = []
        for row in rows:
            metadata = row["metadata"]
            if isinstance(metadata, str):
                metadata = json.loads(metadata)
            hits.append(VectorHit(str(row["chunk_id"]), float(row["score"]), {"content": row["content"], **dict(metadata or {})}))
        return hits

    async def delete_chunks(self, connection: PgVectorConnection, *, tenant_id: int, chunk_ids: list[str]) -> None:
        if chunk_ids:
            await connection.execute(
                "DELETE FROM rag_vectors WHERE tenant_id=$1 AND chunk_id=ANY($2::text[])",
                tenant_id, chunk_ids,
            )
