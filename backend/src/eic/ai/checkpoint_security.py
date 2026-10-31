"""Checkpoint read authorization and typed payload secret filtering."""
from __future__ import annotations

import copy
import re
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass, replace
from typing import Any, Iterator

from langchain_core.messages.base import BaseMessage
from langgraph.types import Interrupt, Send


_SECRET_KEYS = frozenset({
    "apikey", "accesstoken", "refreshtoken", "idtoken", "authtoken",
    "authorization", "proxyauthorization", "password", "passwd", "secret",
    "secretkey", "modelkey", "clientsecret", "clientpassword", "privatekey",
    "credential", "credentials",
    "sessioncookie", "setcookie",
})
_KEY_SEPARATORS = re.compile(r"[^a-z0-9]+")

# 显式列出标准消息业务载荷；子类新增的声明字段也必须经过同一过滤。
_MESSAGE_PAYLOAD_FIELDS = (
    "content", "additional_kwargs", "response_metadata", "tool_calls",
    "invalid_tool_calls", "tool_call_chunks", "artifact", "usage_metadata",
)


@dataclass(frozen=True, slots=True)
class CheckpointReadScope:
    """Identity resolved by a trusted service for one checkpoint read operation."""

    run_id: str
    tenant_id: int
    owner_id: int
    session_id: int | None
    request_id: str | None
    graph_version: str


def _is_secret_key(key: Any) -> bool:
    if not isinstance(key, str):
        return False
    normalized = _KEY_SEPARATORS.sub("", key.casefold())
    return normalized in _SECRET_KEYS


def is_credential_channel(channel: Any) -> bool:
    """Credential names are not persistable business channels; graph control names remain intact."""
    return _is_secret_key(channel)


_CLEANING = object()


def _clean_copied(value: Any, memo: dict[int, tuple[Any, Any]]) -> Any:
    identity = id(value)
    if identity in memo:
        result = memo[identity][1]
        if result is _CLEANING:
            raise TypeError("checkpoint_cyclic_payload")
        return result
    # 只发布完整的清理结果；回边不得取得尚未清理完的副本。
    # 同时保留输入副本，避免重建对象释放后其 id 在本次遍历中被复用。
    memo[identity] = (value, _CLEANING)
    if isinstance(value, dict):
        for key in list(value):
            if _is_secret_key(key):
                del value[key]
            else:
                value[key] = _clean_copied(value[key], memo)
        result = value
    elif isinstance(value, list):
        for index, item in enumerate(value):
            value[index] = _clean_copied(item, memo)
        result = value
    elif isinstance(value, tuple):
        items = tuple(_clean_copied(item, memo) for item in value)
        if hasattr(value, "_fields"):
            result = type(value)(*items)
        elif type(value) is tuple:
            result = items
        else:
            raise TypeError("checkpoint_unsupported_tuple_type")
    elif isinstance(value, set):
        if type(value) is not set:
            raise TypeError("checkpoint_unsupported_set_type")
        result = {_clean_copied(item, memo) for item in value}
    elif isinstance(value, BaseMessage):
        # 入口已深拷贝；原位清理该副本，保留子类、共享引用和消息关联。
        fields = dict.fromkeys((*_MESSAGE_PAYLOAD_FIELDS, *type(value).model_fields))
        for name in fields:
            if hasattr(value, name):
                if _is_secret_key(name):
                    # 标准消息没有此类声明字段；未知扩展不能借类型豁免携密钥。
                    raise TypeError("checkpoint_credential_message_field")
                setattr(value, name, _clean_copied(getattr(value, name), memo))
        extras = value.model_extra
        if extras is not None:
            _clean_copied(extras, memo)
        result = value
    elif isinstance(value, Interrupt):
        result = replace(value, value=_clean_copied(value.value, memo))
    elif isinstance(value, Send):
        result = Send(value.node, _clean_copied(value.arg, memo))
    elif hasattr(value, "__dict__"):
        raise TypeError("checkpoint_unknown_capability_type")
    elif value is None or isinstance(value, (str, int, float, bool, bytes)):
        result = value
    else:
        raise TypeError("checkpoint_unsupported_value_type")
    memo[identity] = (value, result)
    return result


def sanitize_checkpoint_payload(value: Any) -> Any:
    """Deep-copy and scrub known credential fields without stringifying typed values."""
    return _clean_copied(copy.deepcopy(value), {})


def sanitize_tool_result(value: Any) -> Any:
    """序列化前过滤结构化工具输出；不解析或删改普通字符串正文。"""
    return sanitize_checkpoint_payload(value)


def sanitize_checkpoint(checkpoint: dict[str, Any]) -> dict[str, Any]:
    """Preserve the LangGraph envelope and clean only business channels."""
    if not isinstance(checkpoint, dict):
        raise TypeError("checkpoint_mapping_required")
    result = copy.deepcopy(checkpoint)
    channels = result.get("channel_values")
    if channels is not None:
        if not isinstance(channels, dict):
            raise TypeError("checkpoint_channel_values_mapping_required")
        result["channel_values"] = _clean_copied(channels, {})
    return result


class CheckpointReadScopeBinding:
    """Task-local scope slot; reset reliably after nested or concurrent operations."""

    def __init__(self) -> None:
        self._scope: ContextVar[CheckpointReadScope | None] = ContextVar(
            f"eic_checkpoint_read_scope_{id(self)}", default=None
        )

    @property
    def current(self) -> CheckpointReadScope | None:
        return self._scope.get()

    @contextmanager
    def bind(self, scope: CheckpointReadScope) -> Iterator[None]:
        if not isinstance(scope, CheckpointReadScope):
            raise TypeError("checkpoint_read_scope_required")
        token: Token[CheckpointReadScope | None] = self._scope.set(scope)
        try:
            yield
        finally:
            self._scope.reset(token)
