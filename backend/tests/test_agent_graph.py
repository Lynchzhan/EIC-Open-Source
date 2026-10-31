from eic.ai.agent_graph import plan_chat_execution


def test_agent_graph_deduplicates_server_approved_skills_and_uses_model_route() -> None:
    plan = plan_chat_execution(
        "请分析这份报告",
        model_available=True,
        allowed_tools=["知识库检索", "知识库检索", "单位换算"],
    )

    assert plan["route"] == "model"
    assert plan["allowed_tools"] == ["知识库检索", "单位换算"]
    assert "2 个" in plan["reasoning"]
    assert plan["agent_run_id"] and plan["trace_id"]
    assert [event["node"] for event in plan["trace_events"]] == [
        "prepare_context", "rewrite_query", "prepare_retrieval", "choose_route",
    ]


def test_agent_graph_has_an_explicit_no_model_route() -> None:
    plan = plan_chat_execution("你好", model_available=False, allowed_tools=[])

    assert plan["route"] == "no_model"


def test_agent_graph_rewrites_query_and_caps_retrieval_context() -> None:
    evidence = [{"chunkId": str(index), "content": f"证据 {index}"} for index in range(12)]

    plan = plan_chat_execution(
        "  企业  分析\n",
        model_available=True,
        allowed_tools=[],
        retrieved_documents=evidence,
    )

    assert plan["rewritten_query"] == "企业 分析"
    assert len(plan["retrieved_documents"]) == 8
    assert plan["retrieved_documents"][0]["chunkId"] == "0"
    assert plan["trace_events"][-1]["payload"]["documentCount"] == 8 or plan["trace_events"][-1]["node"] == "choose_route"
