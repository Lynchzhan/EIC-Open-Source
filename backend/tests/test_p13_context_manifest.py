import json

import pytest

from eic.ai.budget import ChatConfigurationError, message_units
from eic.ai.context_manifest import bounded_tool_result, prepare_context, working_manifest


def test_optional_evidence_trim_keeps_system_question_and_complete_turns():
    system = [{"role": "system", "content": "系统约束"}]
    history = [
        {"role": "assistant", "content": "孤立回答"},
        {"role": "user", "content": "old" * 200},
        {"role": "assistant", "content": "old-answer"},
        {"role": "user", "content": "new"},
        {"role": "assistant", "content": "new-answer"},
        {"role": "user", "content": "孤立问题"},
    ]
    optional = [
        {"id": 1, "source": "knowledge", "priority": 70, "content": "巨大证据" * 500},
        {"id": 2, "source": "memory", "priority": 60, "content": "中文偏好"},
    ]
    messages, info = prepare_context(system, history, "当前问题", 260, optional=optional)
    assert messages[0] == system[0]
    assert messages[-1] == {"role": "user", "content": "当前问题"}
    assert [item["content"] for item in messages[2:-1]] == ["new", "new-answer"]
    assert message_units(messages) <= 260
    manifest = info["contextManifest"]
    assert manifest["exactTokenizer"] is False
    assert manifest["entries"][1]["trimReason"] == "optional_context_budget"
    assert all("content" not in item for item in manifest["entries"])
    assert len(manifest["hash"]) == 64


def test_tool_schema_reserved_and_mandatory_context_rejected():
    tools = [{"type": "function", "function": {"name": "calculator", "description": "工具" * 10}}]
    messages, info = prepare_context(
        [{"role": "system", "content": "system"}], [], "current", 300, tools=tools,
    )
    assert info["inputEstimate"] == message_units(messages, tools)
    assert info["contextManifest"]["toolSchemaReserve"] > 0
    with pytest.raises(ChatConfigurationError):
        prepare_context([{"role": "system", "content": "system" * 100}], [], "current", 200)


def test_tool_output_truncation_is_valid_json_and_byte_bounded():
    for content in ["中文" * 10000, '"' * 10000, "\\n" * 10000]:
        text = bounded_tool_result({"status": "completed", "result": content})
        assert len(text.encode()) <= 4096
        value = json.loads(text)
        assert value["truncated"] is True
        assert len(value["originalHash"]) == 64


def test_working_manifest_includes_tool_protocol_and_no_raw_content():
    messages = [
        {"role": "assistant", "content": None, "tool_calls": [{
            "id": "call1", "type": "function",
            "function": {"name": "calculator", "arguments": '{"a":1,"b":2}'},
        }]},
        {"role": "tool", "tool_call_id": "call1", "content": '{"value":3}'},
    ]
    manifest = working_manifest(messages, [], 1024)
    assert manifest["inputEstimate"] == message_units(messages)
    assert manifest["entries"][1]["source"] == "tool_result"
    assert manifest["entries"][1]["sourceId"] == "call1"
    assert not any("content" in row for row in manifest["entries"])
