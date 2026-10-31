import pytest

from eic.ai.tool_center import ToolCenter, ToolDefinition, validate_mcp_config
from eic.knowledge.retrieval import lexical_search, rerank, rrf_fuse
from eic.vector.qdrant_reader import VectorHit


def test_hybrid_rrf_and_rerank_pipeline() -> None:
    vector = [VectorHit("a", 0.9, {"content": "enterprise intelligence"}), VectorHit("b", 0.8, {"content": "other"})]
    keyword = lexical_search("enterprise", vector)
    fused = rrf_fuse([vector, keyword])
    assert fused[0].point_id == "a"
    assert rerank("enterprise", fused, lambda query, content: 1 if query in content else 0)[0].point_id == "a"


def test_tool_center_enforces_allowlist_and_mcp_boundary() -> None:
    center = ToolCenter()
    center.register(ToolDefinition("calculator", "calculate", lambda args: {"value": args["a"] + args["b"]}))
    assert center.execute("calculator", {"a": 1, "b": 2}, {"calculator"}) == {"value": 3}
    with pytest.raises(PermissionError):
        center.execute("calculator", {"a": 1, "b": 2}, set())
    assert validate_mcp_config({"transport": "sse", "endpoint": "https://mcp.example/sse"})["transport"] == "sse"
    with pytest.raises(ValueError):
        validate_mcp_config({"transport": "stdio", "endpoint": "ignored"})
