"""只返回记忆候选；单调投影栅栏防止旧写入复活已清理向量。"""
import asyncio
import json

from eic.core.config import get_settings
from eic.core.runtime_io import vector_connection
from eic.vector.contracts import validate_projection_dimensions


def dsn():
    return get_settings().vector_database_url.replace("postgresql+asyncpg://", "postgresql://", 1)


async def prepare(sql, dimensions):
    validate_projection_dimensions(dimensions)
    if dimensions is None:
        raise ValueError("memory_projection_dimensions_required")
    async with vector_connection(dsn()) as connection:
        await connection.execute(sql)
        # dimensions 经整数范围校验；标识符和类型不能使用绑定参数。
        await connection.execute(f"CREATE INDEX IF NOT EXISTS ix_memory_hnsw_{dimensions} "
            f"ON public.eic_memory_vectors USING hnsw ((embedding::vector({dimensions})) vector_cosine_ops) "
            f"WHERE dimensions = {dimensions}")


async def replace(memory_id, generation, row):
    if row is not None:
        validate_projection_dimensions(row["dimensions"])
    async with asyncio.timeout(8):
        async with vector_connection(dsn()) as connection:
            async with connection.transaction():
                accepted = await connection.fetchval(
                    "INSERT INTO public.eic_memory_projection_heads(memory_id,generation) VALUES($1,$2) "
                    "ON CONFLICT(memory_id) DO UPDATE SET generation=EXCLUDED.generation "
                    "WHERE eic_memory_projection_heads.generation <= EXCLUDED.generation RETURNING generation",
                    memory_id, generation)
                if accepted is None:
                    return False
                await connection.execute("DELETE FROM public.eic_memory_vectors WHERE memory_id=$1", memory_id)
                if row is not None:
                    await connection.execute("INSERT INTO public.eic_memory_vectors "
                        "(vector_id,memory_id,tenant_id,owner_id,agent_id,session_id,session_scoped,"
                        "memory_version,content_hash,model_fingerprint,dimensions,embedding) "
                        "VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12::vector)",
                        row["vector_id"], memory_id, row["tenant_id"], row["owner_id"], row["agent_id"],
                        row["session_id"], row["session_scoped"], row["memory_version"], row["content_hash"],
                        row["model_fingerprint"], row["dimensions"], json.dumps(row["vector"], allow_nan=False))
                return True


async def candidates(vector, *, tenant_id, owner_id, agent_id, session_id, fingerprint, limit=240):
    dimensions = len(vector)
    validate_projection_dimensions(dimensions)
    async with asyncio.timeout(1):
        async with vector_connection(dsn()) as connection:
            async with connection.transaction():
                await connection.execute("SET LOCAL statement_timeout = '900ms'")
                await connection.execute("SET LOCAL hnsw.ef_search = 240")
                rows = await connection.fetch(f"SELECT vector_id,memory_id,memory_version,content_hash,"
                    f"1-(embedding::vector({dimensions}) <=> $1::vector({dimensions})) AS score "
                    f"FROM public.eic_memory_vectors WHERE dimensions={dimensions} "
                    "AND tenant_id=$2 AND owner_id=$3 AND agent_id IS NOT DISTINCT FROM $4::bigint "
                    "AND model_fingerprint=$5 AND (NOT session_scoped OR session_id=$6) "
                    f"ORDER BY embedding::vector({dimensions}) <=> $1::vector({dimensions}) LIMIT $7",
                    json.dumps(vector, allow_nan=False), tenant_id, owner_id, agent_id,
                    fingerprint, session_id, max(1, min(limit, 240)))
    return [dict(row) for row in rows]
