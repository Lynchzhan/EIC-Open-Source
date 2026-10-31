from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True)
class VectorHit:
    point_id: str
    score: float
    payload: dict[str, Any]


class QdrantQueryClient(Protocol):
    async def query_points(self, *, collection_name: str, query: list[float], limit: int, **kwargs: Any) -> Any:
        ...


class QdrantReadAdapter:
    """Read-only Qdrant adapter for migration comparison and recall audits."""

    def __init__(self, client: QdrantQueryClient, collection_name: str) -> None:
        self._client = client
        self.collection_name = collection_name

    async def search(self, vector: list[float], *, limit: int = 5, query_filter: Any = None) -> list[VectorHit]:
        if not vector:
            return []
        if limit < 1 or limit > 100:
            raise ValueError("limit must be between 1 and 100")
        response = await self._client.query_points(
            collection_name=self.collection_name,
            query=vector,
            limit=limit,
            query_filter=query_filter,
        )
        points = getattr(response, "points", response)
        hits: list[VectorHit] = []
        for point in points:
            if isinstance(point, dict):
                point_id = point.get("id")
                score = point.get("score", 0.0)
                payload = point.get("payload") or {}
            else:
                point_id = point.id
                score = point.score
                payload = point.payload or {}
            hits.append(VectorHit(point_id=str(point_id), score=float(score), payload=dict(payload)))
        return hits
