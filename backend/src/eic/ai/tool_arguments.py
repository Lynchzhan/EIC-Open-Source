"""工具参数的本地文本边界；不替代 Skill 对象预检或执行授权。"""

from __future__ import annotations

import json
from typing import Any

MAX_TOOL_ARGUMENT_BYTES = 256 * 1024
MAX_TOOL_ROUND_BYTES = 1024 * 1024
MAX_TOOL_CALLS = 64


class ToolArgumentsError(ValueError):
    code = "invalid_tool_arguments"
    retryable = False


def tool_argument_text_size(text: str) -> int:
    """先限字符数，再逐字符计量，避免为巨大输入构造编码副本。"""
    if not isinstance(text, str) or len(text) > MAX_TOOL_ARGUMENT_BYTES:
        raise ToolArgumentsError("工具参数文本超过本地限制")
    size = 0
    for char in text:
        code = ord(char)
        if 0xD800 <= code <= 0xDFFF:
            raise ToolArgumentsError("工具参数文本包含无效 Unicode")
        size += 1 if code < 128 else 2 if code < 2048 else 3 if code < 65536 else 4
        if size > MAX_TOOL_ARGUMENT_BYTES:
            raise ToolArgumentsError("工具参数文本超过本地限制")
    return size


def parse_tool_arguments(text: str) -> dict[str, Any]:
    """在 json.loads 前限定 UTF-8 字节、根0深度32及含键节点16384。"""
    tool_argument_text_size(text)
    stack: list[str] = []
    nodes = 0
    index = 0
    while index < len(text):
        char = text[index]
        if char in " \t\r\n:,":
            index += 1
            continue
        if char in "}]":
            if not stack or stack.pop() != ("{" if char == "}" else "["):
                raise ToolArgumentsError("工具参数 JSON 格式无效")
            index += 1
            continue
        depth = len(stack)
        nodes += 1
        if nodes > 16384:
            raise ToolArgumentsError("工具参数节点超过本地限制")
        if char == '"':
            index += 1
            while index < len(text):
                if text[index] == "\\":
                    index += 2
                elif text[index] == '"':
                    index += 1
                    break
                else:
                    index += 1
            else:
                raise ToolArgumentsError("工具参数 JSON 字符串未闭合")
            lookahead = index
            while lookahead < len(text) and text[lookahead] in " \t\r\n":
                lookahead += 1
            # 对象键计节点，但与 _ValueBudget 一致不增加值深度。
            if lookahead < len(text) and text[lookahead] == ":":
                depth -= 1
        elif char in "{[":
            stack.append(char)
            index += 1
        else:
            while index < len(text) and text[index] not in " \t\r\n,]}:":
                index += 1
        if depth > 32:
            raise ToolArgumentsError("工具参数深度超过本地限制")
    if stack:
        raise ToolArgumentsError("工具参数 JSON 容器未闭合")

    def reject_constant(value: str) -> None:
        raise ToolArgumentsError("工具参数不接受非有限数值")

    try:
        arguments = json.loads(text, parse_constant=reject_constant)
    except (ValueError, RecursionError, OverflowError):
        raise ToolArgumentsError("工具参数 JSON 无效或超过解析限制") from None
    if not isinstance(arguments, dict):
        raise ToolArgumentsError("工具参数必须是 JSON object")
    return arguments


def validate_message_tool_arguments(message: dict[str, Any]) -> None:
    """在消息预算序列化前预检工具参数；不执行 SDK 转换或复制参数。"""
    from eic.ai.declarative_skills import validate_skill_arguments
    from eic.ai.tool_center import ToolExecutionError

    raw_calls = message.get("tool_calls")
    if raw_calls is None:
        return
    if not isinstance(raw_calls, list) or len(raw_calls) > MAX_TOOL_CALLS:
        raise ToolArgumentsError("历史工具调用格式或数量无效")
    total = 0
    for call in raw_calls:
        if not isinstance(call, dict) or not isinstance(call.get("function"), dict):
            raise ToolArgumentsError("历史工具调用格式无效")
        arguments = call["function"].get("arguments", {})
        if isinstance(arguments, str):
            total += tool_argument_text_size(arguments)
            arguments = parse_tool_arguments(arguments)
        try:
            validate_skill_arguments(arguments)
        except ToolExecutionError:
            raise ToolArgumentsError("历史工具参数对象无效或超过本地限制") from None
        if not isinstance(call["function"].get("arguments", {}), str):
            total += tool_argument_text_size(json.dumps(arguments, ensure_ascii=False, separators=(",", ":")))
        if total > MAX_TOOL_ROUND_BYTES:
            raise ToolArgumentsError("历史工具参数累计超过本地限制")
