"""受控确定性技能，只处理调用输入，不运行上传代码或访问外部资源。"""
from __future__ import annotations

import csv
import io
import json
import math
import re
from decimal import Decimal, InvalidOperation, localcontext
from importlib import resources
from pathlib import Path
from typing import Any

from eic.ai.skill_manager import SkillManager
from eic.ai.tool_center import ToolCenter, ToolDefinition, ToolExecutionError

SOURCE_ROOT = Path(__file__).resolve().parents[3]
ROOT = SOURCE_ROOT / "skills"
VERSION = "1.0.0"
CATALOG = {
    "text-statistics": {"title": "文本统计", "tool": "text_statistics", "example": {"text": "Hello EIC\n技能执行"}},
    "json-format": {"title": "JSON 校验与格式化", "tool": "json_format", "example": {"text": '{"b":2,"a":1}'}},
    "csv-summary": {"title": "CSV 数值汇总", "tool": "csv_summary", "example": {"text": "name,amount\na,10\nb,20\n", "column": "amount"}},
}


def invalid(message):
    raise ToolExecutionError("invalid_tool_arguments", message)


def text_statistics(args):
    text = args["text"]
    return {"characters": len(text), "charactersWithoutWhitespace": len(re.sub(r"\s", "", text)),
            "lines": len(text.splitlines()), "words": len(re.findall(r"\b[\w]+\b", text)),
            "utf8Bytes": len(text.encode("utf-8"))}


def json_format(args):
    def reject_constant(value):
        invalid("JSON 不接受非有限数值")

    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                invalid("JSON 包含重复字段")
            result[key] = value
        return result

    try:
        value = json.loads(args["text"], parse_constant=reject_constant, object_pairs_hook=unique_object)
        pending = [value]
        while pending:
            item = pending.pop()
            if isinstance(item, float) and not math.isfinite(item):
                invalid("JSON 不接受非有限数值")
            if isinstance(item, dict):
                pending.extend(item.values())
            elif isinstance(item, list):
                pending.extend(item)
        formatted = json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
    except (ValueError, RecursionError, OverflowError) as exc:
        invalid("JSON 格式无效或嵌套过深")
    return {"valid": True, "rootType": type(value).__name__, "formatted": formatted}


def csv_summary(args):
    reader = csv.DictReader(io.StringIO(args["text"], newline=""), strict=True)
    try:
        columns = reader.fieldnames or []
        if len(columns) != len(set(columns)) or args["column"] not in columns:
            invalid("CSV 字段重复或所选列不存在")
        values = []
        for row in reader:
            if len(values) >= 2000:
                invalid("CSV 超过 2000 行限制")
            if None in row or any(value is None for value in row.values()):
                invalid("CSV 行与表头列数不一致")
            raw = row[args["column"]].strip()
            if len(raw) > 64:
                invalid("CSV 数值长度超限")
            number = Decimal(raw)
            if not number.is_finite() or number.copy_abs() > Decimal("1e18") or number.as_tuple().exponent < -18:
                invalid("CSV 需要有限且精度受控的数值")
            values.append(number)
    except (csv.Error, InvalidOperation, ValueError):
        invalid("CSV 格式无效或所选列包含非数值")
    if not values:
        invalid("CSV 没有可汇总的数据行")
    with localcontext() as context:
        context.prec = 64
        total = sum(values, Decimal(0))
        return {"column": args["column"], "count": len(values), "sum": str(total),
                "mean": str(total / len(values)), "min": str(min(values)), "max": str(max(values))}


def register_builtin_tools(center: ToolCenter):
    for key, handler in (("text-statistics", text_statistics), ("json-format", json_format), ("csv-summary", csv_summary)):
        fields = {"text": {"type": "string", "maxLength": 32000}}
        if key == "csv-summary":
            fields["column"] = {"type": "string", "minLength": 1, "maxLength": 128}
        center.register(ToolDefinition(name=CATALOG[key]["tool"], description=CATALOG[key]["title"], handler=handler,
            input_schema={"type": "object", "required": list(fields), "properties": fields, "additionalProperties": False},
            max_output_length=64000, timeout_ms=5000))


def builtin_data(key: str) -> dict[str, Any]:
    spec = CATALOG[key]
    packaged = resources.files("eic").joinpath("skills", key, "SKILL.md")
    if packaged.is_file():
        with resources.as_file(packaged) as path:
            manifest = SkillManager._parse(path)
    elif (SOURCE_ROOT / "src/eic/ai/builtin_skills.py").resolve() == Path(__file__).resolve():
        # 源码开发只读取自身 backend/skills；安装版本仅使用随包发布的声明。
        manifest = SkillManager._parse(SOURCE_ROOT / "skills" / key / "SKILL.md")
    else:
        raise FileNotFoundError("安装包缺少内置技能声明")
    if (manifest.name != key or manifest.allowed_tools != (spec["tool"],) or manifest.version != VERSION
            or manifest.permissions != ("skill.read", "tool.execute") or not manifest.enabled):
        raise ValueError("内置技能声明与执行器不一致")
    return {"builtinKey": key, "builtinVersion": VERSION, "skillToolType": 2, "isSystem": True,
            "description": manifest.description, "version": VERSION, "enabled": True, "activeStatus": 1,
            "allowedTools": [spec["tool"]], "permissions": ["skill.read", "tool.execute"],
            "contentHash": manifest.content_hash, "executionMode": "deterministic", "classMethod": spec["tool"]}


def resource_enabled(data):
    return data.get("enabled", True) is True and data.get("activeStatus", 1) in (1, "1")


def trusted_builtin(resource):
    data = resource.data or {}
    key = data.get("builtinKey")
    if not isinstance(key, str) or key not in CATALOG:
        return None
    expected = builtin_data(key)
    # 展示名称不是信任依据；调用绑定服务器声明、版本、哈希和固定工具。
    if all(data.get(field) == expected[field] for field in
           ("builtinVersion", "contentHash", "allowedTools", "permissions", "skillToolType", "executionMode")):
        return key
    return None
