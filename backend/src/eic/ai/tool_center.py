from __future__ import annotations

import asyncio
import copy
import hashlib
import inspect
import json
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from jsonschema import Draft202012Validator, ValidationError

if TYPE_CHECKING:
    from eic.ai.standard_tools import ControlledToolSet, StandardToolDefinition, TrustedToolDispatch

@dataclass(frozen=True)
class ToolDefinition:
    name: str
    description: str
    handler: Callable[[dict[str, Any]], dict[str, Any]]
    input_schema: dict[str, Any] | None = None
    tool_type: str = "local"
    max_output_length: int = 8000
    timeout_ms: int = 10_000
    requires_approval: bool = False
    idempotent: bool = True


@dataclass(frozen=True)
class ToolExecution:
    value: dict[str, Any]
    elapsed_ms: int
    input_digest: str
    output_digest: str
    replayed: bool = False


@dataclass(frozen=True)
class ToolApprovalGrant:
    """仅由持久审批消费路径签发，绑定具体调用，普通 ExecuteTool 不签发。"""

    tool_name: str
    input_digest: str
    idempotency_key: str


class ToolExecutionError(RuntimeError):
    """工具执行失败时返回给 Agent 的结构化错误。"""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool = False,
        elapsed_ms: int = 0,
        input_digest: str = "",
        effect_status: str = "none",
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.elapsed_ms = elapsed_ms
        self.input_digest = input_digest
        self.effect_status = effect_status


class ToolNotAllowedError(ToolExecutionError, PermissionError):
    """保留旧 ToolCenter API 的 PermissionError 兼容性。"""


class ToolValidationError(ValueError):
    """工具参数不符合声明式 JSON Schema。"""

    def __init__(self, message: str, *, path: str = "$") -> None:
        super().__init__(message)
        self.code = "invalid_tool_arguments"
        self.path = path


