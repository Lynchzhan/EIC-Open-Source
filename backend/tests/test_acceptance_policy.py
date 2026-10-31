"""P0-09：真实临时 SQLite 中的预算、档位切换及超时，无真实模型。"""
import asyncio
from copy import deepcopy
from types import SimpleNamespace

import pytest

from test_id_generation_safety import fixture_db
from eic.ai import runtime_policy as rp
from eic.core.observability import observe_model_call
from eic.models import ConversationBudget


def test_stop_policy_keeps_invalid_draft_without_validating_or_using_model_id():
    policy = rp.normalize_policy({"action": "stop", "tiers": [{"modelId": "removed-model"}]})
    assert policy["action"] == "stop"
    with pytest.raises(ValueError, match="模型 ID"):
        rp.normalize_policy({"action": "degrade", "tiers": policy["tiers"]})


@pytest.mark.parametrize("outcome", ["failed", "call-threshold", "token-threshold"])
async def test_tiers_advance_once_and_survive_account_recreation(monkeypatch, tmp_path, outcome):
    async with fixture_db(monkeypatch, tmp_path) as sessions:
        monkeypatch.setattr(rp, "SessionLocal", sessions)
        policy = rp.normalize_policy({"action": "degrade", "tiers": [
            {"modelId": "201", "maxCalls": 1 if outcome == "call-threshold" else 10,
             "maxTokens": 10 if outcome == "token-threshold" else 1000},
            {"modelId": "202", "maxCalls": 10, "maxTokens": 1000},
        ]})
        account = rp.Account(1, 11, 101, deepcopy(policy))
        assert await account.choose(999) == 201
        key, _ = await account.reserve(SimpleNamespace(id=201), "model", 100)
        await account.settle(key, {"_invoked": True, "total_tokens": 10}, .01, outcome == "failed")
        restored = rp.Account(1, 11, 101, deepcopy(policy))
        assert await restored.choose(999) == 202
        async with sessions() as session:
            row = await session.get(ConversationBudget, 101)
            assert row.state["tier"] == 1
            assert len(row.state["entries"]) == 1
            assert rp.Account.totals(row.state)["calls"] == 1


async def test_call_limit_rejects_before_second_provider_reservation(monkeypatch, tmp_path):
    async with fixture_db(monkeypatch, tmp_path) as sessions:
        monkeypatch.setattr(rp, "SessionLocal", sessions)
        policy = rp.normalize_policy({"action": "stop", "maxCalls": 1})
        account = rp.Account(1, 11, 101, policy)
        key, _ = await account.reserve(SimpleNamespace(id=201), "model", 100)
        await account.settle(key, {"_invoked": True, "total_tokens": 10}, .01, False)
        restored = rp.Account(1, 11, 101, deepcopy(policy))
        with pytest.raises(rp.BudgetStopped) as error:
            await restored.reserve(SimpleNamespace(id=201), "model", 100)
        assert error.value.reason == "conversation_call_limit"
        async with sessions() as session:
            row = await session.get(ConversationBudget, 101)
            assert len(row.state["entries"]) == 1


async def test_provider_timeout_cancels_and_preserves_unknown_reservation(monkeypatch, tmp_path):
    async with fixture_db(monkeypatch, tmp_path) as sessions:
        monkeypatch.setattr(rp, "SessionLocal", sessions)
        policy = rp.normalize_policy({"callTimeoutSeconds": 1})
        account = rp.Account(1, 11, 101, policy)
        token = rp.ACTIVE.set(account)
        closed = []

        @observe_model_call("embedding")
        async def stalled(model, texts, *, usage_sink):
            usage_sink["_invoked"] = True
            try:
                await asyncio.sleep(10)
            finally:
                closed.append(True)

        try:
            with pytest.raises(TimeoutError):
                await stalled(SimpleNamespace(id=201, tenant_id=1, model_name="synthetic"), ["fixture"])
        finally:
            rp.ACTIVE.reset(token)
        assert closed == [True]
        async with sessions() as session:
            row = await session.get(ConversationBudget, 101)
            assert len(row.state["entries"]) == 1
            entry = next(iter(row.state["entries"].values()))
            assert entry["status"] == "unknown" and entry["tokens"] > 0
