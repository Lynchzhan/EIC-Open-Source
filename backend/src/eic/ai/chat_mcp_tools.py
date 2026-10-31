"""聊天 MCP 声明发现；执行仅交给主入口已有治理服务。"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import math
import time
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from typing import Any

from jsonschema import Draft202012Validator

from eic.ai.mcp_client import HttpMcpClient
from eic.ai.standard_tools import ControlledToolCall, StandardToolDefinition
from eic.ai.stream_reader import complete_cleanup
from eic.ai.tool_center import ToolCenter, ToolExecutionError, _reject_remote_schema_refs
from eic.core.security import decrypt_secret
from eic.models import Resource

MAX_RESOURCES = 16
MAX_TOOLS_PER_RESOURCE = 64
MAX_SELECTION_ENTRIES = 1024
MAX_DESCRIPTION_BYTES = 8192
MAX_SCHEMA_BYTES = 65536
MAX_ADVERTISEMENT_BYTES = 131072
MAX_RESOURCE_ADVERTISEMENT_BYTES = 1048576


def _bounded_json_size(value: Any, limit: int) -> int:
    """先迭代限制深度、节点及字符串，再序列化；不递归遍历任意远程树。"""
    stack = [(value, 0)]
    nodes, string_bytes = 0, 0
    while stack:
        item, depth = stack.pop()
        nodes += 1
        if nodes > 4096 or depth > 32:
            _deny("mcp_advertisement_too_large")
        if isinstance(item, str):
            if len(item) > limit:
                _deny("mcp_advertisement_too_large")
            string_bytes += len(item.encode("utf-8"))
            if string_bytes > limit:
                _deny("mcp_advertisement_too_large")
        elif isinstance(item, dict):
            if len(item) > 4096 or any(not isinstance(key, str) for key in item):
                _deny("invalid_mcp_advertisement")
            if nodes + len(stack) + 2 * len(item) > 4096:
                _deny("mcp_advertisement_too_large")
            stack.extend((key, depth + 1) for key in item)
            stack.extend((child, depth + 1) for child in item.values())
        elif isinstance(item, list):
            if nodes + len(stack) + len(item) > 4096:
                _deny("mcp_advertisement_too_large")
            stack.extend((child, depth + 1) for child in item)
        elif item is not None and not isinstance(item, (bool, int, float)):
            _deny("invalid_mcp_advertisement")
    try:
        size = len(json.dumps(value, ensure_ascii=False, allow_nan=False,
                              separators=(",", ":")).encode("utf-8"))
    except (ValueError, OverflowError, RecursionError):
        _deny("invalid_mcp_advertisement")
    if size > limit:
        _deny("mcp_advertisement_too_large")
    return size


def _check_advertisement(advertised: dict[str, Any]) -> int:
    size = _bounded_json_size(advertised, MAX_ADVERTISEMENT_BYTES)
    name, description = advertised.get("name"), advertised.get("description", "")
    if (not isinstance(name, str) or not name.strip() or len(name) > 128
            or not isinstance(description, str)):
        _deny("invalid_mcp_advertisement")
    if len(description.encode("utf-8")) > MAX_DESCRIPTION_BYTES:
        _deny("mcp_advertisement_too_large")
    schema = advertised.get("inputSchema")
    if not isinstance(schema, dict):
        _deny("invalid_mcp_advertisement")
    _bounded_json_size(schema, MAX_SCHEMA_BYTES)
    return size

@dataclass(frozen=True)
class ChatMcpSnapshot:
    """服务闭包持有；配置仅留摘要，不包含 endpoint、headers 或凭据。"""

    resource_id: int
    config_digest: str
    definitions: tuple[StandardToolDefinition, ...]


def _config_digest(config: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(config, sort_keys=True, ensure_ascii=False,
                                    separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _deny(code: str) -> None:
    raise ToolExecutionError(code, "聊天 MCP 当前配置或授权不允许此调用", retryable=False)


def _resource_id(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        _deny("invalid_mcp_selection")
    if isinstance(value, str) and (not value.isascii() or not value.isdigit()):
        _deny("invalid_mcp_selection")
    number = int(value)
    if not 0 < number < 2**63:
        _deny("invalid_mcp_selection")
    return number


def selected_mcp_ids(app_data: dict[str, Any]) -> tuple[int, ...]:
    """仅解析可信 app 配置；不根据资源目录自动扩大选择。"""
    enabled = app_data.get("isMcp")
    if not (enabled is True or (type(enabled) is int and enabled == 1)
            or (type(enabled) is str and enabled in ("1", "true"))):
        return ()
    entries = app_data.get("mcps", [])
    if not isinstance(entries, list):
        _deny("invalid_mcp_selection")
    if len(entries) > MAX_SELECTION_ENTRIES:
        _deny("mcp_selection_limit")
    selected: list[int] = []
    for entry in entries:
        if isinstance(entry, dict):
            if entry.get("isSelect") is not True:
                continue
            values = [entry[key] for key in ("aIMcpId", "aiSkillToolManagementId", "id")
                      if entry.get(key) is not None]
            if not values:
                _deny("invalid_mcp_selection")
            ids = {_resource_id(value) for value in values}
            if len(ids) != 1:
                _deny("ambiguous_mcp_selection")
            resource_id = ids.pop()
        else:
            resource_id = _resource_id(entry)
        if resource_id not in selected:
            selected.append(resource_id)
            if len(selected) > MAX_RESOURCES:
                _deny("mcp_resource_limit")
    return tuple(selected)


def standard_mcp_name(resource_id: int, remote_name: str) -> str:
    if not isinstance(remote_name, str) or not remote_name.strip() or len(remote_name) > 128:
        _deny("invalid_mcp_tool_name")
    return f"mcp_{_resource_id(resource_id)}_{hashlib.sha256(remote_name.encode()).hexdigest()[:16]}"


def _automatic_names(config: dict[str, Any]) -> set[str]:
    names: set[str] = set()
    for key in ("chatAutoTools", "chatReadOnlyTools"):
        values = config.get(key, [])
        if (not isinstance(values, list) or len(values) > MAX_TOOLS_PER_RESOURCE
                or any(not isinstance(v, str) or not v.strip() or len(v) > 128 for v in values)):
            _deny("invalid_mcp_auto_allowlist")
        names.update(values)
    if len(names) > MAX_TOOLS_PER_RESOURCE:
        _deny("mcp_tool_limit")
    return names


def _config(resource: Resource, tenant_id: int, resource_id: int) -> dict[str, Any]:
    if (resource is None or resource.id != resource_id or resource.tenant_id != tenant_id
            or resource.resource_type != "mcps" or resource.is_deleted
            or not isinstance(resource.data, dict) or resource.data.get("enabled", True) is not True
            or resource.data.get("activeStatus", 1) not in (1, "1")):
        _deny("mcp_resource_unavailable")
    return copy.deepcopy(resource.data)


def _timeout_ms(config: dict[str, Any]) -> int:
    timeout = config.get("timeout", 30)
    if (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout) or not 0 < timeout <= 120):
        _deny("invalid_mcp_timeout")
    return max(1, int(timeout * 1000))


def _definition(resource_id: int, advertised: dict[str, Any], timeout_ms: int) -> StandardToolDefinition:
    _check_advertisement(advertised)
    name = advertised.get("name")
    schema = advertised.get("inputSchema")
    description = advertised.get("description", "")
    if not isinstance(schema, dict) or not isinstance(description, str):
        _deny("invalid_mcp_advertisement")
    Draft202012Validator.check_schema(schema)
    _reject_remote_schema_refs(schema)
    return StandardToolDefinition(
        name=standard_mcp_name(resource_id, name), description=description,
        input_schema=copy.deepcopy(schema), tool_type="mcp", source_id=str(resource_id),
        source_name=name, timeout_ms=timeout_ms, requires_approval=False, idempotent=False,
    )


async def _advertisements(client: HttpMcpClient) -> list[dict[str, Any]]:
    cursor, seen_cursors, names, tools = None, set(), set(), []
    total_bytes = 0
    for _ in range(64):
        page = await client.list_tools(cursor=cursor)
        if not isinstance(page, dict) or not isinstance(page.get("tools"), list):
            _deny("invalid_mcp_advertisement")
        if len(tools) + len(page["tools"]) > MAX_TOOLS_PER_RESOURCE:
            _deny("mcp_tool_limit")
        for tool in page["tools"]:
            if not isinstance(tool, dict) or not isinstance(tool.get("name"), str):
                _deny("invalid_mcp_advertisement")
            total_bytes += _check_advertisement(tool)
            if total_bytes > MAX_RESOURCE_ADVERTISEMENT_BYTES:
                _deny("mcp_advertisement_too_large")
            name = tool["name"]
            if name in names:
                _deny("duplicate_mcp_tool_name")
            names.add(name)
            tools.append(tool)
        cursor = page.get("nextCursor")
        if cursor is None:
            return tools
        if not isinstance(cursor, str) or not cursor or len(cursor) > 2048 or cursor in seen_cursors:
            _deny("invalid_mcp_cursor")
        seen_cursors.add(cursor)
    _deny("mcp_pagination_limit")


@asynccontextmanager
async def _resource_network_scope(resource_id, network_scope):
    if network_scope is None:
        yield
    else:
        async with network_scope(resource_id) as admitted_digest:
            yield admitted_digest


async def discover_chat_mcp_tools(
    app_data: dict[str, Any], *, tenant_id: int,
    authorize_resource: Callable[[int], Awaitable[Resource]],
    before_discovery: Callable[[int, int], Awaitable[None]],
    remaining_ms: int,
    snapshots: list[ChatMcpSnapshot] | None = None,
    network_scope: Callable[[int], AbstractAsyncContextManager[Any]] | None = None,
) -> list[StandardToolDefinition]:
    """回调缺一不可；不持有 principal、数据库 session 或长期明文凭据。"""
    if type(remaining_ms) is not int or remaining_ms <= 0:
        _deny("tool_budget_exceeded")
    selected = selected_mcp_ids(app_data)
    definitions: list[StandardToolDefinition] = []
    prepared_snapshots: list[ChatMcpSnapshot] = []
    names: set[str] = set()
    deadline = time.monotonic() + remaining_ms / 1000
    async with asyncio.timeout(remaining_ms / 1000):
        for resource_id in selected:
            async with _resource_network_scope(resource_id, network_scope) as admitted_digest:
                resource = await authorize_resource(resource_id)
                config = _config(resource, tenant_id, resource_id)
                if admitted_digest is not None and _config_digest(config) != admitted_digest:
                    _deny("mcp_configuration_changed")
                allowed = _automatic_names(config)
                if not allowed:
                    continue
                timeout_ms = _timeout_ms(config)
                available_ms = min(timeout_ms, int((deadline - time.monotonic()) * 1000))
                if available_ms <= 0:
                    _deny("tool_budget_exceeded")
                await before_discovery(resource_id, available_ms)
                # 审计/预算可能 await；外呼前重新授权并拒绝配置漂移。
                current = await authorize_resource(resource_id)
                if _config(current, tenant_id, resource_id) != config:
                    _deny("mcp_configuration_changed")
                headers: dict[str, str] = {}
                if config.get("headers_encrypted"):
                    try:
                        headers = json.loads(decrypt_secret(config["headers_encrypted"]))
                    except Exception:
                        _deny("mcp_credentials_unavailable")
                    if not isinstance(headers, dict) or any(
                        not isinstance(k, str) or not isinstance(v, str) for k, v in headers.items()
                    ):
                        _deny("mcp_credentials_unavailable")
                available_ms = min(timeout_ms, int((deadline - time.monotonic()) * 1000))
                if available_ms <= 0:
                    _deny("tool_budget_exceeded")
                client = HttpMcpClient({"transport": config.get("transport"),
                                        "endpoint": config.get("endpoint"), "headers": headers},
                                       timeout=available_ms / 1000)
                try:
                    resource_definitions: list[StandardToolDefinition] = []
                    await client.initialize()
                    for advertised in await _advertisements(client):
                        if advertised["name"] not in allowed:
                            continue
                        spec = _definition(resource_id, advertised, timeout_ms)
                        if spec.name in names:
                            _deny("duplicate_mcp_binding")
                        names.add(spec.name)
                        definitions.append(spec)
                        resource_definitions.append(spec)
                    # 网络等待期间也可能发生撤权；返回绑定前再核对一次。
                    current = await authorize_resource(resource_id)
                    if _config(current, tenant_id, resource_id) != config:
                        _deny("mcp_configuration_changed")
                    prepared_snapshots.append(ChatMcpSnapshot(resource_id, _config_digest(config),
                                                             tuple(copy.deepcopy(resource_definitions))))
                finally:
                    try:
                        await complete_cleanup(client.aclose())
                    finally:
                        headers.clear()
    if snapshots is not None:
        snapshots.extend(prepared_snapshots)
    return definitions


async def revalidate_chat_mcp_snapshots(
    snapshots: list[ChatMcpSnapshot] | tuple[ChatMcpSnapshot, ...], *,
    app_data: dict[str, Any], tenant_id: int,
    authorize_resource: Callable[[int], Awaitable[Resource]],
) -> list[StandardToolDefinition]:
    """每轮仅调用可信 DB 授权回调；不解密、不创建 SDK session、不联网。"""
    selected = selected_mcp_ids(app_data)
    if len(snapshots) > MAX_RESOURCES:
        _deny("mcp_resource_limit")
    definitions: list[StandardToolDefinition] = []
    for snapshot in snapshots:
        if len(snapshot.definitions) > MAX_TOOLS_PER_RESOURCE:
            _deny("mcp_tool_limit")
        if snapshot.resource_id not in selected:
            _deny("tool_not_allowed")
        resource = await authorize_resource(snapshot.resource_id)
        config = _config(resource, tenant_id, snapshot.resource_id)
        if _config_digest(config) != snapshot.config_digest:
            _deny("mcp_configuration_changed")
        allowed = _automatic_names(config)
        for spec in snapshot.definitions:
            if (spec.source_name not in allowed or spec.source_id != str(snapshot.resource_id)
                    or spec.name != standard_mcp_name(snapshot.resource_id, spec.source_name)):
                _deny("tool_definition_changed")
            definitions.append(copy.deepcopy(spec))
    return definitions


def verify_chat_mcp_call(
    call: ControlledToolCall, *, resource: Resource, app_data: dict[str, Any],
    advertised: dict[str, Any], tenant_id: int,
) -> None:
    """只做声明重核，不能替代 main 的身份/预算/审批/intent 服务。"""
    spec = call.definition
    if spec.tool_type != "mcp" or spec.source_id is None:
        _deny("tool_not_allowed")
    resource_id = _resource_id(spec.source_id)
    if resource_id not in selected_mcp_ids(app_data):
        _deny("tool_not_allowed")
    config = _config(resource, tenant_id, resource_id)
    if spec.source_name not in _automatic_names(config):
        _deny("approval_required")
    current = _definition(resource_id, advertised, _timeout_ms(config))
    if current != spec:
        _deny("tool_definition_changed")
    spec.verify_mcp_advertisement(str(resource_id), advertised)
    if type(call.remaining_ms) is not int or call.remaining_ms <= 0:
        _deny("tool_budget_exceeded")
    ToolCenter._validate_arguments(call.arguments, current.input_schema)
