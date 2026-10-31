import asyncio

from eic.vector.pgvector_store import PgVectorStore


class ScopeFilteringFakeConnection:
    def __init__(self, rows):
        self.rows = rows
        self.fetch_call = None

    async def fetch(self, query, *args):
        self.fetch_call = (query, args)
        sql = " ".join(query.split()).upper()
        where_position = sql.index(" WHERE ")
        order_position = sql.index(" ORDER BY ")
        limit_position = sql.index(" LIMIT ")
        predicates = sql[where_position:order_position]

        assert where_position < order_position < limit_position
        assert "TENANT_ID = $1" in predicates
        assert "KNOWLEDGE_BASE_ID = $2" in predicates

        tenant_id, knowledge_base_id, _, limit, chunk_ids = args
        scoped_rows = [
            row for row in self.rows
            if row["tenant_id"] == tenant_id
            and row["knowledge_base_id"] == knowledge_base_id
        ]
        if chunk_ids is not None:
            scoped_rows = [row for row in scoped_rows if row["chunk_id"] in chunk_ids]

        ranked_rows = sorted(scoped_rows, key=lambda row: row["distance"])
        return [
            {
                "chunk_id": row["chunk_id"],
                "content": row["content"],
                "metadata": row["metadata"],
                "score": 1 - row["distance"],
            }
            for row in ranked_rows[:limit]
        ]


def vector_row(chunk_id, tenant_id, knowledge_base_id, distance):
    return {
        "chunk_id": chunk_id,
        "tenant_id": tenant_id,
        "knowledge_base_id": knowledge_base_id,
        "distance": distance,
        "content": chunk_id,
        "metadata": {},
    }


def test_search_filters_scope_before_similarity_order_and_limit():
    connection = ScopeFilteringFakeConnection(
        [
            vector_row("other-tenant", 8, 17, 0.01),
            vector_row("other-knowledge-base", 7, 18, 0.02),
            vector_row("other-tenant-and-base", 8, 18, 0.03),
            vector_row("in-scope", 7, 17, 0.40),
            vector_row("in-scope-second", 7, 17, 0.50),
        ]
    )

    hits = asyncio.run(
        PgVectorStore(dimensions=2).search(
            connection,
            tenant_id=7,
            knowledge_base_id=17,
            vector=[0.1, 0.2],
            limit=1,
        )
    )

    assert connection.fetch_call is not None
    query, args = connection.fetch_call
    assert args == (7, 17, "[0.1, 0.2]", 1, None)
    assert [hit.point_id for hit in hits] == ["in-scope"]
    assert hits[0].score == 0.6
    assert hits[0].payload["content"] == "in-scope"
    assert "WHERE tenant_id = $1 AND knowledge_base_id = $2" in query
    assert query.index("WHERE tenant_id = $1 AND knowledge_base_id = $2") < query.index("ORDER BY")
    assert query.index("ORDER BY") < query.index("LIMIT $4")


def test_chunk_id_filter_cannot_return_out_of_scope_rows():
    connection = ScopeFilteringFakeConnection(
        [
            vector_row("other-tenant", 8, 17, 0.01),
            vector_row("other-knowledge-base", 7, 18, 0.02),
            vector_row("in-scope-not-requested", 7, 17, 0.03),
        ]
    )

    hits = asyncio.run(
        PgVectorStore(dimensions=2).search(
            connection,
            tenant_id=7,
            knowledge_base_id=17,
            vector=[0.1, 0.2],
            limit=5,
            chunk_ids=["other-tenant", "other-knowledge-base"],
        )
    )

    assert connection.fetch_call is not None
    query, args = connection.fetch_call
    assert args == (7, 17, "[0.1, 0.2]", 5, ["other-tenant", "other-knowledge-base"])
    assert hits == []
    assert "AND ($5::text[] IS NULL OR chunk_id=ANY($5::text[]))" in query
    assert query.index("knowledge_base_id = $2") < query.index("ORDER BY")
    assert query.index("ORDER BY") < query.index("LIMIT $4")
