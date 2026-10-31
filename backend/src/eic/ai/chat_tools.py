"""chat_tools 领域；通过显式装配提供跨域依赖。"""
from dataclasses import dataclass
from collections.abc import Callable
from types import SimpleNamespace as _RouteExports
from typing import Any
from eic.core.runtime_bindings import RuntimeBindings
from eic.ai.agent_graph import AGENT_GRAPH_VERSION
from eic.models import AgentRun
from eic.models import ChatMessage
from eic.core.authentication import Principal
from eic.models import ToolAudit
from eic.ai.tool_center import ToolDefinition
from eic.ai.tool_center import ToolExecutionError
from eic.core import agent_approval
from eic.core import agent_runtime
from eic.ai.context_manifest import bounded_tool_result
from eic.core import generation
from eic.ai.checkpoint_security import sanitize_tool_result
import time
from eic.ai.reliable_calls import validate_tool_arguments


@dataclass(frozen=True)
class Dependencies:
    new_id: Callable[..., Any]
    require_runtime_chat_permission: Callable[..., Any]


def bind_routes(*, dependencies: Dependencies, runtime: RuntimeBindings):
    async def finish_approved_tool(ticket, principal, stored_id, envelope, allowed, definition,
                                   execution, key, fingerprint, started):
        """先提交已知工具结果，再建立下一票据；后续失败不得丢失已应用副作用。"""
        async with runtime.sessions() as session:
            await dependencies.new_id(session)
            run = await session.get(AgentRun, ticket.runId)
            stored = await session.get(ChatMessage, stored_id)
            call = await agent_runtime.runtime_store.get_tool_call(session, run_id=run.run_id, call_id=ticket.callId)
            if run.status != "approval_executing" or call.status != "running":
                raise agent_approval.conflict("tool_outcome_uncertain")
            call.status, call.effect_status = "completed", "applied"
            # 审批恢复沿用执行层摘要；不以 runtime 脱敏摘要替换旧票据绑定。
            call.output_digest, call.finished_at = execution.output_digest, agent_runtime.utcnow()
            name, input_digest = call.tool_name, call.input_digest
            result = {"callId": call.call_id, "toolName": name, "status": "completed",
                      "result": sanitize_tool_result(execution.value), "inputDigest": input_digest,
                      "outputDigest": execution.output_digest, "effectStatus": "applied",
                      "executionOutputDigest": execution.output_digest,
                      "runtimeOutputDigest": agent_runtime.digest_payload(sanitize_tool_result(execution.value)),
                      "elapsedMs": execution.elapsed_ms}
            envelope["conversation"].append({
                "role": "tool", "tool_call_id": call.call_id, "name": name,
                "content": bounded_tool_result({"status": "completed", "result": result["result"]}),
            })
            envelope["toolCallCount"] += 1
            envelope["toolResults"][call.call_id] = result
            envelope["seenCalls"][call.call_id] = [name, input_digest]
            envelope["attempts"][-1]["toolCalls"] = envelope["toolCallCount"]
            envelope["remainingMs"] = max(0, envelope["remainingMs"] - round((time.perf_counter() - started) * 1000))
            await agent_runtime.runtime_store.append_event(
                session, run_id=run.run_id, node_name="record_tool_result",
                event_type="tool_result", payload={k: result[k] for k in
                    ("callId", "toolName", "status", "inputDigest", "outputDigest", "effectStatus")})
            session.add(ToolAudit(
                id=await dependencies.new_id(session), tenant_id=principal.tenant_id, user_id=principal.user_id,
                chat_id=int(ticket.requestId) if ticket.requestId.isdigit() else None,
                tool_name=name, tool_type=definition.tool_type, outcome="success",
                input_digest=input_digest, output_digest=execution.output_digest,
                duration_ms=execution.elapsed_ms, error_message="",
            ))
            await agent_approval.emit(session, run, "aIToolsContentMsg", f"工具 {name} 执行完成。",
                                      callId=call.call_id, toolName=name, status="completed")
            await session.commit()
        async with runtime.sessions() as session:
            await dependencies.new_id(session)
            run = await session.get(AgentRun, ticket.runId)
            stored = await session.get(ChatMessage, stored_id)
            call = await agent_runtime.runtime_store.get_tool_call(session, run_id=run.run_id, call_id=ticket.callId)
            if run.status != "approval_executing" or call.status != "completed":
                raise agent_approval.conflict("tool_outcome_uncertain")
            remaining = envelope["remainingCalls"]
            if remaining:
                next_call = remaining.pop(0)
                next_definition = runtime.tools.get(next_call["name"])
                if next_definition is None or next_call["name"] not in allowed:
                    await agent_approval.fail_uncertain(session, run, stored, call, "runtime_reference_unavailable")
                else:
                    validate_tool_arguments(runtime.tools, next_call["arguments"], next_definition.input_schema)
                    await agent_approval.pause(
                        session, run, stored, tool=next_definition, arguments=next_call["arguments"],
                        call_id=next_call["callId"], envelope=envelope)
            else:
                await agent_runtime.runtime_store.append_checkpoint(
                    session, run_id=run.run_id, node_name="approval_continuation",
                    state={"graphVersion": AGENT_GRAPH_VERSION, "providerStarted": False,
                           "providerOutcome": "known", "continuationEnvelope": agent_approval.seal(envelope)},
                    resume_policy="safe_replay")
                stored.status, run.status = "streaming", "running"
            response = {"status": run.status, "toolStatus": "completed", "replayed": False,
                        "requestId": run.request_id, "runId": run.run_id,
                        "afterEventId": f"{run.request_id}-{(run.run_metadata or {}).get('streamAfterSequence', 0)}"}
            receipts = dict((run.run_metadata or {}).get("consumeReceipts") or {})
            receipts[key] = {"identity": fingerprint, "response": response}
            run.run_metadata = {**(run.run_metadata or {}), "consumeReceipts": receipts}
            await session.commit()
            return response


    async def begin_chat_tool_call(
        principal: Principal, agent_run_id: str | None, request: Any, definition: ToolDefinition,
    ) -> None:
        """提交持久 intent 后才触发 handler；未知窗口只允许人工复核。"""
        if not agent_run_id:
            raise ToolExecutionError("tool_outcome_uncertain", "工具缺少持久运行标识，已停止执行")
        async with runtime.sessions() as session:
            await generation.fence(session)
            await dependencies.require_runtime_chat_permission(session, principal)
            existing = await agent_runtime.runtime_store.get_tool_call(
                session, run_id=agent_run_id, call_id=request.call_id,
            )
            if existing is not None:
                raise ToolExecutionError(
                    "tool_outcome_uncertain", "已有持久工具记录，需复核后继续，系统未自动重放",
                    effect_status="unknown",
                )
            await agent_runtime.runtime_store.append_tool_call_once(
                session, run_id=agent_run_id, call_id=request.call_id, tool_name=request.name,
                arguments=request.arguments, status="running", timeout_ms=definition.timeout_ms,
                effect_status="unknown",
            )
            await agent_runtime.runtime_store.append_checkpoint(
                session, run_id=agent_run_id, node_name="tool_pending",
                state={"providerStarted": True, "providerOutcome": "known",
                       "pendingCallId": request.call_id, "pendingToolName": request.name,
                       "effectStatus": "unknown"},
                resume_policy="manual_review",
            )
            await session.commit()


    async def record_chat_tool_result(
        *,
        principal: Principal,
        request_id: str,
        agent_run_id: str | None,
        call_id: str,
        tool_name: str,
        arguments: dict[str, Any],
        definition: ToolDefinition | None,
        result: dict[str, Any],
    ) -> None:
        """同时写入 Agent Trace 和通用工具审计，避免聊天工具调用只存在于内存。"""
        status = str(result.get("status") or "failed")
        error_code = str(result.get("errorCode") or "")
        outcome = {
            "completed": "success",
            "rejected": "denied",
            "approval_required": "approval_required",
        }.get(status, "timeout" if error_code == "tool_timeout" else "failed")
        input_digest = agent_runtime.digest_payload(arguments, redact=False)
        output = result.get("result") if status == "completed" else None
        output_digest = agent_runtime.digest_payload(output) if output is not None else None
        # 普通图行的旧 output_digest 是 runtime 域；事件显式标注，兼容旧记录。
        async with runtime.sessions() as session:
            await generation.fence(session)
            if agent_run_id:
                previous = await agent_runtime.runtime_store.get_tool_call(
                    session, run_id=agent_run_id, call_id=call_id,
                )
                if previous is not None and previous.finished_at is not None:
                    effect = str(result.get("effectStatus") or (
                        "applied" if status == "completed" else "unknown" if status == "failed" else "none"))
                    if (previous.tool_name, previous.input_digest, previous.status,
                            previous.effect_status, previous.output_digest, previous.error_code or "") != (
                            tool_name, input_digest, status, effect, output_digest, error_code):
                        raise ToolExecutionError("tool_outcome_uncertain",
                            "持久工具结果与恢复结果冲突，需人工复核", effect_status="unknown")
                    # 结果与审计同事务提交；图节点重放只能补上下文，不能重复审计。
                    return
                if previous is None or (previous.tool_name, previous.input_digest) == (tool_name, input_digest):
                    await agent_runtime.runtime_store.append_tool_call_once(
                        session,
                        run_id=agent_run_id,
                        call_id=call_id,
                        tool_name=tool_name,
                        arguments=arguments,
                        status=status,
                        timeout_ms=definition.timeout_ms if definition is not None else 0,
                        effect_status=str(result.get("effectStatus") or (
                            "applied" if status == "completed" else "unknown" if status == "failed" else "none"
                        )),
                        result=output,
                        error_code=error_code,
                        finished_at=agent_runtime.utcnow(),
                    )
                await agent_runtime.runtime_store.append_event(
                    session,
                    run_id=agent_run_id,
                    node_name="record_tool_result",
                    event_type="tool_result",
                    status=status,
                    payload={
                        "callId": call_id,
                        "toolName": tool_name,
                        "status": status,
                        "errorCode": error_code,
                        "inputDigest": input_digest,
                        "outputDigest": output_digest or "",
                        "runtimeOutputDigest": output_digest or "",
                        "executionOutputDigest": result.get("executionOutputDigest") or result.get("outputDigest") or "",
                    },
                )
            session.add(ToolAudit(
                id=await dependencies.new_id(session),
                tenant_id=principal.tenant_id,
                user_id=principal.user_id,
                chat_id=int(request_id) if request_id.isdigit() else None,
                tool_name=tool_name,
                tool_type=definition.tool_type if definition is not None else "local",
                outcome=outcome,
                input_digest=input_digest,
                output_digest=output_digest,
                duration_ms=int(result.get("elapsedMs") or 0),
                error_message=str(result.get("message") or error_code)[:1000],
            ))
            await session.commit()

    return _RouteExports(
        finish_approved_tool=finish_approved_tool,
        begin_chat_tool_call=begin_chat_tool_call,
        record_chat_tool_result=record_chat_tool_result,
    )
