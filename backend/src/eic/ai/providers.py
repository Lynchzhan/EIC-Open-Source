from __future__ import annotations

import re
from collections.abc import AsyncIterator
from typing import Any

import httpx

from eic.ai.budget import ChatConfigurationError, message_units, positive_budget
from eic.core.observability import observe_model_stream
from eic.models import AIModel

CONNECT_TIMEOUT_SECONDS = 10.0
READ_TIMEOUT_SECONDS = 180.0


class ModelProviderError(RuntimeError):
    """A provider failure that is safe to present as a generic chat error."""

    def __init__(self, message: str, *, reason: str = "provider_error", retryable: bool = True):
        super().__init__(message)
        self.reason = reason
        self.retryable = retryable


CHAT_PARAMETER_PROFILES = {"auto", "openai_legacy", "openai_completion", "openai_reasoning", "ollama"}


def chat_parameters(model: AIModel, temperature: float | None = None, *, reasoning_selection=None) -> tuple[dict, dict]:
    provider = model.provider.strip().lower()
    if provider in {'openai_responses', 'anthropic'}:
        provider = 'openai_compatible'
    if provider not in {"openai", "openai_compatible", "1", "azure", "2", "zhipu", "3",
                         "dashscope", "12", "ollama", "10"}:
        raise ChatConfigurationError("unsupported_chat_provider")
    profile = getattr(model, "chat_parameter_profile", None) or "auto"
    if profile not in CHAT_PARAMETER_PROFILES:
        raise ChatConfigurationError("unsupported_parameter_profile")
    if profile == "auto":
        if _is_ollama(model):
            profile = "ollama"
        elif provider in {"openai", "openai_compatible", "1", "azure", "2", "zhipu", "3", "dashscope", "12"}:
            profile = ("openai_reasoning" if re.match(r"^(o[134](?:-|$)|gpt-[56](?:[.-]|$))", model.model_name)
                       else "openai_legacy")
        else:
            raise ChatConfigurationError("unsupported_chat_provider")
    if (_is_ollama(model)) != (profile == "ollama"):
        raise ChatConfigurationError("provider_profile_mismatch")
    input_limit = positive_budget(model.max_input_tokens, 131072)
    output_limit = positive_budget(model.max_output_tokens, 8192)
    from eic.ai.runtime_policy import ACTIVE
    account = ACTIVE.get()
    if account is not None:
        output_limit = min(output_limit, account.policy["maxOutputTokens"])
    if temperature is not None and (isinstance(temperature, bool) or not isinstance(temperature, (float, int))
                                    or not 0 <= temperature <= 2):
        raise ChatConfigurationError("invalid_temperature")
    if profile == "openai_reasoning" and temperature is not None:
        raise ChatConfigurationError("temperature_not_supported")
    parameters = {}
    if profile == "ollama":
        parameters["options"] = {"temperature": 1.0 if temperature is None else temperature,
                                 "num_predict": output_limit, "num_ctx": input_limit + output_limit}
        output_field = "options.num_predict"
    else:
        output_field = "max_tokens" if profile == "openai_legacy" else "max_completion_tokens"
        parameters[output_field] = output_limit
        if profile != "openai_reasoning":
            parameters["temperature"] = 1.0 if temperature is None else temperature
    from eic.ai.reasoning_policy import effective_reasoning
    reasoning_parameters, reasoning = effective_reasoning(model, reasoning_selection, output_limit=output_limit)
    parameters.update(reasoning_parameters)
    if parameters.get("thinking", {}).get("type") == "enabled":
        parameters.pop("temperature", None)
    return parameters, {"profile": profile, "outputField": output_field, "outputLimit": output_limit,
                        "reasoning": reasoning,
                        "inputLimit": input_limit, "temperature": parameters.get("temperature",
                            parameters.get("options", {}).get("temperature")),
                        "temperatureStatus": "provider_default" if profile == "openai_reasoning" else
                                             "default_sent" if temperature is None else "configured_sent",
                        "upstreamAcceptanceVerified": False}


def probe_chat_payload(model: AIModel, prompt: str = "ping") -> dict:
    parameters, _ = chat_parameters(model)
    return {"model": model.model_name, "messages": [{"role": "user", "content": prompt}],
            "stream": False, **parameters}

def _is_ollama(model: AIModel) -> bool:
    return model.provider.strip().lower() in {"ollama", "10"}


def _openai_url(endpoint: str) -> str:
    cleaned = endpoint.rstrip("/")
    return cleaned if cleaned.endswith("/chat/completions") else f"{cleaned}/chat/completions"


def _text_from_content(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, dict):
                parts.append(str(part.get("text") or part.get("content") or ""))
        return "".join(parts)
    return ""



REASONING_PREFIXES = (
    "Here's a thinking process",
    "Here is a thinking process",
    "Let's think",
    "Let me think",
    "**Analyze",
    "Analyze User Input",
    "<think>",
    "<thinking>",
)


def looks_like_reasoning(text: str) -> bool:
    sample = (text or "").lstrip()
    return bool(sample) and sample.startswith(REASONING_PREFIXES)


def _looks_like_reasoning(text: str) -> bool:
    return looks_like_reasoning(text)


def _maybe_reasoning_prefix(text: str) -> bool:
    sample = (text or "").lstrip()
    if not sample:
        return True
    if looks_like_reasoning(sample):
        return True
    return any(prefix.startswith(sample) for prefix in REASONING_PREFIXES)


