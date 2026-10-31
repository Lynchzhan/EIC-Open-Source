"""知识库和文档的墓碑、执行失效与投影清理共用业务事务。"""
from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.orm import defer

from eic.core import outbox
from eic.models import KnowledgeDocument, KnowledgeIngestionRun, OutboxEvent


async def retire_document(session, document):
    document.is_deleted = True
    document.source_revision += 1
    document.pending_index_version = None
    document.status = document.index_status = 'deleted'
    await session.execute(update(KnowledgeIngestionRun).where(
        KnowledgeIngestionRun.tenant_id == document.tenant_id,
        KnowledgeIngestionRun.document_id == document.id,
        KnowledgeIngestionRun.status.in_(['pending', 'parsing', 'embedding', 'parsed', 'staged']),
    ).values(status='cancelled', lease_until=None, finished_at=datetime.now(UTC)))
    await session.execute(update(OutboxEvent).where(
        OutboxEvent.tenant_id == document.tenant_id, OutboxEvent.entity_id == document.id,
        OutboxEvent.kind.in_(['knowledge-document', 'document-embeddings']),
        OutboxEvent.consume_status.in_(['pending', 'running']),
    ).values(status='sent', consume_status='completed', owner_token=None, lease_until=None,
             consume_token=None, consume_until=None, result={'status': 'cancelled'}))
    await outbox.stage(session, 'index-cleanup', document.id, document.tenant_id,
                       event_key=f'document-retired:{document.id}:{document.source_revision}')


async def retire_base_documents(session, resource):
    after = 0
    while True:
        documents = list(await session.scalars(select(KnowledgeDocument).options(
            defer(KnowledgeDocument.content, raiseload=True)).where(
                KnowledgeDocument.tenant_id == resource.tenant_id,
                KnowledgeDocument.knowledge_base_id == resource.id,
                KnowledgeDocument.is_deleted.is_(False), KnowledgeDocument.id > after,
            ).order_by(KnowledgeDocument.id).limit(100).with_for_update()))
        if not documents:
            return
        for document in documents:
            await retire_document(session, document)
        after = documents[-1].id
