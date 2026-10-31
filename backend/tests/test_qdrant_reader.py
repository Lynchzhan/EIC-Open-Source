import asyncio
from dataclasses import dataclass

import pytest

from eic.vector.qdrant_reader import QdrantReadAdapter


@dataclass
class Point:
    id: int
    score: float
    payload: dict


@dataclass
class QueryResponse:
    points: list[Point]


class FakeQdrant:
    async def query_points(self, **kwargs):
        assert kwargs["collection_name"] == "eic_documents"
        assert kwargs["limit"] == 2
        assert kwargs["query_filter"] == {"tenant_id": 1000}
        return QueryResponse([Point(7, 0.91, {"text": "企智"}), Point(8, 0.82, {"text": "中心"})])


def test_qdrant_adapter_is_read_only_and_normalizes_hits() -> None:
    adapter = QdrantReadAdapter(FakeQdrant(), "eic_documents")
    hits = asyncio.run(adapter.search([0.1, 0.2], limit=2, query_filter={"tenant_id": 1000}))

    assert hits[0].point_id == "7"
    assert hits[0].score == 0.91
    assert hits[1].payload == {"text": "中心"}


def test_qdrant_adapter_rejects_invalid_limits() -> None:
    adapter = QdrantReadAdapter(FakeQdrant(), "eic_documents")
    with pytest.raises(ValueError):
        asyncio.run(adapter.search([0.1], limit=0))
