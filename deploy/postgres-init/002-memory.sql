-- 独立记忆候选投影；权威授权、正文、遗忘状态仍在关系库。
CREATE EXTENSION IF NOT EXISTS vector;
CREATE TABLE IF NOT EXISTS public.eic_memory_projection_heads (
    memory_id bigint PRIMARY KEY,
    generation bigint NOT NULL
);
CREATE TABLE IF NOT EXISTS public.eic_memory_vectors (
    vector_id bigint PRIMARY KEY,
    memory_id bigint NOT NULL,
    tenant_id bigint NOT NULL,
    owner_id bigint NOT NULL,
    agent_id bigint,
    session_id bigint,
    session_scoped boolean NOT NULL,
    memory_version integer NOT NULL,
    content_hash varchar(64) NOT NULL,
    model_fingerprint varchar(64) NOT NULL,
    dimensions integer NOT NULL,
    embedding vector NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_memory_projection_scope
    ON public.eic_memory_vectors (tenant_id, owner_id, agent_id, model_fingerprint);
CREATE INDEX IF NOT EXISTS ix_memory_projection_memory
    ON public.eic_memory_vectors (memory_id);
-- 各维度的 HNSW 表达式索引由 prepare-memory-projection.py 显式创建。
