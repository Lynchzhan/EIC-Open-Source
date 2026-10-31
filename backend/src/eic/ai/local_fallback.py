"""降级摘录仍从当前授权数据库读取，不能直接信任过时缓存的正文。"""
from eic.core.file_access import can_bind_shared_file
from eic.core.runtime_refs import require_runtime_resource
from eic.models import KnowledgeBase, KnowledgeChunk, KnowledgeDocument, Resource


async def authorized_excerpt(session, tenant_id, base_ids, rows, *, byte_limit=2048):
    parts, used = [], []
    remaining = byte_limit
    for row in rows:
        try:
            document_id, chunk_id = int(row["documentId"]), int(row["chunkId"])
        except (KeyError, TypeError, ValueError):
            continue
        document = await session.get(KnowledgeDocument, document_id)
        chunk = await session.get(KnowledgeChunk, chunk_id)
        if (document is None or chunk is None or document.tenant_id != tenant_id
                or chunk.tenant_id != tenant_id or document.is_deleted
                or document.knowledge_base_id not in base_ids
                or chunk.document_id != document.id or chunk.knowledge_base_id != document.knowledge_base_id
                or document.active_source_revision != document.source_revision
                or chunk.index_version != document.active_index_version):
            continue
        await require_runtime_resource(session, tenant_id, document.knowledge_base_id,
                                       "knowledge_bases", KnowledgeBase, require_dedicated=True)
        if document.file_id:
            file = await session.get(Resource, document.file_id)
            if not can_bind_shared_file(file, tenant_id):
                continue
        text = chunk.content.encode("utf-8")[:remaining].decode("utf-8", errors="ignore")
        if not text.strip():
            continue
        parts.append(text)
        used.append({**row, "content": text, "childContent": text, "documentName": document.source_name})
        remaining -= len(text.encode("utf-8")) + 2
        if remaining <= 0:
            break
    return "\n\n".join(parts), used
