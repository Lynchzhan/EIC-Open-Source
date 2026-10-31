"""将领域工具声明适配为仅调用可信服务的 LangChain BaseTool。"""
from __future__ import annotations

import asyncio
import copy
from collections.abc import Awaitable, Callable, Sequence
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

from jsonschema import Draft202012Validator
from langchain_core.tools import BaseTool
from pydantic import PrivateAttr

from eic.ai.tool_center import (
    ToolCenter, ToolDefinition, ToolExecutionError, ToolValidationError, _reject_remote_schema_refs,
    sanitize_tool_output,
)


@dataclass(frozen=True)
class StandardToolDefinition:
    """仅含声明；MCP source_id/source_name由服务端解析，绝不是凭据或调用权限。"""

    name: str
    description: str
    input_schema: dict[str, Any] | None = None
    tool_type: str = "local"
    max_output_length: int = 8000
    timeout_ms: int = 10_000
    requires_approval: bool = False
    idempotent: bool = True
    source_id: str | None = None
    source_name: str | None = None

    @classmethod
    def from_tool(cls, tool: ToolDefinition) -> StandardToolDefinition:
        return cls(
            name=tool.name, description=tool.description, input_schema=copy.deepcopy(tool.input_schema),
            tool_type=tool.tool_type, max_output_length=tool.max_output_length,
            timeout_ms=tool.timeout_ms, requires_approval=tool.requires_approval, idempotent=tool.idempotent,
        )

    def parameters(self) -> dict[str, Any]:
        # 与 ToolCenter.openai_tools 的旧空 schema 回退逐值一致。
        return copy.deepcopy(self.input_schema or {
            "type": "object", "properties": {}, "additionalProperties": False,
        })

    def verify_mcp_advertisement(self, resource_id: str, advertised: dict[str, Any]) -> None:
        """在原 MCP 服务的同一会话 list 后、持久意图和 call 前核对模型绑定声明。"""
        if (self.tool_type != "mcp" or resource_id != self.source_id
                or advertised.get("name") != self.source_name
                or advertised.get("description", "") != self.description
                or advertised.get("inputSchema") != self.input_schema):
            raise ToolExecutionError("tool_definition_changed", "MCP 工具声明已变化，必须重新授权并绑定")


@dataclass(frozen=True)
class ControlledToolCall:
    """由可信入口组装；审批凭据、租户及幂等命名空间仍留在服务回调闭包。"""

    definition: StandardToolDefinition
    arguments: dict[str, Any]
    call_id: str
    remaining_ms: int


TrustedToolDispatch = Callable[[ControlledToolCall], Awaitable[dict[str, Any]]]


def _preflight_skill(arguments: dict[str, Any], tool_type: str) -> None:
    if tool_type == "skill":
        # 延迟导入避免工具层与声明式技能模块形成初始化环。
        from eic.ai.declarative_skills import validate_skill_arguments

        validate_skill_arguments(arguments)


def _validate(arguments: dict[str, Any], schema: dict[str, Any] | None) -> None:
    try:
        ToolCenter._validate_arguments(arguments, schema)
    except ToolValidationError as exc:
        raise ToolExecutionError(exc.code, f"工具参数校验失败：{exc}", retryable=False) from exc


@dataclass
class _Invocation:
    name: str
    arguments: dict[str, Any]
    call_id: str
    remaining_ms: int
    consumed: bool = False


