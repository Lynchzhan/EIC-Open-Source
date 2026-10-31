"""受限声明式技能：上传内容只描述数据流，执行始终经过可信服务工具集。"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import io
import json
import re
import stat
import time
import zipfile
import zlib
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import yaml
from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

from eic.ai.tool_center import ToolCenter, ToolExecutionError, sanitize_tool_output

if TYPE_CHECKING:
    from eic.ai.standard_tools import ControlledToolSet

MAX_DOCUMENT = 65536
MAX_VALUE = 262144
IDENTIFIER = re.compile(r"[a-zA-Z][a-zA-Z0-9_-]{0,63}\Z")


class _UniqueLoader(yaml.SafeLoader):
    pass


def _mapping(loader, node):
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node)
        if not isinstance(key, str) or key in result:
            raise ValueError("技能 YAML 字段必须是唯一字符串")
        result[key] = loader.construct_object(value_node)
    return result


_UniqueLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _mapping)


def _yaml(raw: str) -> dict[str, Any]:
    if not isinstance(raw, str) or len(raw.encode("utf-8")) > MAX_DOCUMENT:
        raise ValueError("技能文档大小超限")
    try:
        depth = 0
        for token in yaml.scan(raw):
            if isinstance(token, (yaml.tokens.AliasToken, yaml.tokens.AnchorToken, yaml.tokens.TagToken)):
                raise ValueError("技能不接受 YAML 别名、锚点或自定义标签")
            if isinstance(token, (yaml.tokens.BlockMappingStartToken, yaml.tokens.BlockSequenceStartToken,
                                  yaml.tokens.FlowMappingStartToken, yaml.tokens.FlowSequenceStartToken)):
                depth += 1
                if depth > 24:
                    raise ValueError("技能 YAML 嵌套超限")
            elif isinstance(token, (yaml.tokens.BlockEndToken, yaml.tokens.FlowMappingEndToken,
                                    yaml.tokens.FlowSequenceEndToken)):
                depth -= 1
        data = yaml.load(raw, Loader=_UniqueLoader)
        if not isinstance(data, dict):
            raise ValueError("技能声明必须是对象")
        _json(data)
        return data
    except (yaml.YAMLError, RecursionError, TypeError, OverflowError) as exc:
        raise ValueError("技能 YAML 格式无效") from exc


def _json(value: Any) -> str:
    result = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    if len(result.encode("utf-8")) > MAX_VALUE:
        raise ValueError("技能数据大小超限")
    return result


def _schema(schema: Any, depth: int = 0) -> None:
    # 刻意只开放本地、无引用的常用 Schema 子集。
    allowed = {"type", "properties", "required", "additionalProperties", "items", "enum",
               "minLength", "maxLength", "minimum", "maximum", "minItems", "maxItems", "description"}
    if not isinstance(schema, dict) or depth > 16 or set(schema) - allowed:
        raise ValueError("技能 input_schema 超出受限 Schema 子集")
    if not isinstance(schema.get("type"), str) or schema["type"] not in {
        "object", "array", "string", "integer", "number", "boolean", "null",
    }:
        raise ValueError("技能 Schema 必须声明单一类型")
    if schema["type"] != "object" and set(schema) & {"properties", "required", "additionalProperties"}:
        raise ValueError("对象 Schema 关键字只能用于对象")
    if "items" in schema and schema["type"] != "array":
        raise ValueError("items 只能用于数组")
    if schema["type"] == "object":
        if schema.get("additionalProperties") is not False or not isinstance(schema.get("properties", {}), dict):
            raise ValueError("技能对象 Schema 必须关闭额外字段")
        for child in schema.get("properties", {}).values():
            _schema(child, depth + 1)
    if "items" in schema:
        _schema(schema["items"], depth + 1)
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as exc:
        raise ValueError("技能 input_schema 无效") from exc


def _references(value: Any, previous: set[str], depth: int = 0) -> None:
    if depth > 20:
        raise ValueError("技能参数模板嵌套超限")
    if isinstance(value, dict):
        if "$ref" in value:
            if set(value) != {"$ref"} or not isinstance(value["$ref"], str):
                raise ValueError("技能引用必须是独立的 $ref 对象")
            parts = value["$ref"].split("/")
            if parts[0] == "input":
                return
            if len(parts) < 2 or parts[0] != "steps" or parts[1] not in previous:
                raise ValueError("技能只能引用输入或已完成步骤")
        else:
            for child in value.values():
                _references(child, previous, depth + 1)
    elif isinstance(value, list):
        for child in value:
            _references(child, previous, depth + 1)


@dataclass(frozen=True)
class SkillPackage:
    document: str
    manifest: str | None
    canonical: str

    @property
    def data(self) -> dict[str, Any]:
        return json.loads(self.canonical)

    @property
    def content_hash(self) -> str:
        return hashlib.sha256(self.canonical.encode("utf-8")).hexdigest()

    def resource_data(self) -> dict[str, Any]:
        data = self.data
        return {"executionMode": "declarative-v1", "skillToolType": 2,
                "name": data["name"], "description": data["description"], "version": data["version"],
                "enabled": data["enabled"], "activeStatus": int(data["enabled"]),
                "allowedTools": data["allowed_tools"], "permissions": data["permissions"],
                "contentHash": self.content_hash, "skillDocument": self.document,
                "skillManifest": self.manifest}


def parse_skill_document(raw: str, manifest: str | None = None) -> SkillPackage:
    if not isinstance(raw, str) or len(raw.encode("utf-8")) > MAX_DOCUMENT:
        raise ValueError("SKILL.md 大小超限")
    normalized = raw.replace("\r\n", "\n")
    lines = normalized.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        raise ValueError("SKILL.md 必须以 YAML front matter 开始")
    end = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
    if end is None:
        raise ValueError("SKILL.md front matter 未闭合")
    data = _yaml("".join(lines[1:end]))
    if manifest is not None:
        extra = _yaml(manifest)
        if set(extra) - {"steps", "input_schema"} or set(extra) & set(data):
            raise ValueError("manifest.yaml 只能补充未声明的 steps/input_schema")
        data.update(extra)
    if set(data) - {"name", "description", "version", "enabled", "allowed_tools", "permissions",
                    "steps", "input_schema"}:
        raise ValueError("技能声明含不支持字段，禁止代码、shell、凭据或权限扩展")
    if not isinstance(data.get("name"), str) or not IDENTIFIER.fullmatch(data["name"]):
        raise ValueError("技能名称无效")
    for key, default, limit in (("description", "", 2000), ("version", "0.0.0", 64)):
        data.setdefault(key, default)
        if not isinstance(data[key], str) or len(data[key]) > limit:
            raise ValueError(f"技能 {key} 无效")
    data.setdefault("enabled", True)
    if type(data["enabled"]) is not bool:
        raise ValueError("技能 enabled 必须是布尔值")
    for key in ("allowed_tools", "permissions"):
        data.setdefault(key, [])
        values = data[key]
        if (not isinstance(values, list) or len(values) > 32
                or any(not isinstance(x, str) or not x or len(x) > 128 or any(c.isspace() for c in x) for x in values)
                or len(values) != len(set(values))):
            raise ValueError(f"技能 {key} 必须是有界且无重复的字符串数组")
    if set(data["permissions"]) - {"skill.read", "tool.execute"}:
        raise ValueError("技能包不能声明额外权限")
    data.setdefault("input_schema", {"type": "object", "properties": {}, "additionalProperties": False})
    _schema(data["input_schema"])
    if data["input_schema"]["type"] != "object":
        raise ValueError("技能输入必须是对象")
    data.setdefault("steps", [])
    if not isinstance(data["steps"], list) or len(data["steps"]) > 32:
        raise ValueError("技能 steps 必须是最多 32 项的数组")
    previous: set[str] = set()
    for step in data["steps"]:
        if not isinstance(step, dict) or set(step) != {"id", "tool", "arguments"}:
            raise ValueError("每步必须且只能声明 id/tool/arguments")
        if not isinstance(step["id"], str) or not IDENTIFIER.fullmatch(step["id"]) or step["id"] in previous:
            raise ValueError("步骤 id 无效或重复")
        if not isinstance(step["tool"], str) or step["tool"] not in data["allowed_tools"]:
            raise ValueError("步骤工具必须列入 allowed_tools")
        if not isinstance(step["arguments"], dict) or "$ref" in step["arguments"]:
            raise ValueError("步骤 arguments 必须为字段模板对象")
        _references(step["arguments"], previous)
        previous.add(step["id"])
    data["instructions"] = "".join(lines[end + 1:]).strip()
    return SkillPackage(raw, manifest, _json(data))


def parse_skill_package(payload: bytes) -> SkillPackage:
    if not isinstance(payload, bytes) or not payload or len(payload) > 131072:
        raise ValueError("声明式技能 ZIP 必须为非空且不超过 128 KiB")
    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            entries = archive.infolist()
            if not 1 <= len(entries) <= 2:
                raise ValueError("技能 ZIP 只接受根 SKILL.md 和可选 manifest.yaml")
            files = {}
            for entry in entries:
                mode = entry.external_attr >> 16
                if (entry.filename not in {"SKILL.md", "manifest.yaml"} or entry.filename in files
                        or entry.is_dir() or (stat.S_IFMT(mode) not in {0, stat.S_IFREG})
                        or entry.flag_bits & 1 or entry.file_size > MAX_DOCUMENT
                        or entry.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}
                        or entry.file_size > max(entry.compress_size, 1) * 100):
                    raise ValueError("技能 ZIP 路径、文件类型、重复条目或压缩预算无效")
                with archive.open(entry) as stream:
                    content = stream.read(MAX_DOCUMENT + 1)
                    if len(content) > MAX_DOCUMENT or stream.read(1):
                        raise ValueError("技能 ZIP 实际解压大小超限")
                files[entry.filename] = content.decode("utf-8-sig")
            if "SKILL.md" not in files:
                raise ValueError("技能 ZIP 缺少根 SKILL.md")
            return parse_skill_document(files["SKILL.md"], files.get("manifest.yaml"))
    except (zipfile.BadZipFile, UnicodeError, EOFError, RuntimeError, NotImplementedError, zlib.error) as exc:
        raise ValueError("声明式技能 ZIP 无效") from exc


def restore_skill_package(data: dict[str, Any]) -> SkillPackage:
    if not isinstance(data, dict) or data.get("executionMode") != "declarative-v1":
        raise ValueError("不是声明式技能资源")
    package = parse_skill_document(data.get("skillDocument"), data.get("skillManifest"))
    expected = package.resource_data()
    if any(data.get(key) != expected[key] for key in
           ("contentHash", "allowedTools", "permissions", "version", "skillToolType")):
        raise ValueError("技能资源声明或哈希不一致")
    if data.get("enabled") is not True or type(data.get("activeStatus")) is not int or data["activeStatus"] != 1:
        raise ValueError("技能资源已禁用")
    return package


class _ValueBudget:
    """在复制或序列化前计量 JSON；重复引用按每次展开后的实际大小计费。"""

    def __init__(self, *, completed: bool = False) -> None:
        self.nodes = 0
        self.size = 0
        self.effect = "unknown" if completed else "none"

    def fail(self) -> None:
        raise ToolExecutionError("skill_value_limit", "技能数据超过深度、节点或 UTF-8 大小限制",
                                 retryable=False, effect_status=self.effect)

    def add(self, size: int) -> None:
        self.size += size
        if self.size > MAX_VALUE:
            self.fail()

    def string(self, value: str) -> None:
        # 不先 encode 整个客户端字符串；计入 JSON 引号与转义字节。
        self.add(2)
        for char in value:
            code = ord(char)
            if 0xD800 <= code <= 0xDFFF:
                self.fail()
            self.add(2 if char in '"\\\b\f\n\r\t' else 6 if code < 32
                     else 1 if code < 128 else 2 if code < 2048 else 3 if code < 65536 else 4)

    def head(self, value: Any, depth: int) -> None:
        self.nodes += 1
        if depth > 32 or self.nodes > 16384:
            self.fail()
        if isinstance(value, (dict, list)):
            if len(value) > 16384 - self.nodes:
                self.fail()
            self.add(2 + max(0, len(value) - 1))
        elif isinstance(value, str):
            self.string(value)
        elif value is None:
            self.add(4)
        elif type(value) is bool:
            self.add(4 if value else 5)
        elif type(value) in (int, float):
            # 限制整数转换工作量，JSON 编码拒绝非有限浮点数。
            if type(value) is int and value.bit_length() > 14000:
                self.fail()
            try:
                self.add(len(json.dumps(value, allow_nan=False)))
            except (ValueError, OverflowError):
                self.fail()
        else:
            self.fail()

    def key(self, key: Any) -> None:
        self.nodes += 1
        if not isinstance(key, str) or self.nodes > 16384:
            self.fail()
        self.string(key)
        self.add(1)

    def check(self, value: Any, depth: int = 0) -> None:
        self.head(value, depth)
        if isinstance(value, dict):
            for key, child in value.items():
                self.key(key)
                self.check(child, depth + 1)
        elif isinstance(value, list):
            for child in value:
                self.check(child, depth + 1)


def validate_skill_arguments(arguments: dict[str, Any]) -> None:
    """可信入口在 Schema 校验及复制前调用；预检不授予任何执行权限。"""
    _ValueBudget().check(arguments)
    if not isinstance(arguments, dict):
        raise ToolExecutionError("invalid_tool_arguments", "技能参数必须是 JSON object", retryable=False)


def _resolve(value: Any, arguments: dict, results: dict,
             budget: _ValueBudget | None = None, depth: int = 0) -> Any:
    if budget is None:
        budget = _ValueBudget(completed=bool(results))
    if isinstance(value, dict):
        if set(value) == {"$ref"}:
            parts = value["$ref"].split("/")
            current = arguments if parts[0] == "input" else results
            path = parts[1:]
            try:
                for part in path:
                    key = part.replace("~1", "/").replace("~0", "~")
                    if isinstance(current, dict):
                        current = current[key]
                    elif isinstance(current, list) and key.isascii() and key.isdigit():
                        current = current[int(key)]
                    else:
                        raise KeyError(key)
                budget.check(current, depth)
                return copy.deepcopy(current)
            except (KeyError, IndexError, ValueError) as exc:
                raise ToolExecutionError("skill_reference_missing", "技能引用的数据不存在",
                                         retryable=False, effect_status=budget.effect) from exc
        budget.head(value, depth)
        output = {}
        for key, child in value.items():
            budget.key(key)
            output[key] = _resolve(child, arguments, results, budget, depth + 1)
        return output
    if isinstance(value, list):
        budget.head(value, depth)
        return [_resolve(child, arguments, results, budget, depth + 1) for child in value]
    budget.head(value, depth)
    return value


def _step_result(result: Any, *, completed: bool) -> dict[str, Any]:
    """兼容管理/图执行封装，但任何显式非成功标记均不得推进下一步。"""
    def check(value: Any) -> None:
        if not isinstance(value, dict):
            raise ToolExecutionError("skill_invalid_tool_result", "技能工具结果必须是对象",
                                     effect_status="unknown")
        status = value.get("status")
        bad_status = "status" in value and status not in ("completed", "success")
        bad_error = value.get("errorCode") not in (None, "")
        bad_flag = "isError" in value and value["isError"] is not False
        if bad_status or bad_error or bad_flag:
            # 已完成前序步骤时整个技能不再是无副作用；未确认失败结果也按未知处理。
            no_effect = (value.get("effectStatus") == "none" or value.get("effect_status") == "none"
                         or status == "approval_required")
            uncertain = (status == "uncertain" or value.get("effectStatus") in ("unknown", "applied")
                         or value.get("effect_status") in ("unknown", "applied"))
            code = "approval_required" if status == "approval_required" else "skill_step_failed"
            raise ToolExecutionError(code, "技能工具未成功完成，已停止后续步骤", retryable=False,
                                     effect_status="none" if no_effect and not completed and not uncertain else "unknown")

    check(result)
    # 仅剥离一层服务返回封装，不递归解释普通业务对象的任意嵌套字段。
    if "result" in result and "resultData" in result:
        raise ToolExecutionError("skill_invalid_tool_result", "技能工具结果封装存在歧义",
                                 effect_status="unknown")
    payload = result.get("result", result.get("resultData", result))
    check(payload)
    return sanitize_tool_output(payload)


class BoundSkill:
    def __init__(self, package: SkillPackage, tools: ControlledToolSet) -> None:
        # 重新解析，避免调用方直接构造 dataclass 绕过文档约束。
        self._package = parse_skill_document(package.document, package.manifest)
        if self._package.content_hash != package.content_hash:
            raise ValueError("技能声明与文档不一致")
        self._tools = tools
        self._data = self._package.data
        if not self._data["enabled"]:
            raise ValueError("技能文档已禁用")
        for name in self._data["allowed_tools"]:
            if tools.definition(name) is None:
                raise ToolExecutionError("tool_not_allowed", "技能请求了当前可信工具集之外的工具")

    @property
    def instructions(self) -> str:
        return self._data["instructions"]

    def openai_tools(self) -> list[dict[str, Any]]:
        allowed = set(self._data["allowed_tools"])
        return [item for item in self._tools.openai_tools() if item["function"]["name"] in allowed]

    async def execute(self, arguments: dict[str, Any], *, call_id: str, remaining_ms: int) -> dict[str, Any]:
        if not isinstance(call_id, str) or not call_id.strip() or len(call_id) > 256:
            raise ToolExecutionError("invalid_tool_call_id", "技能调用必须使用服务端非空标识")
        if type(remaining_ms) is not int or remaining_ms <= 0:
            raise ToolExecutionError("invalid_tool_timeout", "技能需要正整数执行预算")
        deadline = time.monotonic() + remaining_ms / 1000
        await asyncio.sleep(0)
        validate_skill_arguments(arguments)
        ToolCenter._validate_arguments(arguments, self._data["input_schema"])
        arguments = copy.deepcopy(arguments)
        results: dict[str, Any] = {}
        for step in self._data["steps"]:
            # 显式取消检查，不吞 CancelledError，不自动重试未知副作用。
            await asyncio.sleep(0)
            remaining = int((deadline - time.monotonic()) * 1000)
            if remaining <= 0:
                raise ToolExecutionError("skill_timeout", "技能执行预算耗尽", effect_status="unknown" if results else "none")
            values = _resolve(step["arguments"], arguments, results)
            remaining = int((deadline - time.monotonic()) * 1000)
            if remaining <= 0:
                raise ToolExecutionError("skill_timeout", "技能参数准备耗尽预算", effect_status="unknown" if results else "none")
            identity = _json([call_id, self._package.content_hash, step["id"]])
            step_id = "skill-" + hashlib.sha256(identity.encode("utf-8")).hexdigest()
            try:
                result = await self._tools.ainvoke(step["tool"], values, call_id=step_id, remaining_ms=remaining)
            except ToolExecutionError as exc:
                if results and exc.effect_status != "unknown":
                    raise ToolExecutionError(exc.code, exc.message, retryable=False,
                                             elapsed_ms=exc.elapsed_ms, input_digest=exc.input_digest,
                                             effect_status="unknown") from exc
                raise
            results[step["id"]] = _step_result(result, completed=bool(results))
            _ValueBudget(completed=True).check(results)
        if time.monotonic() >= deadline:
            raise ToolExecutionError("skill_timeout", "技能结果准备耗尽预算", effect_status="unknown" if results else "none")
        return {"skillKey": self._data["name"], "version": self._data["version"],
                "contentHash": self._package.content_hash, "executionMode": "declarative-v1",
                "executed": bool(self._data["steps"]), "steps": results, "resultData": copy.deepcopy(results)}


def bind_skill(package: SkillPackage, tools: ControlledToolSet) -> BoundSkill:
    return BoundSkill(package, tools)
