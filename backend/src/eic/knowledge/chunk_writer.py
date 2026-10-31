"""分批写入未激活版本；完整提交后才由调用者切换活动版本。"""
from datetime import UTC, datetime, timedelta
import hashlib

from sqlalchemy import insert

from eic.core.ids import new_ids
from eic.knowledge.sparse_index import insert_new_chunks
from eic.models import KnowledgeChunk


def persistent_metadata(metadata):
    # 父正文只存在 parent 行；保留页码、来源及用户定义的其它元数据。
    return {key: value for key, value in metadata.items() if key != 'parent_content'}


async def write_chunk_batches(*, sessions, validate, drafts, source_metadata, source_name,
                              document_id, tenant_id, base_id, version, batch_size=64):
    parent_drafts = {}
    for draft in drafts:
        parent_drafts.setdefault(draft.parent_index, draft)
    parents = {}
    values = list(parent_drafts.values())
    for offset in range(0, len(values), batch_size):
        batch = values[offset:offset + batch_size]
        async with sessions() as session:
            ids = await new_ids(session, len(batch))
            *_, run = await validate(session, document_id, tenant_id, version, include_content=False)
            rows = []
            for draft, identity in zip(batch, ids, strict=True):
                parents[draft.parent_index] = identity
                content = str(draft.metadata['parent_content'])
                rows.append(dict(id=identity, tenant_id=tenant_id, knowledge_base_id=base_id,
                    document_id=document_id, index_version=version, chunk_index=-(draft.parent_index + 1),
                    content=content, content_hash=hashlib.sha256(content.encode()).hexdigest(),
                    attributes={**{k: v for k, v in source_metadata.items() if k != '_trusted_pages'},
                        'pageNumbers': draft.metadata['parent_page_numbers'],
                        'source_name': source_name, 'chunk_kind': 'parent'}))
            await session.execute(insert(KnowledgeChunk), rows)
            run.lease_until = datetime.now(UTC) + timedelta(seconds=900)
            await session.commit()
    for offset in range(0, len(drafts), batch_size):
        batch = drafts[offset:offset + batch_size]
        async with sessions() as session:
            ids = await new_ids(session, len(batch))
            *_, run = await validate(session, document_id, tenant_id, version, include_content=False)
            rows = [dict(id=identity, tenant_id=tenant_id, knowledge_base_id=base_id,
                document_id=document_id, index_version=version,
                parent_chunk_id=parents[draft.parent_index], chunk_index=draft.child_index,
                content=draft.content, content_hash=draft.content_hash,
                attributes={**persistent_metadata(draft.metadata), 'chunk_kind': 'child', 'indexVersion': str(version)})
                for draft, identity in zip(batch, ids, strict=True)]
            await session.execute(insert(KnowledgeChunk), rows)
            await insert_new_chunks(session, rows)
            run.lease_until = datetime.now(UTC) + timedelta(seconds=900)
            await session.commit()
