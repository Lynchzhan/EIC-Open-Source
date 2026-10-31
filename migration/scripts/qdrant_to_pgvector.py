"""Explicit Qdrant read -> PGVector import for the migration window.

This command reads Qdrant points and writes target PGVector rows. It requires
all connection arguments explicitly; it never deletes or updates Qdrant.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

import asyncpg
from qdrant_client import AsyncQdrantClient

try:
    BACKEND_SRC = Path(__file__).parents[2] / "backend" / "src"
except IndexError:
    BACKEND_SRC = Path("/app/src")
if not BACKEND_SRC.is_dir():
    # The migration command can also run inside the backend image, where the
    # package is installed under /app rather than beside the repository root.
    BACKEND_SRC = Path("/app/src")
if BACKEND_SRC.is_dir():
    sys.path.insert(0, str(BACKEND_SRC))
from eic.vector.pgvector_store import ChunkRecord, PgVectorStore


async def main(args: argparse.Namespace) -> None:
    qdrant = AsyncQdrantClient(url=args.qdrant_url, api_key=args.qdrant_api_key)
    points, offset = [], None
    try:
        while True:
            batch, offset = await qdrant.scroll(collection_name=args.collection, limit=args.batch_size, offset=offset, with_payload=True, with_vectors=True)
            points.extend(batch)
            if offset is None or not batch:
                break
    finally:
        await qdrant.close()
    connection = await asyncpg.connect(args.pg_url)
    imported, skipped = 0, 0
    valid_vectors = []
    for point in points:
        vector = point.vector
        if isinstance(vector, dict):
            vector = next(iter(vector.values()), [])
        if vector:
            valid_vectors.append(vector)
    dimensions = len(valid_vectors[0]) if valid_vectors else args.embedding_dimensions
    if not dimensions:
        raise ValueError("collection has no vectors and no embedding dimension was supplied")
    store = PgVectorStore(dimensions=dimensions)
    try:
        await store.initialize(connection, dimensions=dimensions)
        for point in points:
            vector = point.vector
            if isinstance(vector, dict):
                vector = next(iter(vector.values()), [])
            payload = dict(point.payload or {})
            content = str(payload.get("content") or payload.get("text") or "")
            if not content or not vector:
                skipped += 1
                continue
            if len(vector) != dimensions:
                skipped += 1
                continue
            raw_document_id = payload.get("document_id") or payload.get("documentId")
            try:
                document_id = int(raw_document_id)
            except (TypeError, ValueError):
                document_id = 0
            if document_id <= 0:
                skipped += 1
                continue
            await store.upsert(connection, ChunkRecord(
                chunk_id=str(point.id), tenant_id=args.tenant_id,
                knowledge_base_id=args.knowledge_base_id,
                document_id=document_id,
                content=content, vector=[float(value) for value in vector], metadata=payload,
            ))
            imported += 1
    finally:
        await connection.close()
    report = {"generated_at": datetime.now(UTC).isoformat(), "collection": args.collection, "points_read": len(points), "embedding_dimensions": dimensions, "imported": imported, "skipped": skipped, "qdrant_write": False}
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))


parser = argparse.ArgumentParser()
parser.add_argument("--qdrant-url", required=True)
parser.add_argument("--qdrant-api-key")
parser.add_argument("--collection", required=True)
parser.add_argument("--pg-url", required=True)
parser.add_argument("--tenant-id", type=int, required=True)
parser.add_argument("--knowledge-base-id", type=int, required=True)
parser.add_argument("--batch-size", type=int, default=100)
parser.add_argument("--embedding-dimensions", type=int, default=1024)
parser.add_argument("--report", type=Path, default=Path(__file__).parents[1] / "reports" / "qdrant_pgvector_migration.json")
asyncio.run(main(parser.parse_args()))