class _ServiceTool(BaseTool):
    """不支持同步线程回退；持有服务回调而非领域 handler。"""

    _execute: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]] = PrivateAttr()
    _tool_type: str = PrivateAttr()

    def __init__(self, spec: StandardToolDefinition, execute: Callable[..., Any]) -> None:
        super().__init__(name=spec.name, description=spec.description, args_schema=spec.parameters())
        self._execute = execute
        self._tool_type = spec.tool_type

    def _to_args_and_kwargs(
        self, tool_input: str | dict[str, Any], tool_call_id: str | None,
    ) -> tuple[tuple[Any, ...], dict[str, Any]]:
        # 保留 config/callbacks/run_manager 等合法参数键，不让 SDK 将其当控制参数覆盖。
        if not isinstance(tool_input, dict):
            raise ToolExecutionError("invalid_tool_arguments", "工具参数必须是 JSON object")
        _preflight_skill(tool_input, self._tool_type)
        return (copy.deepcopy(tool_input),), {}

    def _run(self, arguments: dict[str, Any]) -> Any:
        raise ToolExecutionError("trusted_async_dispatch_required", "标准工具仅支持可信异步服务入口")

    def run(self, tool_input: Any, *args: Any, **kwargs: Any) -> Any:
        # 同步拒绝也必须先于 SDK callback，以免拒绝前暴露输入。
        raise ToolExecutionError("trusted_async_dispatch_required", "标准工具仅支持可信异步服务入口")

    async def _arun(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return await self._execute(arguments)

    async def arun(self, tool_input: str | dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        """专用服务入口不启用 SDK callback、环境 tracing 或额外执行 Task。"""
        if kwargs:
            raise ToolExecutionError("tool_execution_config_forbidden", "受控工具不接受外部执行配置")
        if not isinstance(tool_input, dict):
            raise ToolExecutionError("invalid_tool_arguments", "工具参数必须是 JSON object")
        _preflight_skill(tool_input, self._tool_type)
        return await self._arun(copy.deepcopy(tool_input))


class ControlledToolSet:
    """绑定与执行共享声明快照；工具对象单独 ainvoke 不获得执行资格。

    dispatch 必须是主入口绑定的 EIC 完整可信服务：在实际副作用前重核权限、
    审批、预算、凭据、持久意图，并按原路径完成审计和事务。此类不替代这些步骤。
    """

    def __init__(
        self, definitions: Sequence[StandardToolDefinition], *, dispatch: TrustedToolDispatch,
        check_definition: Callable[[StandardToolDefinition], None] | None = None,
    ) -> None:
        if not callable(dispatch):
            raise TypeError("trusted service dispatch is required")
        self._scope: ContextVar[_Invocation | None] = ContextVar("eic_standard_tool_invocation", default=None)
        self._specs: dict[str, StandardToolDefinition] = {}
        self._tools: dict[str, BaseTool] = {}
        for definition in definitions:
            spec = copy.deepcopy(definition)
            if not spec.name.strip() or any(char.isspace() for char in spec.name) or spec.name in self._specs:
                raise ValueError("invalid or duplicate standard tool name")
            if type(spec.timeout_ms) is not int or spec.timeout_ms <= 0:
                raise ValueError("tool timeout must be positive")
            if spec.input_schema is not None:
                Draft202012Validator.check_schema(spec.input_schema)
                _reject_remote_schema_refs(spec.input_schema)
            self._specs[spec.name] = spec

            async def execute(arguments: dict[str, Any], bound: StandardToolDefinition = spec) -> dict[str, Any]:
                _preflight_skill(arguments, bound.tool_type)
                invocation = self._scope.get()
                if (invocation is None or invocation.consumed or invocation.name != bound.name
                        or invocation.arguments != arguments):
                    raise ToolExecutionError("trusted_dispatch_required", "缺少匹配的可信工具调用上下文")
                invocation.consumed = True
                if check_definition is not None:
                    check_definition(bound)
                _validate(arguments, bound.input_schema)
                # 不签发 approval grant、不生成新幂等键、不重试、不创建 MCP session。
                result = await dispatch(ControlledToolCall(
                    definition=copy.deepcopy(bound), arguments=copy.deepcopy(arguments),
                    call_id=invocation.call_id, remaining_ms=invocation.remaining_ms,
                ))
                return sanitize_tool_output(result)

            self._tools[spec.name] = _ServiceTool(spec, execute)

    def model_tools(self) -> list[BaseTool]:
        """传给 model.bind_tools；不含 handler、principal、凭据或审批 grant。"""
        return list(self._tools.values())

    def definition(self, name: str) -> StandardToolDefinition | None:
        """供主的 validate/approval 节点读取同份声明，不返回内部可变 schema。"""
        spec = self._specs.get(name)
        return copy.deepcopy(spec) if spec is not None else None

    def openai_tools(self) -> list[dict[str, Any]]:
        """原 provider 的兼容 schema，同样由执行侧声明快照生成。"""
        return [{"type": "function", "function": {
            "name": spec.name, "description": spec.description, "parameters": spec.parameters(),
        }} for spec in self._specs.values()]

    async def ainvoke(
        self, name: str, arguments: dict[str, Any], *, call_id: str, remaining_ms: int,
    ) -> dict[str, Any]:
        """可信主入口使用；模型只提供 name/arguments，不提供此处身份和预算。"""
        if name not in self._tools:
            raise ToolExecutionError("tool_not_allowed", "工具未获当前白名单授权")
        if not isinstance(call_id, str) or not call_id.strip():
            raise ToolExecutionError("invalid_tool_call_id", "工具调用必须保留非空标识")
        if type(remaining_ms) is not int or remaining_ms <= 0:
            raise ToolExecutionError("invalid_tool_timeout", "工具超时预算必须为正整数")
        spec = self._specs[name]
        _preflight_skill(arguments, spec.tool_type)
        _validate(arguments, spec.input_schema)
        tool = self._tools[name]
        if (tool.name != spec.name or tool.description != spec.description
                or tool.args_schema != spec.parameters()):
            raise ToolExecutionError("tool_definition_changed", "标准工具声明已变化，必须重新绑定")
        invocation = _Invocation(name, copy.deepcopy(arguments), call_id, min(remaining_ms, spec.timeout_ms))
        token = self._scope.set(invocation)
        try:
            # 专用 arun 保持同一 Task，且预算覆盖可信服务的前置和收尾阶段。
            async with asyncio.timeout(invocation.remaining_ms / 1000):
                return await tool.arun(copy.deepcopy(arguments))
        except TimeoutError as exc:
            # 外层覆盖授权、持久意图及 handler，超时不能推断副作用未发生。
            raise ToolExecutionError('tool_timeout', '工具调用超时，结果不确定，需人工复核',
                                     retryable=False, effect_status='unknown') from exc
        finally:
            invocation.consumed = True
            self._scope.reset(token)
