"""聊天工具调用服务；编排层保留轮次、流式事件与终态所有权。"""
import asyncio
import logging
from dataclasses import dataclass
from collections.abc import Callable
from typing import Any

from eic.ai import chat_mcp_tools, context_access, declarative_skills
from eic.ai.agent_graph import ToolCallRequest
from eic.ai.checkpoint_security import sanitize_tool_result
from eic.ai.reliable_calls import validate_tool_arguments
from eic.ai.standard_tools import ControlledToolSet
from eic.ai.stream_reader import complete_cleanup
from eic.ai.tool_center import ToolExecutionError
from eic.core import agent_runtime, agent_worker, generation
from eic.core.chat_tool_cleanup import converge_child_unknown
from eic.core.runtime_refs import RuntimeReferenceUnavailable


@dataclass
class ChatToolExecutor:
    runtime: Any
    dependencies: Any
    principal: Any
    request_id: str
    agent_run_id: str | None
    stored_message: Any
    standard_tools: Any
    base_tools: Any
    skill_bindings: dict
    app_id: int | None
    app_config: dict
    mcp_snapshots: Any
    allowed_tool_names: set[str]
    limits: Any
    validate_context: Callable
    reserve_child_call: Callable
    used_calls: Callable

    async def validate(self, request):
        tool_call_count, limits = self.used_calls(), self.limits
        standard_tools, allowed_tool_names, runtime = self.standard_tools, self.allowed_tool_names, self.runtime
        if tool_call_count >= limits.max_tool_calls:
            return {"errorCode": "max_tool_calls_exceeded", "message": "工具预算耗尽"}
        definition = standard_tools.definition(request.name)
        if definition is None or request.name not in allowed_tool_names:
            return {"errorCode": "tool_not_allowed", "message": "工具未获当前 Agent 白名单授权"}
        if definition.tool_type == "skill":
            declarative_skills.validate_skill_arguments(request.arguments)
        try:
            validate_tool_arguments(runtime.tools, request.arguments, definition.input_schema)
        except ToolExecutionError as exc:
            return {"errorCode": exc.code, "message": str(exc)}
        return {"requiresApproval": definition.requires_approval}

    async def execute(self, request, remaining_ms):
        await self.validate_context()
        runtime, dependencies, principal = self.runtime, self.dependencies, self.principal
        request_id, agent_run_id, stored_message = self.request_id, self.agent_run_id, self.stored_message
        standard_tools, base_tools, skill_bindings = self.standard_tools, self.base_tools, self.skill_bindings
        app_id, app_config, mcp_snapshots = self.app_id, self.app_config, self.mcp_snapshots
        allowed_tool_names = self.allowed_tool_names
        definition = standard_tools.definition(request.name)
        await dependencies.begin_chat_tool_call(principal, agent_run_id, request, definition)
        if definition.tool_type == "context":
            async with runtime.sessions() as context_session:
                await dependencies.require_runtime_chat_permission(context_session, principal)
                await dependencies.require_owned_chat_session(context_session, principal, stored_message.session_id)
                value = await context_access.invoke(context_session, request.name, request.arguments,
                    tenant_id=principal.tenant_id, owner_id=principal.user_id,
                    session_id=stored_message.session_id, before_id=stored_message.id)
            return {"callId": request.call_id, "toolName": request.name, "status": "completed",
                    "result": value, "effectStatus": "none"}
        if request.name in skill_bindings:
            package = skill_bindings[request.name]
            async def dispatch_step(call):
                self.reserve_child_call()
                child = ToolCallRequest(call_id=call.call_id, name=call.definition.name,
                    arguments=call.arguments, requires_approval=call.definition.requires_approval)
                original_generation = generation.OWNER.get()
                original_worker = agent_worker.OWNER.get()
                child_digest = agent_runtime.digest_payload(child.arguments, redact=False)
                try:
                    result = await base_tools.ainvoke(child.name, child.arguments,
                        call_id=child.call_id, remaining_ms=call.remaining_ms)
                    await dependencies.record_chat_tool_result(principal=principal, request_id=request_id,
                        agent_run_id=agent_run_id, call_id=child.call_id, tool_name=child.name,
                        arguments=child.arguments, definition=call.definition, result=result)
                except BaseException:
                    try:
                        await complete_cleanup(converge_child_unknown(
                            runtime.sessions, generation_owner=original_generation,
                            worker_owner=original_worker, tenant_id=principal.tenant_id,
                            user_id=principal.user_id, request_id=request_id, run_id=agent_run_id,
                            call_id=child.call_id, tool_name=child.name, input_digest=child_digest,
                            tool_type=call.definition.tool_type))
                    except BaseException:
                        # 清理错误不替换原取消；不记录参数或异常敏感正文。
                        logging.getLogger(__name__).error("Skill child outcome cleanup unavailable")
                    raise
                return result
            step_tools = ControlledToolSet(
                [base_tools.definition(name) for name in package.data["allowed_tools"]],
                dispatch=dispatch_step)
            value = await declarative_skills.bind_skill(package, step_tools).execute(
                request.arguments, call_id=f"{agent_run_id}:{request.call_id}",
                remaining_ms=remaining_ms)
            digest = agent_runtime.digest_payload(value, redact=False)
            return {"callId": request.call_id, "toolName": request.name, "status": "completed",
                "result": sanitize_tool_result(value), "outputDigest": digest,
                "executionOutputDigest": digest,
                "runtimeOutputDigest": agent_runtime.digest_payload(sanitize_tool_result(value)),
                "effectStatus": "applied"}
        if definition.tool_type == "mcp":
            async def authorize_mcp(item):
                async with runtime.sessions() as current_session:
                    await generation.fence(current_session)
                    await dependencies.require_owned_chat_request(current_session, principal, request_id)
                    live = await dependencies.authorize_chat_mcp_resource(current_session, principal,
                        app_id, app_config, item.id)
                    await chat_mcp_tools.revalidate_chat_mcp_snapshots(
                        mcp_snapshots, app_data=app_config, tenant_id=principal.tenant_id,
                        authorize_resource=lambda resource_id: dependencies.authorize_chat_mcp_resource(
                            current_session, principal, app_id, app_config, resource_id))
                    if live.data != item.data:
                        raise RuntimeReferenceUnavailable("mcp_configuration_changed")
            async with runtime.sessions() as mcp_session:
                async with asyncio.timeout(remaining_ms / 1000):
                    value = await dependencies._mcp_call_service({
                        "mcpId": int(definition.source_id), "name": definition.source_name,
                        "arguments": request.arguments,
                        "idempotencyKey": f"{agent_run_id}:{request.call_id}"},
                        principal, mcp_session, expected_definition=definition,
                        authorize=authorize_mcp)
            digest = agent_runtime.digest_payload(value, redact=False)
            return {"callId": request.call_id, "toolName": request.name,
                "status": "failed" if value.get("isError") else "completed",
                "result": sanitize_tool_result(value), "outputDigest": digest,
                "executionOutputDigest": digest,
                "runtimeOutputDigest": agent_runtime.digest_payload(sanitize_tool_result(value)),
                "effectStatus": "applied"}
        execution = await dependencies.reliable_tool(runtime.tools, request.name, request.arguments,
            allowed_tool_names, tenant_id=principal.tenant_id, sessions=runtime.sessions,
            idempotency_key=f"{agent_run_id or request_id}:{request.call_id}",
            timeout_ms=min(definition.timeout_ms, remaining_ms))
        return {"callId": request.call_id, "toolName": request.name, "status": "completed",
            "result": sanitize_tool_result(execution.value), "inputDigest": execution.input_digest,
            "outputDigest": execution.output_digest, "elapsedMs": execution.elapsed_ms,
            "executionOutputDigest": execution.output_digest,
            "runtimeOutputDigest": agent_runtime.digest_payload(sanitize_tool_result(execution.value)),
            "replayed": execution.replayed, "effectStatus": "applied"}


