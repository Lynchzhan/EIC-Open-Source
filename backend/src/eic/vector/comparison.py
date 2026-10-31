from __future__ import annotations

from dataclasses import dataclass

from eic.vector.qdrant_reader import VectorHit


@dataclass(frozen=True)
class RecallComparison:
    overlap_count: int
    union_count: int
    jaccard: float
    pgvector_only: list[str]
    qdrant_only: list[str]


def compare_recall(pgvector_hits: list[VectorHit], qdrant_hits: list[VectorHit]) -> RecallComparison:
    pgvector_ids = {hit.point_id for hit in pgvector_hits}
    qdrant_ids = {hit.point_id for hit in qdrant_hits}
    union = pgvector_ids | qdrant_ids
    return RecallComparison(
        overlap_count=len(pgvector_ids & qdrant_ids),
        union_count=len(union),
        jaccard=len(pgvector_ids & qdrant_ids) / len(union) if union else 1.0,
        pgvector_only=sorted(pgvector_ids - qdrant_ids),
        qdrant_only=sorted(qdrant_ids - pgvector_ids),
    )
