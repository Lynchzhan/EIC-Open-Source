"""显式应用结构化响应契约；仅接受不触发外部解析的内联JSON Schema。"""
from __future__ import annotations

import copy
import json
import math
import re
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, ValidationError
from langchain_core.utils.function_calling import convert_to_openai_function


class ResponseContractError(ValueError):
    """只携带固定服务错误码，不回显业务正文或Schema内容。"""


def validate_response_schema(value: dict[str, Any] | None) -> dict[str, Any] | None:
    """保存与运行共用；None禁用，其他值必须是命名的封闭对象Schema。"""
    if value is None:
        return None
    try:
        # 固定大小上限仅限制新增配置，不改变模型调用预算。
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False)
        if len(encoded.encode("utf-8")) > 32768:
            raise ResponseContractError("invalid_response_schema")
        schema = json.loads(encoded)
        if (not isinstance(schema, dict) or schema.get("type") != "object"
                or not isinstance(schema.get("title"), str)
                or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]{0,63}", schema["title"])):
            raise ResponseContractError("invalid_response_schema")
        _validate_inline_schema(schema)
        Draft202012Validator.check_schema(schema)
        return copy.deepcopy(schema)
    except ResponseContractError:
        raise
    except (TypeError, ValueError, RecursionError, SchemaError):
        raise ResponseContractError("invalid_response_schema") from None


def _validate_inline_schema(schema: Any) -> None:
    # 采用明确的provider通用子集，不解析$ref/$id/$schema或未知扩展。
    if not isinstance(schema, dict):
        raise ResponseContractError("invalid_response_schema")
    allowed = {"type", "title", "description", "properties", "required",
               "additionalProperties", "items", "enum", "anyOf"}
    if set(schema) - allowed:
        raise ResponseContractError("unsupported_response_schema")
    kind = schema.get("type")
    if kind is not None and kind not in {
        "object", "array", "string", "number", "integer", "boolean", "null"
    }:
        raise ResponseContractError("unsupported_response_schema")
    if kind == "object":
        properties = schema.get("properties")
        required = schema.get("required")
        if (not isinstance(properties, dict) or not isinstance(required, list)
                or any(not isinstance(key, str) for key in required)
                or len(set(required)) != len(required)
                or set(required) != set(properties)
                or schema.get("additionalProperties") is not False):
            raise ResponseContractError("invalid_response_schema")
        for child in properties.values():
            _validate_inline_schema(child)
    elif "properties" in schema or "required" in schema or "additionalProperties" in schema:
        raise ResponseContractError("invalid_response_schema")
    if kind == "array":
        _validate_inline_schema(schema.get("items"))
    elif "items" in schema:
        raise ResponseContractError("invalid_response_schema")
    if "anyOf" in schema:
        if not isinstance(schema["anyOf"], list) or not schema["anyOf"]:
            raise ResponseContractError("invalid_response_schema")
        for child in schema["anyOf"]:
            _validate_inline_schema(child)
    if kind is None and "anyOf" not in schema:
        raise ResponseContractError("invalid_response_schema")


def response_format(schema: dict[str, Any]) -> dict[str, Any]:
    """通过LangChain公开转换接口构造ChatOpenAI标准json_schema绑定。"""
    function = convert_to_openai_function(copy.deepcopy(schema), strict=True)
    return {"type": "json_schema", "json_schema": {
        "name": function["name"], "strict": True, "schema": function["parameters"],
    }}


def parse_response(text: str, schema: dict[str, Any]) -> dict[str, Any]:
    """只验证完整JSON，不修补截断JSON、剥围栏或自动重发。"""
    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate_json_key")
            result[key] = value
        return result

    def reject_constant(value):
        raise ValueError("non_finite_json_number")

    def finite_float(value):
        number = float(value)
        if not math.isfinite(number):
            raise ValueError("non_finite_json_number")
        return number

    try:
        value = json.loads(text, object_pairs_hook=unique_object,
                           parse_constant=reject_constant, parse_float=finite_float)
        Draft202012Validator(schema).validate(value)
        if not isinstance(value, dict):
            raise ValueError("not_object")
        return value
    except (ValueError, TypeError, RecursionError, ValidationError):
        raise ResponseContractError("invalid_structured_response") from None