def extract_visible_answer(text: str, user_message: str = "") -> str:
    raw = text or ""
    visible, nested = _split_think_content(raw)
    candidate = (visible or "").strip()
    if not looks_like_reasoning(raw) and not nested:
        return candidate
    user = (user_message or "").strip()
    chunks: list[str] = []
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith(("**", "#", "-", "*", "Here's", "Here is", "Let's", "Let me", "Analyze", "The user", "The request", "I need", "I should", "The assistant")):
            continue
        if user and (stripped == user or stripped.endswith(user)):
            continue
        chinese = sum(1 for ch in stripped if "\u4e00" <= ch <= "\u9fff")
        if chinese >= 2 and chinese * 2 >= max(1, len(stripped) - stripped.count(" ")):
            chunks.append(stripped)
    if chunks:
        return chunks[-1] if len(chunks[-1]) <= 80 else "\n".join(chunks).strip()
    if looks_like_reasoning(raw) or nested:
        return ""
    return candidate


class VisibleStreamGate:
    def __init__(self, user_message: str = "") -> None:
        self.user_message = user_message or ""
        self.held = ""
        self.mode = "unknown"
        self.emitted = ""

    def push(self, chunk: str) -> list[str]:
        if not chunk:
            return []
        if self.mode == "visible":
            return [chunk]
        self.held += chunk
        sample = self.held.lstrip()
        if self.mode == "unknown":
            if looks_like_reasoning(sample):
                self.mode = "reasoning"
            elif not _maybe_reasoning_prefix(sample) or len(sample) >= 96:
                self.mode = "visible"
                out = self.held
                self.held = ""
                return [out] if out else []
            else:
                return []
        if self.mode == "reasoning":
            visible = extract_visible_answer(self.held, self.user_message)
            if not visible:
                return []
            if visible.startswith(self.emitted):
                delta = visible[len(self.emitted):]
            else:
                delta = visible
            self.emitted = visible
            return [delta] if delta else []
        return []

    def flush(self) -> list[str]:
        if self.mode == "visible":
            out = self.held
            self.held = ""
            return [out] if out else []
        visible = extract_visible_answer(self.held, self.user_message) if self.held else ""
        if self.mode == "reasoning" or looks_like_reasoning(self.held):
            if visible.startswith(self.emitted):
                delta = visible[len(self.emitted):]
            else:
                delta = visible if visible != self.emitted else ""
            self.emitted = visible
            self.held = ""
            return [delta] if delta else []
        out = self.held
        self.held = ""
        return [out] if out else []


def _split_think_content(text: str) -> tuple[str, str]:
    raw = text or ""
    reasoning_parts: list[str] = []
    visible = raw
    for open_tag, close_tag in (("<think>", "</think>"), ("<thinking>", "</thinking>")):
        while open_tag in visible:
            start = visible.find(open_tag)
            end = visible.find(close_tag, start)
            if end < 0:
                reasoning_parts.append(visible[start + len(open_tag):])
                visible = visible[:start]
                break
            reasoning_parts.append(visible[start + len(open_tag):end])
            visible = visible[:start] + visible[end + len(close_tag):]
    return visible, "".join(reasoning_parts)


@observe_model_stream
async def stream_chat_completion(
    model: AIModel,
    user_message: str,
    *,
    messages: list[dict[str, Any]] | None = None,
    client: httpx.AsyncClient | None = None,
    reasoning_sink: list[str] | None = None,
    temperature: float | None = None,
    tools: list[dict[str, object]] | None = None,
    tool_choice: object | None = None,
    tool_call_sink: list[dict[str, object]] | None = None,
    usage_sink: dict[str, Any] | None = None,
    response_schema: dict[str, Any] | None = None,
    structured_sink: dict[str, Any] | None = None,
    model_tools: list[Any] | None = None,
    reasoning_selection: dict | None = None,
    protocol_continuation: dict | None = None,
    native_search: bool = False,
    citation_sink: list[dict] | None = None,
) -> AsyncIterator[str]:
    """Yield normalized text chunks without storing or logging model credentials."""
    if model.provider.strip().lower() in {
        'openai_compatible', 'openai', '1', 'azure', '2', 'zhipu', '3',
        'dashscope', '12', 'ollama', '10', 'openai_responses', 'anthropic',
    }:
        from eic.ai.langchain_stream import stream_chat_completion_langchain
        source = stream_chat_completion_langchain.__wrapped__(
            model, user_message, messages=messages, client=client,
            reasoning_sink=reasoning_sink, temperature=temperature, tools=tools,
            tool_choice=tool_choice, tool_call_sink=tool_call_sink, usage_sink=usage_sink,
            compatible_terminal=True, response_schema=response_schema,
            structured_sink=structured_sink, model_tools=model_tools, reasoning_selection=reasoning_selection,
            protocol_continuation=protocol_continuation, native_search=native_search, citation_sink=citation_sink)
        try:
            async for chunk in source:
                yield chunk
        finally:
            await source.aclose()
        return
    reason = "unsupported_structured_response_provider" if response_schema is not None else "unsupported_chat_provider"
    raise ModelProviderError("模型供应商不支持本次聊天调用。", reason=reason, retryable=False)


async def probe_model_connection(model: AIModel, secret: str, *, client: httpx.AsyncClient | None = None,
                                 prompt: str = "ping", timeout_seconds: int = 180) -> dict:
    """保留服务层已授权的探测入口，凭据仅作为本次调用覆盖值。"""
    from eic.ai.langchain_probe import probe_model_connection_langchain
    return await probe_model_connection_langchain(model, secret, client=client,
        prompt=prompt, timeout_seconds=timeout_seconds)
