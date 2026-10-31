"""LangChain/OpenAI SDK error mapping contracts using synthetic failures."""

from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any

import pytest
from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AuthenticationError,
    BadRequestError,
    InternalServerError,
    NotFoundError,
    PermissionDeniedError,
    RateLimitError,
    UnprocessableEntityError,
)
from openai._base_client import httpx2

os.environ.setdefault(
    "EIC_SECRET_ENCRYPTION_KEY",
    "test-encryption-secret-with-at-least-thirty-two-characters",
)

from eic.ai import reliable_calls
from eic.ai.langchain_errors import (
    map_langchain_stream_errors,
    map_openai_exception,
    raise_model_provider_error,
)
from eic.ai.providers import ModelProviderError

_PRIVATE_DETAIL = "private-provider-response-detail"
_REQUEST_URL = "https://provider-fixture.invalid/v1/chat/completions"


class FakeAsyncIterator:
    def __init__(
        self,
        chunks: tuple[str, ...] = (),
        *,
        failure: BaseException | None = None,
        cleanup_failure: Exception | None = None,
    ) -> None:
        self._chunks = iter(chunks)
        self._failure = failure
        self._cleanup_failure = cleanup_failure
        self.close_count = 0

    def __aiter__(self) -> FakeAsyncIterator:
        return self

    async def __anext__(self) -> str:
        try:
            return next(self._chunks)
        except StopIteration:
            if self._failure is not None:
                failure, self._failure = self._failure, None
                raise failure
            raise StopAsyncIteration

    async def aclose(self) -> None:
        self.close_count += 1
        if self._cleanup_failure is not None:
            raise self._cleanup_failure


def make_status_error(
    status: int,
    error_type: type[APIStatusError] = APIStatusError,
) -> APIStatusError:
    request = httpx2.Request("POST", _REQUEST_URL)
    response = httpx2.Response(
        status,
        request=request,
        json={"error": {"message": _PRIVATE_DETAIL}},
    )
    return error_type(
        f"SDK message: {_PRIVATE_DETAIL}",
        response=response,
        body={"error": {"message": _PRIVATE_DETAIL}},
    )


def make_connection_error() -> APIConnectionError:
    return APIConnectionError(
        message=f"Connection failed: {_PRIVATE_DETAIL}",
        request=httpx2.Request("POST", _REQUEST_URL),
    )


def make_timeout_error() -> APITimeoutError:
    return APITimeoutError(request=httpx2.Request("POST", _REQUEST_URL))


@pytest.mark.parametrize(
    ("status", "error_type", "reason", "retryable"),
    [
        (400, BadRequestError, "provider_parameters_rejected", False),
        (401, AuthenticationError, "invalid_provider_credentials", False),
        (403, PermissionDeniedError, "provider_permission_denied", False),
        (404, NotFoundError, "provider_error", False),
        (408, APIStatusError, "provider_outcome_uncertain", False),
        (422, UnprocessableEntityError, "provider_parameters_rejected", False),
        (429, RateLimitError, "provider_rate_limited", True),
        (500, InternalServerError, "provider_outcome_uncertain", False),
        (503, InternalServerError, "provider_outcome_uncertain", False),
    ],
)
def test_sdk_http_status_errors_map_to_stable_eic_contract(
    status: int,
    error_type: type[APIStatusError],
    reason: str,
    retryable: bool,
) -> None:
    sdk_error = make_status_error(status, error_type)

    mapped = map_openai_exception(sdk_error)

    assert isinstance(mapped, ModelProviderError)
    assert mapped.reason == reason
    assert mapped.retryable is retryable
    assert _PRIVATE_DETAIL not in str(mapped)
    assert _PRIVATE_DETAIL not in repr(mapped)


@pytest.mark.parametrize("make_error", [make_connection_error, make_timeout_error])
def test_sdk_connection_and_timeout_errors_are_uncertain_and_sanitized(make_error: Any) -> None:
    mapped = map_openai_exception(make_error())

    assert mapped.reason == "provider_outcome_uncertain"
    assert mapped.retryable is False
    assert _PRIVATE_DETAIL not in str(mapped)


