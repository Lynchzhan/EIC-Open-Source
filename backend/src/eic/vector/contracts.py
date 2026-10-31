"""模型调用前只读检查投影合同，不修改已有向量表。"""
import asyncio

from eic.core.config import get_settings
from eic.core.runtime_io import vector_connection


class ProjectionContractError(ValueError):
    pass


def validate_projection_dimensions(dimensions):
    if dimensions is None:
        return
    if type(dimensions) is not int or not 1 <= dimensions <= 16000:
        raise ProjectionContractError('embedding_dimensions_invalid')
    # 当前投影使用 vector_cosine_ops 的 HNSW；更高维需显式迁移存储合同。
    if dimensions > 2000:
        raise ProjectionContractError('vector_index_dimensions_unsupported')


async def check_projection_dimensions(dimensions):
    validate_projection_dimensions(dimensions)
    if dimensions is None:
        return {'status': 'unknown', 'dimensions': None}
    dsn = get_settings().vector_database_url.replace('postgresql+asyncpg://', 'postgresql://', 1)
    async with asyncio.timeout(5):
        async with vector_connection(dsn) as connection:
            actual = await connection.fetchval(
                "SELECT format_type(a.atttypid, a.atttypmod) FROM pg_attribute a "
                "WHERE a.attrelid = to_regclass('public.rag_vectors') AND a.attname = 'embedding'")
    if actual and actual != f'vector({dimensions})':
        raise ProjectionContractError('vector_dimension_mismatch')
    return {'status': 'compatible' if actual else 'not_initialized', 'dimensions': dimensions}
