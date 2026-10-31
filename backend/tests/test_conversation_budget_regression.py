"""预算误拦截回归；所有 HTTP 和数据库均使用内存替身。"""
from contextlib import asynccontextmanager
from copy import deepcopy
from types import SimpleNamespace

import pytest

from eic.ai import runtime_policy as rp
from eic.core.observability import observe_model_call


def state():
    return {"policy": rp.normalize_policy({}), "history": {"calls": 0, "tokens": 0, "seconds": 0, "unknown": 0},
            "entries": {}, "tier": 0, "reason": None}


@pytest.fixture
def account(monkeypatch):
    row = SimpleNamespace(state=state())
    account = rp.Account(7, 8, 9, deepcopy(row.state["policy"]))

    class Session:
        async def commit(self):
            pass

    @asynccontextmanager
    async def sessions():
        yield Session()

    async def locked(session):
        return row, deepcopy(row.state)

    monkeypatch.setattr(rp, "SessionLocal", sessions)
    monkeypatch.setattr(account, "locked", locked)
    token = rp.ACTIVE.set(account)
    yield account, row
    rp.ACTIVE.reset(token)


def model():
    return SimpleNamespace(id=3, tenant_id=7, max_input_tokens=131072, max_output_tokens=4096)


@pytest.mark.asyncio
@pytest.mark.parametrize("keyword", [False, True])
async def test_24_rerank_candidates_do_not_reserve_25_context_windows(account, keyword):
    a, row = account
    hits = [SimpleNamespace(payload={"content": "知识库内容" * 100}) for _ in range(24)]
    inputs = () if keyword else ("你是什么模型", hits)
    kwargs = {"query": "你是什么模型", "hits": hits} if keyword else {}
    expected = rp.input_reservation(model(), "rerank", inputs, kwargs)
    assert 30000 < expected < 100000
    ticket = await rp.begin_call(model(), "rerank", inputs, kwargs)
    assert row.state["entries"][ticket[1]]["tokens"] == expected
    await rp.finish_call(ticket, {"_invoked": True, "total_tokens": 507}, False)
    assert rp.Account.totals(row.state)["tokens"] == 507


@pytest.mark.asyncio
async def test_embedding_batch_uses_real_text_bytes_and_keeps_unknown_reservation(account):
    a, row = account
    ticket = await rp.begin_call(model(), "embedding", (["中文问题"] * 8,))
    reserved = row.state["entries"][ticket[1]]["tokens"]
    assert 0 < reserved < 1000
    await rp.finish_call(ticket, {"_invoked": True}, False)
    assert rp.Account.totals(row.state)["tokens"] == reserved
    assert rp.Account.totals(row.state)["unknown"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value,code", [
    ("tokens", 999999, "conversation_token_limit"),
    ("calls", 100, "conversation_call_limit"),
    ("seconds", 1800, "conversation_time_limit"),
    ("unknown", 1, "conversation_usage_incomplete"),
])
async def test_budget_reasons_are_distinct_and_no_entry_is_created(account, field, value, code):
    a, row = account
    row.state["history"][field] = value
    with pytest.raises(rp.BudgetStopped) as error:
        await a.reserve(model(), "rerank", 200)
    assert error.value.reason == code
    assert row.state["reasonCode"] == code
    assert row.state["entries"] == {}
    assert row.state["history"][field] == value


@pytest.mark.asyncio
async def test_unknown_usage_is_not_zero_and_long_input_still_blocks(account):
    a, row = account
    hits = [SimpleNamespace(payload={"content": "x" * 1000000})]
    with pytest.raises(rp.BudgetStopped) as error:
        await rp.begin_call(model(), "rerank", ("Q", hits))
    assert error.value.reason == "conversation_token_limit"
    assert row.state["blocked"]["requestedTokens"] > 1000000
    assert not row.state["entries"]


@pytest.mark.asyncio
async def test_observer_passes_keyword_inputs_and_clears_stale_denial(account):
    a, row = account
    row.state.update(reason="old", reasonCode="conversation_token_limit", blocked={"old": 1})

    @observe_model_call("embedding")
    async def invoke(model, texts, *, usage_sink):
        usage_sink.update(_invoked=True, total_tokens=5)
        return [1]

    assert await invoke(model(), texts=["hello"]) == [1]
    assert row.state["reason"] is None and row.state["blocked"] is None
    assert rp.Account.totals(row.state)["tokens"] == 5


def test_approved_raise_keeps_history_calls_tiers_and_higher_limits():
    original = state()
    original["policy"]["maxTokens"] = 100000
    original["history"].update(tokens=527, calls=2, unknown=1)
    original["entries"]["old"] = {"status": "unknown", "tokens": 1000}
    raised = rp.raise_token_budget(original, 1000000, app_id=123)
    assert original["policy"]["maxTokens"] == 100000
    assert raised["policy"]["maxTokens"] == 1000000
    assert raised["history"] == original["history"]
    assert raised["entries"] == original["entries"]
    assert raised["tier"] == original["tier"]
    assert raised["policy"]["maxOutputTokens"] == original["policy"]["maxOutputTokens"]
    assert rp.raise_token_budget(raised, 1000000, app_id=123) == raised
    raised["policy"]["maxTokens"] = 2000000
    assert rp.raise_token_budget(raised, 1000000, app_id=123) == raised


@pytest.mark.asyncio
async def test_exhausted_tier_has_specific_reason(account):
    a, row = account
    row.state["policy"].update(action="degrade", tiers=[{"modelId": "3", "maxCalls": 10, "maxTokens": 10000}])
    row.state["tier"] = 1
    with pytest.raises(rp.BudgetStopped) as error:
        await a.choose(3)
    assert error.value.reason == "conversation_tiers_exhausted"


@pytest.mark.asyncio
async def test_uninvoked_call_removes_reservation(account):
    a, row = account
    ticket = await rp.begin_call(model(), "embedding", (["hello"],))
    await rp.finish_call(ticket, {}, True)
    assert not row.state["entries"]