class ToolCenter:
    """Allowlisted in-process tools. MCP and subprocess execution are not implicit."""

    def __init__(self) -> None:
        self._tools: dict[str, ToolDefinition] = {}
        self._completed_idempotency: dict[str, ToolExecution] = {}
        self._failed_idempotency: dict[str, ToolExecutionError] = {}
        self._idempotency_fingerprints: dict[str, tuple[str, str]] = {}
        self._idempotency_inflight: dict[str, threading.Event] = {}
        self._idempotency_lock = threading.Lock()

    def register(self, tool: ToolDefinition) -> None:
        if tool.name in self._tools:
            raise ValueError(f"duplicate tool: {tool.name}")
        if not tool.name.strip() or any(char.isspace() for char in tool.name):
            raise ValueError("tool name must be a non-empty identifier without whitespace")
        if tool.timeout_ms <= 0:
            raise ValueError("tool timeout must be positive")
        if tool.input_schema is not None:
            Draft202012Validator.check_schema(tool.input_schema)
            _reject_remote_schema_refs(tool.input_schema)
        self._tools[tool.name] = tool

    def get(self, name: str) -> ToolDefinition | None:
        return self._tools.get(name)

    def definitions(self, allowed: set[str] | list[str] | tuple[str, ...]) -> list[ToolDefinition]:
        """只返回当前租户/Agent 白名单中的工具定义。"""
        return [self._tools[name] for name in dict.fromkeys(allowed) if name in self._tools]

    def openai_tools(self, allowed: set[str] | list[str] | tuple[str, ...]) -> list[dict[str, Any]]:
        """转换成 OpenAI-compatible tool schema，不暴露白名单外工具。"""
        tools: list[dict[str, Any]] = []
        for definition in self.definitions(allowed):
            parameters = definition.input_schema or {
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            }
            tools.append({
                "type": "function",
                "function": {
                    "name": definition.name,
                    "description": definition.description,
                    "parameters": parameters,
                },
            })
        return tools

    def standard_tools(
        self,
        allowed: set[str] | list[str] | tuple[str, ...],
        *,
        dispatch: TrustedToolDispatch,
        external: Sequence[StandardToolDefinition] = (),
    ) -> ControlledToolSet:
        """同一声明生成标准工具；必须绑定服务层回调，不默认回退到裸 handler。"""
        from eic.ai.standard_tools import ControlledToolSet, StandardToolDefinition

        definitions = self.definitions(allowed)
        snapshots = {item.name: StandardToolDefinition.from_tool(item) for item in definitions}
        originals = {item.name: item for item in definitions}
        specs = list(snapshots.values())
        names = set(self._tools)
        for item in external:
            if item.tool_type != "mcp" or not item.source_id or not item.source_name:
                raise ValueError("external tool must identify its authorized MCP resource and tool")
            if item.name in names:
                raise ValueError(f"duplicate standard tool: {item.name}")
            names.add(item.name)
            if item.name in allowed:
                specs.append(item)

        def check_definition(spec: StandardToolDefinition) -> None:
            if spec.name not in originals:
                return
            current = self.get(spec.name)
            if current is not originals[spec.name] or StandardToolDefinition.from_tool(current) != snapshots[spec.name]:
                raise ToolExecutionError("tool_definition_changed", "工具声明已变化，必须重新授权并绑定")

        return ControlledToolSet(specs, dispatch=dispatch, check_definition=check_definition)

    def execute(self, name: str, arguments: dict[str, Any], allowed: set[str]) -> dict[str, Any]:
        return self.execute_audited(name, arguments, allowed).value

    def execute_audited(
        self,
        name: str,
        arguments: dict[str, Any],
        allowed: set[str],
        *,
        idempotency_key: str | None = None,
        approval_grant: ToolApprovalGrant | None = None,
    ) -> ToolExecution:
        if name not in allowed or name not in self._tools:
            raise ToolNotAllowedError(
                "tool_not_allowed",
                "工具未获当前租户或 Agent 白名单授权",
                retryable=False,
                input_digest=_digest(arguments),
            )
        definition = self._tools[name]
        if definition.requires_approval and approval_grant != ToolApprovalGrant(
            name, _digest(arguments), idempotency_key or "",
        ):
            raise ToolExecutionError("approval_required", "该工具需要显式审批后执行")
        try:
            self._validate_arguments(arguments, definition.input_schema)
        except ToolValidationError as exc:
            raise ToolExecutionError(
                exc.code,
                f"工具参数校验失败：{exc}",
                retryable=False,
                input_digest=_digest(arguments),
            ) from exc

        if idempotency_key:
            cached = self._wait_for_idempotent_result(idempotency_key, name, _digest(arguments))
            if cached is not None:
                return ToolExecution(
                    value=copy.deepcopy(cached.value),
                    elapsed_ms=cached.elapsed_ms,
                    input_digest=cached.input_digest,
                    output_digest=cached.output_digest,
                    replayed=True,
                )

        start = time.perf_counter()
        try:
            output = definition.handler(arguments)
            if inspect.isawaitable(output):
                if inspect.iscoroutine(output):
                    output.close()
                raise TypeError("当前工具适配器仅执行同步 handler")
            if not isinstance(output, dict):
                raise TypeError("工具必须返回 JSON object")
            output = sanitize_tool_output(output)
            serialized = json.dumps(output, ensure_ascii=False, sort_keys=True, default=str)
            if len(serialized) > definition.max_output_length:
                output = {"truncated": True, "content": serialized[:definition.max_output_length]}
                serialized = json.dumps(output, ensure_ascii=False, sort_keys=True)
            result = ToolExecution(
                value=output,
                elapsed_ms=round((time.perf_counter() - start) * 1000),
                input_digest=_digest(arguments),
                output_digest=_digest(serialized),
            )
        except ToolExecutionError as exc:
            if idempotency_key:
                self._store_idempotent_failure(idempotency_key, exc)
            raise
        except Exception as exc:
            failure = ToolExecutionError(
                "tool_failed",
                "工具执行失败，请检查工具日志和参数",
                retryable=False,
                elapsed_ms=round((time.perf_counter() - start) * 1000),
                input_digest=_digest(arguments),
                effect_status="unknown",
            )
            if idempotency_key:
                self._store_idempotent_failure(idempotency_key, failure)
            raise failure from exc
        if idempotency_key:
            self._store_idempotent_result(idempotency_key, result)
        return result

    async def execute_audited_async(
        self,
        name: str,
        arguments: dict[str, Any],
        allowed: set[str],
        *,
        idempotency_key: str | None = None,
        timeout_ms: int | None = None,
        approval_grant: ToolApprovalGrant | None = None,
    ) -> ToolExecution:
        """在线程池中执行同步工具，并把超时转换成稳定错误码。"""
        definition = self.get(name)
        if definition is None or name not in allowed:
            return self.execute_audited(name, arguments, allowed, idempotency_key=idempotency_key,
                                        approval_grant=approval_grant)
        limit = definition.timeout_ms if timeout_ms is None else timeout_ms
        if limit <= 0:
            raise ToolExecutionError(
                "invalid_tool_timeout",
                "工具超时预算必须为正数",
                retryable=False,
                input_digest=_digest(arguments),
            )
        started = time.perf_counter()
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(
                    self.execute_audited,
                    name,
                    arguments,
                    allowed,
                    idempotency_key=idempotency_key,
                    approval_grant=approval_grant,
                ),
                timeout=limit / 1000,
            )
        except (TimeoutError, asyncio.CancelledError) as exc:
            failure = ToolExecutionError(
                "tool_timeout",
                "工具执行超时，未自动重试以避免未知副作用",
                retryable=False,
                elapsed_ms=round((time.perf_counter() - started) * 1000),
                input_digest=_digest(arguments),
                effect_status="unknown",
            )
            # wait_for 取消等待，不会终止同步 handler 的线程。锁定未知结果，
            # 即使线程稍后成功也不提升为可自动重放的成功。
            if idempotency_key:
                self._store_idempotent_failure(
                    idempotency_key, failure, fingerprint=(name, _digest(arguments)),
                )
            if isinstance(exc, asyncio.CancelledError):
                raise
            raise failure from exc

    @staticmethod
    def _validate_arguments(arguments: dict[str, Any], schema: dict[str, Any] | None) -> None:
        if not isinstance(arguments, dict):
            raise ToolValidationError("工具参数必须是 JSON object")
        try:
            json.dumps(arguments, allow_nan=False)
        except (ValueError, TypeError) as exc:
            raise ToolValidationError("工具参数必须是有效 JSON，不接受非有限数值") from exc
        if schema is None:
            return
        try:
            Draft202012Validator(schema).validate(arguments)
        except ValidationError as exc:
            path = "$" + "".join(f"[{item}]" if isinstance(item, int) else f".{item}"
                                for item in exc.absolute_path)
            # 不把失败实例值或 Schema 原文（可能带密钥）暴露给模型/前端。
            raise ToolValidationError(f"{path} 不符合 {exc.validator} 约束", path=path) from exc

    def _wait_for_idempotent_result(
        self, key: str, name: str, input_digest: str,
    ) -> ToolExecution | None:
        with self._idempotency_lock:
            fingerprint = (name, input_digest)
            previous = self._idempotency_fingerprints.setdefault(key, fingerprint)
            if previous != fingerprint:
                raise ToolExecutionError(
                    "duplicate_tool_call_id", "工具调用标识已绑定到不同工具或参数",
                    input_digest=input_digest,
                )
            failure = self._failed_idempotency.get(key)
            if failure is not None:
                raise failure
            cached = self._completed_idempotency.get(key)
            if cached is not None:
                return cached
            event = self._idempotency_inflight.get(key)
            if event is None:
                self._idempotency_inflight[key] = threading.Event()
                return None
        event.wait()
        with self._idempotency_lock:
            failure = self._failed_idempotency.get(key)
            if failure is not None:
                raise failure
            return self._completed_idempotency.get(key)

    def _store_idempotent_result(self, key: str, result: ToolExecution) -> None:
        with self._idempotency_lock:
            if key not in self._failed_idempotency:
                self._completed_idempotency[key] = copy.deepcopy(result)
            event = self._idempotency_inflight.pop(key, None)
            if event is not None:
                event.set()

    def _store_idempotent_failure(
        self, key: str, failure: ToolExecutionError,
        fingerprint: tuple[str, str] | None = None,
    ) -> None:
        with self._idempotency_lock:
            if fingerprint is not None:
                previous = self._idempotency_fingerprints.setdefault(key, fingerprint)
                if previous != fingerprint:
                    return
            if failure.effect_status == "unknown":
                # 线程完成不代表等待方收到结果。超时/取消必须覆盖交付前
                # 写入的成功缓存，保持未知结果，禁止自动提升为成功重放。
                self._completed_idempotency.pop(key, None)
            if key not in self._completed_idempotency:
                self._failed_idempotency.setdefault(key, failure)
            event = self._idempotency_inflight.pop(key, None)
            if event is not None:
                event.set()