@pytest.mark.parametrize(
    "error_type",
    [asyncio.CancelledError, GeneratorExit, KeyboardInterrupt, SystemExit],
)
def test_non_exception_control_flow_is_passed_through_unchanged(
    error_type: type[BaseException],
) -> None:
    original = error_type("control flow")

    with pytest.raises(error_type) as mapped_error:
        map_openai_exception(original)
    assert mapped_error.value is original

    with pytest.raises(error_type) as raised_error:
        raise_model_provider_error(original)
    assert raised_error.value is original


def test_existing_eic_provider_error_is_returned_and_raised_unchanged() -> None:
    original = ModelProviderError(
        "existing EIC error",
        reason="existing_eic_reason",
        retryable=True,
    )

    assert map_openai_exception(original) is original
    with pytest.raises(ModelProviderError) as caught:
        raise_model_provider_error(original)
    assert caught.value is original
    assert caught.value.reason == "existing_eic_reason"
    assert caught.value.retryable is True


def test_unknown_sdk_error_is_sanitized_without_exposing_exception_chain() -> None:
    original = RuntimeError(_PRIVATE_DETAIL)

    with pytest.raises(ModelProviderError) as caught:
        raise_model_provider_error(original)

    assert caught.value.reason == "provider_outcome_uncertain"
    assert caught.value.retryable is False
    assert _PRIVATE_DETAIL not in str(caught.value)
    assert caught.value.__cause__ is None
    assert caught.value.__suppress_context__ is True


def test_stream_error_is_translated_after_preserving_chunks_and_closing_source() -> None:
    source = FakeAsyncIterator(
        ("visible prefix",),
        failure=make_status_error(403, PermissionDeniedError),
    )
    visible: list[str] = []

    async def collect() -> None:
        async for chunk in map_langchain_stream_errors(source):
            visible.append(chunk)

    with pytest.raises(ModelProviderError) as caught:
        asyncio.run(collect())

    assert visible == ["visible prefix"]
    assert caught.value.reason == "provider_permission_denied"
    assert caught.value.retryable is False
    assert _PRIVATE_DETAIL not in str(caught.value)
    assert source.close_count == 1


def test_stream_source_is_closed_on_success_and_cleanup_failure_keeps_primary_error() -> None:
    completed = FakeAsyncIterator(("answer",))

    async def collect_success() -> list[str]:
        return [chunk async for chunk in map_langchain_stream_errors(completed)]

    assert asyncio.run(collect_success()) == ["answer"]
    assert completed.close_count == 1

    source = FakeAsyncIterator(
        failure=make_status_error(429, RateLimitError),
        cleanup_failure=RuntimeError(_PRIVATE_DETAIL),
    )

    async def collect_failure() -> None:
        async for _ in map_langchain_stream_errors(source):
            pass

    with pytest.raises(ModelProviderError) as caught:
        asyncio.run(collect_failure())
    assert caught.value.reason == "provider_rate_limited"
    assert caught.value.retryable is True
    assert _PRIVATE_DETAIL not in str(caught.value)
    assert source.close_count == 1


def test_stream_cancellation_is_passed_through_after_source_cleanup() -> None:
    cancellation = asyncio.CancelledError("cancel stream")
    source = FakeAsyncIterator(failure=cancellation)

    async def collect() -> None:
        async for _ in map_langchain_stream_errors(source):
            pass

    with pytest.raises(asyncio.CancelledError) as caught:
        asyncio.run(collect())
    assert caught.value is cancellation
    assert source.close_count == 1


def patch_reliable_stream_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = SimpleNamespace(
        dependency_controls_enabled=True,
        provider_rate_limit_retries=1,
        provider_total_timeout_seconds=5.0,
    )
    monkeypatch.setattr(reliable_calls, "get_settings", lambda: settings)
    monkeypatch.setattr(
        reliable_calls,
        "chat_parameters",
        lambda _model: ({}, {"inputLimit": 16, "outputLimit": 16}),
    )

    @asynccontextmanager
    async def allow_local_attempt(*_args: Any, **_kwargs: Any) -> Any:
        yield {}

    monkeypatch.setattr(reliable_calls, "guard", allow_local_attempt)


