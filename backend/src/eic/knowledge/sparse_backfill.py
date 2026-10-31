"""离线、可续接的稀疏索引回填，不发起模型或向量数据库请求。"""
import argparse
import asyncio
import json

from sqlalchemy import select

from eic.core.database import SessionLocal
from eic.knowledge.sparse_index import index_chunk, tokenizer_version
from eic.models import KnowledgeChunk, KnowledgeSparseChunk


async def backfill(*, tenant_id, knowledge_base_id, after_id=0, batch_size=100, max_chunks=1000, apply=False):
    if min(tenant_id, knowledge_base_id, batch_size, max_chunks) < 1 or batch_size > 500 or after_id < 0:
        raise ValueError("invalid_backfill_scope")
    scanned = written = 0
    cursor = after_id
    while scanned < max_chunks:
        async with SessionLocal() as session:
            rows = list(await session.scalars(select(KnowledgeChunk).where(
                KnowledgeChunk.tenant_id == tenant_id, KnowledgeChunk.knowledge_base_id == knowledge_base_id,
                KnowledgeChunk.id > cursor, KnowledgeChunk.chunk_index >= 0).order_by(KnowledgeChunk.id)
                .limit(min(batch_size, max_chunks - scanned)).with_for_update()))
            if not rows:
                break
            for chunk in rows:
                previous = await session.get(KnowledgeSparseChunk, chunk.id)
                if previous is None or previous.content_hash != chunk.content_hash or previous.tokenizer_version != tokenizer_version():
                    if apply:
                        await index_chunk(session, chunk)
                    written += 1
            if apply:
                await session.commit()
            cursor = rows[-1].id
            scanned += len(rows)
    return {"mode": "apply" if apply else "plan", "tenantId": tenant_id,
        "knowledgeBaseId": knowledge_base_id, "scanned": scanned, "indexed" if apply else "wouldIndex": written,
        "afterId": cursor, "batchSize": batch_size, "maxChunks": max_chunks,
        "mayHaveMore": scanned == max_chunks, "tokenizerVersion": tokenizer_version()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tenant-id", type=int, required=True)
    parser.add_argument("--knowledge-base-id", type=int, required=True)
    parser.add_argument("--after-id", type=int, default=0)
    parser.add_argument("--batch-size", type=int, default=100)
    parser.add_argument("--max-chunks", type=int, default=1000)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    print(json.dumps(asyncio.run(backfill(**vars(args))), ensure_ascii=False))


if __name__ == "__main__":
    main()
