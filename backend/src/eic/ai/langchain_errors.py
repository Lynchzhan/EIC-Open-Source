"""Translate LangChain/OpenAI SDK failures into safe EIC provider errors."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import NoReturn, TypeVar

from openai import APIConnectionError, APIStatusError

from eic.ai.providers import ModelProviderError
from eic.ai.stream_reader import complete_cleanup

_Chunk = TypeVar("_Chunk")


def map_openai_exception(exc: BaseException) -> ModelProviderError:
    """Map SDK failures without retaining provider response bodies or messages.

    Cancellation and process-control exceptions are re-raised unchanged. Only
    HTTP 429 is retryable; ``reliable_stream`` still bounds retries to failures
    before visible output. Callers should re-raise the returned error with
    ``from None`` so SDK response details do not appear in the exception chain.
    """
    if not isinstance(exc, Exception):
        raise exc
    if isinstance(exc, ModelProviderError):
        return exc

    if isinstance(exc, APIStatusError):
        status = exc.status_code
        if status in {400, 422}:
            return ModelProviderError(
                "模型服务拒绝了已配置的请求参数。",
                reason="provider_parameters_rejected",
                retryable=False,
            )
        if status == 401:
            return ModelProviderError(
                "模型凭据无效或已过期。",
                reason="invalid_provider_credentials",
                retryable=False,
            )
        if status == 403:
            return ModelProviderError(
                "模型服务拒绝当前调用权限。",
                reason="provider_permission_denied",
                retryable=False,
            )
        if status == 429:
            return ModelProviderError(
                "模型服务正在限流，请稍后重试。",
                reason="provider_rate_limited",
                retryable=True,
            )
        if status == 408 or status >= 500:
            return ModelProviderError(
                "模型调用结果不确定，系统未自动重发。",
                reason="provider_outcome_uncertain",
                retryable=False,
            )
        return ModelProviderError(
            "模型服务请求失败。",
            reason="provider_error",
            retryable=False,
        )

    if isinstance(exc, APIConnectionError):
        return ModelProviderError(
            "模型调用结果不确定，系统未自动重发。",
            reason="provider_outcome_uncertain",
            retryable=False,
        )

    return ModelProviderError(
        "模型调用结果不确定，系统未自动重发。",
        reason="provider_outcome_uncertain",
        retryable=False,
    )


def raise_model_provider_error(exc: BaseException) -> NoReturn:
    """Raise a sanitized EIC error while preserving cancellation semantics."""
    if not isinstance(exc, Exception):
        raise exc
    if isinstance(exc, ModelProviderError):
        raise exc
    raise map_openai_exception(exc) from None


async def map_langchain_stream_errors(source: AsyncIterator[_Chunk]) -> AsyncIterator[_Chunk]:
    """Translate errors at the ChatOpenAI stream boundary and always close its source."""
    failure: Exception | None = None
    try:
        async for chunk in source:
            yield chunk
    except Exception as exc:  # noqa: BLE001 - 未知 SDK 错误统一脱敏；CancelledError 继续传播。
        failure = exc
    finally:
        close = getattr(source, "aclose", None)
        if callable(close):
            try:
                await complete_cleanup(close())
            except Exception as cleanup_error:  # noqa: BLE001 - 清理失败不覆盖原始 Provider 错误。
                if failure is None:
                    failure = cleanup_error
    if failure is not None:
        raise_model_provider_error(failure)
