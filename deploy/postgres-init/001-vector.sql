CREATE EXTENSION IF NOT EXISTS vector;

-- The selected embedding contract is text-embedding-v4 / 1024 dimensions.
-- Changing dimensions requires a deliberate re-index and a fresh target table.
CREATE TABLE IF NOT EXISTS rag_vectors (
  chunk_id TEXT PRIMARY KEY,
  tenant_id BIGINT NOT NULL,
  knowledge_base_id BIGINT NOT NULL,
  document_id BIGINT NOT NULL,
  content TEXT NOT NULL,
  embedding vector(1024) NOT NULL,
  metadata JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS ix_rag_vectors_tenant_kb
  ON rag_vectors (tenant_id, knowledge_base_id);

CREATE INDEX IF NOT EXISTS ix_rag_vectors_embedding_hnsw
  ON rag_vectors USING hnsw (embedding vector_cosine_ops);
