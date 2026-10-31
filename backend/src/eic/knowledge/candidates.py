"""检索候选的关系库授权与有限正文回填。"""
from sqlalchemy import select
from sqlalchemy.orm import load_only

from eic.core.file_access import can_bind_shared_file
from eic.models import KnowledgeChunk, KnowledgeDocument, Resource
from eic.vector.qdrant_reader import VectorHit


async def allowed_documents(session, tenant_id, base_id):
    rows = list(await session.scalars(select(KnowledgeDocument).options(load_only(
        KnowledgeDocument.id, KnowledgeDocument.tenant_id, KnowledgeDocument.knowledge_base_id,
        KnowledgeDocument.file_id, KnowledgeDocument.source_name, KnowledgeDocument.active_index_version,
        KnowledgeDocument.source_revision, KnowledgeDocument.active_source_revision)).where(
        KnowledgeDocument.tenant_id == tenant_id, KnowledgeDocument.knowledge_base_id == base_id,
        KnowledgeDocument.is_deleted.is_(False),
        KnowledgeDocument.active_source_revision == KnowledgeDocument.source_revision).execution_options(populate_existing=True)))
    file_ids = sorted({row.file_id for row in rows if row.file_id})
    files = {}
    for offset in range(0, len(file_ids), 400):
        files.update({row.id: row for row in await session.scalars(select(Resource).where(
            Resource.tenant_id == tenant_id, Resource.id.in_(file_ids[offset:offset + 400]))
            .execution_options(populate_existing=True))})
    return {row.id: row for row in rows if not row.file_id or can_bind_shared_file(files.get(row.file_id), tenant_id)}


async def hydrate_hits(session, statement, hits, filters=None):
    identities = [int(hit.point_id) for hit in hits if str(hit.point_id).isdigit()]
    rows = {str(row.id): row for row in await session.scalars(select(KnowledgeChunk).where(
        KnowledgeChunk.id.in_(statement), KnowledgeChunk.id.in_(identities))
        .execution_options(populate_existing=True))}
    from eic.knowledge.retrieval import filter_by_metadata
    return filter_by_metadata([VectorHit(str(hit.point_id), hit.score,
        {**(rows[str(hit.point_id)].attributes or {}), "content": rows[str(hit.point_id)].content,
         "documentId": rows[str(hit.point_id)].document_id,
         "parentChunkId": rows[str(hit.point_id)].parent_chunk_id})
        for hit in hits if str(hit.point_id) in rows], filters)


async def parent_contents(session, statement, hits):
    identities = {hit.payload.get("parentChunkId") for hit in hits} - {None}
    # parent 必须同文档、同活动版本且确为父块；不消费向量投影中的 parent_content。
    children = list(await session.scalars(select(KnowledgeChunk).where(
        KnowledgeChunk.id.in_(statement), KnowledgeChunk.id.in_([int(hit.point_id) for hit in hits]))))
    bindings = {(c.document_id, c.index_version, c.parent_chunk_id, c.tenant_id, c.knowledge_base_id) for c in children}
    parents = await session.scalars(select(KnowledgeChunk).where(KnowledgeChunk.id.in_(identities)))
    return {p.id: p.content for p in parents if
        (p.document_id, p.index_version, p.id, p.tenant_id, p.knowledge_base_id) in bindings
        and (p.attributes or {}).get("chunk_kind") == "parent"}
