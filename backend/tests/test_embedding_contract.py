import asyncio
from unittest.mock import AsyncMock

import httpx
import pytest
from cryptography.fernet import InvalidToken

from eic.ai import embeddings
from eic.ai.providers import ModelProviderError
from eic.models import AIModel


def model():
    return AIModel(id=1, tenant_id=1, model_type="embedding", provider="openai",
                   model_name="fixture", endpoint="http://fixture.invalid", enabled=True,
                   embedding_dimensions=2)


def request(data):
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json={"data": data}))) as client:
            return await embeddings.embed_texts(model(), ["first", "second"], client=client, tenant_id=1)
    return asyncio.run(run())


def test_provider_indices_restore_input_order():
    assert request([{"index": 1, "embedding": [0., 1.]},
                    {"index": 0, "embedding": [1., 0.]}]) == [[1., 0.], [0., 1.]]


@pytest.mark.parametrize("indices", [[1, 1], [True, 1], [-1, 1], [2, 1], ["0", 1], [0]])
def test_invalid_provider_indices_fail_entire_batch(indices):
    with pytest.raises(ModelProviderError):
        request([{"index": index, "embedding": [1., 0.]} for index in indices])


@pytest.mark.parametrize("vector", [[], ["NaN", 0], ["Infinity", 0], [1], [1, 0, 0]])
def test_invalid_vectors_fail_entire_batch(vector):
    with pytest.raises(ModelProviderError):
        request([{"index": 0, "embedding": vector}, {"index": 1, "embedding": [1., 0.]}])


@pytest.mark.parametrize("error_type", [ValueError, InvalidToken])
def test_decryption_failure_closes_owned_client(monkeypatch, error_type):
    owned = AsyncMock()
    monkeypatch.setattr(embeddings.httpx, "AsyncClient", lambda **kwargs: owned)
    monkeypatch.setattr(embeddings, "decrypt_secret", lambda _: (_ for _ in ()).throw(error_type("fixture")))
    item = model()
    item.secret_encrypted = "invalid-fixture"
    with pytest.raises(ModelProviderError):
        asyncio.run(embeddings.embed_texts(item, ["first"], tenant_id=1))
    owned.aclose.assert_awaited_once()
    owned.post.assert_not_awaited()
