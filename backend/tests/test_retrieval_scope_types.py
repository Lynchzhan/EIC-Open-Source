import pytest
from pydantic import ValidationError

from eic.knowledge.retrieval import EICRetriever
from eic.vector.qdrant_reader import VectorHit


@pytest.mark.parametrize(
    ("field", "value"),
    [
        (field, value)
        for field in ("tenant_id", "knowledge_base_id")
        for value in (True, 1.0, "1", 0, -1)
    ],
)
def test_retriever_rejects_non_positive_or_non_integer_scope(field, value) -> None:
    with pytest.raises(ValidationError):
        EICRetriever(
            search=lambda *_args, **_kwargs: [],
            tenant_id=1 if field != "tenant_id" else value,
            knowledge_base_id=1 if field != "knowledge_base_id" else value,
        )


@pytest.mark.parametrize(
    "metadata_key",
    ("tenant_id", "tenantId", "knowledge_base_id", "knowledgeBaseId"),
)
@pytest.mark.parametrize("invalid_value", (True, 1.0, "1", 0, -1))
def test_retriever_drops_hits_with_mistyped_or_invalid_metadata_scope(
    metadata_key, invalid_value,
) -> None:
    metadata = {
        "tenant_id": 1,
        "tenantId": 1,
        "knowledge_base_id": 1,
        "knowledgeBaseId": 1,
        "document_id": 11,
        "content": "must be filtered",
    }
    metadata[metadata_key] = invalid_value
    retriever = EICRetriever(
        search=lambda *_args, **_kwargs: [VectorHit("scope-hit", 0.5, metadata)],
        tenant_id=1,
        knowledge_base_id=1,
    )

    assert retriever.invoke("query") == []


@pytest.mark.parametrize(
    "metadata",
    (
        {"tenant_id": 1, "knowledge_base_id": 1},
        {"tenantId": 1, "knowledgeBaseId": 1},
        {"tenant_id": 1, "tenantId": 1, "knowledge_base_id": 1, "knowledgeBaseId": 1},
    ),
)
def test_retriever_preserves_valid_integer_scope_aliases(metadata) -> None:
    metadata = {**metadata, "document_id": 11, "content": "allowed"}
    retriever = EICRetriever(
        search=lambda *_args, **_kwargs: [VectorHit("valid-scope", 0.5, metadata)],
        tenant_id=1,
        knowledge_base_id=1,
    )

    result = retriever.invoke("query")

    assert [document.metadata["chunk_id"] for document in result] == ["valid-scope"]