def run_reliable_stream(factory: Any, metadata: dict[str, Any]) -> list[str]:
    model = SimpleNamespace(
        id=1,
        tenant_id=1,
        provider="synthetic",
        endpoint=_REQUEST_URL,
    )

    async def collect() -> list[str]:
        return [
            chunk
            async for chunk in reliable_calls.reliable_stream(
                factory,
                model,
                sessions=object(),
                metadata=metadata,
            )
        ]

    return asyncio.run(collect())


def test_reliable_stream_retries_sdk_429_only_before_output(monkeypatch: pytest.MonkeyPatch) -> None:
    patch_reliable_stream_runtime(monkeypatch)
    calls = 0
    sources: list[FakeAsyncIterator] = []

    def factory() -> Any:
        nonlocal calls
        calls += 1
        source = (
            FakeAsyncIterator(failure=make_status_error(429, RateLimitError))
            if calls == 1
            else FakeAsyncIterator(("answer",))
        )
        sources.append(source)
        return map_langchain_stream_errors(source)

    metadata: dict[str, Any] = {}
    assert run_reliable_stream(factory, metadata) == ["answer"]
    assert calls == 2
    assert metadata["retryCount"] == 1
    assert metadata["providerInvocations"] == 2
    assert [source.close_count for source in sources] == [1, 1]


@pytest.mark.parametrize(
    ("failure_kind", "reason"),
    [
        ("unknown", "provider_outcome_uncertain"),
        ("connection", "provider_outcome_uncertain"),
        ("timeout", "provider_outcome_uncertain"),
        ("http_408", "provider_outcome_uncertain"),
        ("http_500", "provider_outcome_uncertain"),
    ],
)
def test_reliable_stream_does_not_retry_unstarted_non_429_failures(
    monkeypatch: pytest.MonkeyPatch,
    failure_kind: str,
    reason: str,
) -> None:
    patch_reliable_stream_runtime(monkeypatch)
    failure = {
        "unknown": lambda: RuntimeError(_PRIVATE_DETAIL),
        "connection": make_connection_error,
        "timeout": make_timeout_error,
        "http_408": lambda: make_status_error(408),
        "http_500": lambda: make_status_error(500, InternalServerError),
    }[failure_kind]()
    calls = 0
    sources: list[FakeAsyncIterator] = []

    def factory() -> Any:
        nonlocal calls
        calls += 1
        source = FakeAsyncIterator(failure=failure)
        sources.append(source)
        return map_langchain_stream_errors(source)

    metadata: dict[str, Any] = {}
    with pytest.raises(ModelProviderError) as caught:
        run_reliable_stream(factory, metadata)

    assert caught.value.reason == reason
    assert calls == 1
    assert "retryCount" not in metadata
    assert [source.close_count for source in sources] == [1]


@pytest.mark.parametrize(
    ("failure_kind", "reason", "retryable"),
    [
        ("http_429", "provider_rate_limited", True),
        ("unknown", "provider_outcome_uncertain", False),
        ("timeout", "provider_outcome_uncertain", False),
    ],
)
def test_reliable_stream_never_replays_after_visible_output(
    monkeypatch: pytest.MonkeyPatch,
    failure_kind: str,
    reason: str,
    retryable: bool,
) -> None:
    patch_reliable_stream_runtime(monkeypatch)
    failure = {
        "http_429": lambda: make_status_error(429, RateLimitError),
        "unknown": lambda: RuntimeError(_PRIVATE_DETAIL),
        "timeout": make_timeout_error,
    }[failure_kind]()
    calls = 0
    sources: list[FakeAsyncIterator] = []

    def factory() -> Any:
        nonlocal calls
        calls += 1
        source = FakeAsyncIterator(("visible prefix",), failure=failure)
        sources.append(source)
        return map_langchain_stream_errors(source)

    metadata: dict[str, Any] = {}
    with pytest.raises(ModelProviderError) as caught:
        run_reliable_stream(factory, metadata)

    assert caught.value.reason == reason
    assert caught.value.retryable is retryable
    assert calls == 1
    assert metadata["providerInvocations"] == 1
    assert "retryCount" not in metadata
    assert [source.close_count for source in sources] == [1]
    assert _PRIVATE_DETAIL not in str(caught.value)
