"""确定性保守预算；不是上游 tokenizer 的精确计数。"""
from __future__ import annotations

import json
from typing import Any

from eic.ai.tool_arguments import ToolArgumentsError, validate_message_tool_arguments


class ChatConfigurationError(RuntimeError):
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


def text_units(text: str) -> int:
    # 普通 UTF-8 BPE 的保守估算，另为角色/分隔符预留开销。
    return len(text.encode("utf-8"))


def message_units(
    messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None,
) -> int:
    """工具协议字段和 Schema 同样占输入预算；content 可为 null。"""
    total = 16
    for item in messages:
        try:
            validate_message_tool_arguments(item)
        except ToolArgumentsError:
            raise ChatConfigurationError("invalid_model_messages") from None
        total += 16 + text_units(str(item.get("role") or ""))
        content = item.get("content")
        total += text_units(content if isinstance(content, str) else (
            json.dumps(content, ensure_ascii=False, separators=(",", ":"))
            if content is not None else ""
        ))
        extra = {key: value for key, value in item.items() if key not in {"role", "content"}}
        if extra:
            total += text_units(json.dumps(extra, ensure_ascii=False, separators=(",", ":")))
    if tools:
        total += text_units(json.dumps(tools, ensure_ascii=False, separators=(",", ":")))
    return total


def positive_budget(value: object, default: int) -> int:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 2_000_000:
        raise ChatConfigurationError("invalid_token_budget")
    return value


def app_temperature(config: dict) -> float | None:
    if "temperature" not in config:
        return None
    value = config["temperature"]
    if isinstance(value, bool) or not isinstance(value, (float, int)) or not 0 <= value <= 100:
        raise ChatConfigurationError("invalid_temperature")
    return value / 50.0


def budget_conversation(
    system: list[dict[str, str]], history: list[dict[str, str]], question: str, limit: int,
) -> tuple[list[dict[str, str]], dict]:
    """只删除最旧完整轮次；系统、证据及本轮问题超限则明确拒绝。"""
    current = {"role": "user", "content": question}
    fixed = [*system, current]
    if message_units(fixed) > limit:
        raise ChatConfigurationError("input_budget_exceeded")
    turns: list[list[dict[str, str]]] = []
    for item in history:
        if item["role"] == "user":
            turns.append([item])
        elif item["role"] == "assistant" and turns:
            turns[-1].append(item)
    turns = [turn for turn in turns if len(turn) >= 2 and turn[-1]["role"] == "assistant"]
    selected = [item for turn in turns for item in turn]
    dropped = len(history) - len(selected)
    while turns and message_units([*system, *selected, current]) > limit:
        dropped += len(turns.pop(0))
        selected = [item for turn in turns for item in turn]
    messages = [*system, *selected, current]
    return messages, {"inputEstimate": message_units(messages), "inputLimit": limit,
                      "droppedHistoryMessages": dropped, "estimator": "utf8_bytes_plus_framing",
                      "exactTokenizer": False}
