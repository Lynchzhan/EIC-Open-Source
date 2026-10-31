"""从应用实测对象提取无正文观测，不接受自报成功替代状态校验。"""
from .boundaries import HarnessError
from .metrics import number, percentile


def agent_observations(state, *, expected_answer, expected_tools, elapsed_ms, expected_stop="answer"):
    required = {"answer", "stop_reason", "tool_results", "tool_budget", "agent_step_count",
                "tool_call_count", "trace_events"}
    if not isinstance(state, dict) or not required <= set(state):
        raise HarnessError("Agent 状态不完整")
    steps, calls = state["agent_step_count"], state["tool_call_count"]
    if type(steps) is not int or type(calls) is not int or steps < 0 or calls < 0:
        raise HarnessError("Agent 计数无效")
    max_steps, max_calls = state["tool_budget"]["maxSteps"], state["tool_budget"]["maxToolCalls"]
    if type(max_steps) is not int or type(max_calls) is not int or min(max_steps, max_calls) < 1:
        raise HarnessError("Agent 预算无效")
    correct = state["answer"] == expected_answer and state["stop_reason"] == expected_stop
    rows = [{
        "taskSuccessRate": correct,
        "answerCorrectness": correct if expected_stop == "answer" else None,
        "answerCompleteness": correct if expected_stop == "answer" else None,
        "refusalAccuracy": correct if expected_stop != "answer" else None,
        "maxStepViolationRate": steps > max_steps or calls > max_calls,
        "latencyMs": number(elapsed_ms, "latencyMs"), "firstTokenMs": None,
        # 此适配器不拥有上游 usage，不把 model_decider 的返回延迟称作首 token。
        "inputTokens": None, "outputTokens": None, "providerCost": None,
    }]
    for tool in state["tool_results"]:
        if not isinstance(tool, dict) or not {"toolName", "status"} <= set(tool):
            raise HarnessError("工具观测不完整")
        if tool["status"] not in {"completed", "failed", "rejected", "approval_required"}:
            raise HarnessError("工具状态无效")
        code = tool.get("errorCode", "")
        rows.append({
            "toolSelectionAccuracy": tool["toolName"] in expected_tools,
            "toolArgumentValidity": None if tool["status"] in {"rejected", "approval_required"}
                and code != "invalid_tool_arguments" else code != "invalid_tool_arguments",
            "toolSuccessRate": tool["status"] == "completed",
            "uncertainOutcomeRate": tool.get("effectStatus") == "unknown",
            "retryCount": tool.get("retryCount") if "retryCount" in tool else None,
        })
    return rows


def node_metrics(events):
    timings = {}
    for event in events:
        if not isinstance(event, dict) or not isinstance(event.get("node"), str) or not event["node"]:
            raise HarnessError("节点观测结构无效")
        if event.get("elapsedMs") is not None:
            timings.setdefault(event["node"], []).append(number(event["elapsedMs"], "nodeLatencyMs"))
    return {node: {"count": len(values), "p50Ms": percentile(values, .5), "p95Ms": percentile(values, .95)}
            for node, values in sorted(timings.items())}


def verify_runtime_trace(run, events, checkpoints, tools):
    from eic.core.agent_runtime import digest_payload
    violations = []
    if not run.run_id or not run.trace_id or not run.graph_version:
        violations.append("run_identity_missing")
    if run.event_seq < 1 or run.checkpoint_seq < 1:
        violations.append("trace_evidence_missing")
    if sorted(event.sequence for event in events) != list(range(1, run.event_seq + 1)):
        violations.append("event_sequence_incomplete")
    if sorted(row.checkpoint_seq for row in checkpoints) != list(range(1, run.checkpoint_seq + 1)):
        violations.append("checkpoint_sequence_incomplete")
    for row in [*events, *checkpoints, *tools]:
        if row.run_id != run.run_id:
            violations.append("run_binding_mismatch")
    for row in events:
        if digest_payload(row.payload) != row.payload_hash:
            violations.append("event_digest_mismatch")
    for row in checkpoints:
        if digest_payload(row.state_json) != row.state_hash:
            violations.append("checkpoint_digest_mismatch")
    if len({row.call_id for row in tools}) != len(tools):
        violations.append("duplicate_tool_identity")
    latencies = []
    for row in tools:
        if row.finished_at is not None:
            latency = (row.finished_at - row.started_at).total_seconds() * 1000
            latencies.append(number(latency, "toolLatencyMs"))
    if run.status in {"completed", "failed", "cancelled", "manual_review"} and run.finished_at is None:
        violations.append("terminal_timestamp_missing")
    return {"valid": not violations, "violations": sorted(set(violations)),
            "eventCount": len(events), "checkpointCount": len(checkpoints), "toolCount": len(tools),
            "status": run.status, "graphVersion": run.graph_version,
            "toolLatenciesMs": latencies}