def sanitize_tool_output(output: dict[str, Any]) -> dict[str, Any]:
    """在任何序列化、截断或缓存之前使用 A 分支的统一输出清理边界。"""
    from eic.ai.checkpoint_security import sanitize_tool_result

    if not isinstance(output, dict):
        raise TypeError("工具必须返回 JSON object")
    safe = sanitize_tool_result(output)
    if not isinstance(safe, dict):
        raise TypeError("清理后的工具结果必须是 JSON object")
    return safe


def _reject_remote_schema_refs(value: Any) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if key in {"$ref", "$dynamicRef"} and (
                not isinstance(item, str) or not item.startswith("#")
            ):
                raise ValueError("工具 Schema 仅支持本地引用")
            _reject_remote_schema_refs(item)
    elif isinstance(value, list):
        for item in value:
            _reject_remote_schema_refs(item)


def _digest(value: object) -> str:
    serialized = value if isinstance(value, str) else json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str,
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def validate_mcp_config(config: dict[str, Any]) -> dict[str, Any]:
    transport = config.get("transport")
    if transport not in {"sse", "http", "stdio"}:
        raise ValueError("MCP transport must be sse, http, or stdio")
    if transport == "stdio":
        raise ValueError("stdio MCP requires an explicit sandbox runner and is not enabled")
    endpoint = str(config.get("endpoint", ""))
    if not endpoint.startswith(("https://", "http://")):
        raise ValueError("MCP endpoint must be an HTTP(S) URL")
    return {"transport": transport, "endpoint": endpoint, "headers": dict(config.get("headers", {}))}
