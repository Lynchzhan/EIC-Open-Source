"""旧接口 ID 兼容只在此归一化；业务内部使用整数 ID。"""
from typing import Any
from fastapi import HTTPException

def first_int(*values: Any) -> int | None:
    for value in values:
        item_id = coerce_id(value)
        if item_id not in (None, 0):
            return item_id
    return None


def coerce_id(value: Any) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, dict):
        return coerce_id(value.get("id") or value.get("fileId") or value.get("Id") or value.get("data"))
    if isinstance(value, (list, tuple)):
        return coerce_id(value[0] if value else None)
    text_value = str(value).strip()
    if text_value.lower() in {"none", "null", "[object object]"}:
        return None
    if text_value.endswith(".0") and text_value[:-2].lstrip("-").isdigit():
        text_value = text_value[:-2]
    try:
        return int(text_value)
    except (TypeError, ValueError):
        return None


def require_id(value: Any, message: str = "文件 ID 无效") -> int:
    item_id = coerce_id(value)
    if item_id is None:
        raise HTTPException(status_code=400, detail={"code": 400, "errMsg": message})
    return item_id
