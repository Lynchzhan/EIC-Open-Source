"""本地合成开发检查，不替代冻结正式评测。无网络、无业务数据。"""
import asyncio
from copy import deepcopy

import pytest

from eic.ai.runtime_policy import Account, normalize_policy, settle_call_state
from eic.core.background_dispatch import DispatchPool
from eic.core.security import encrypt_secret, encrypt_secret_if_changed, decrypt_secret
from eic.knowledge.retrieval_graph import compiled_retrieval_graph, RetrievalContext


def budget():
    return {"policy": normalize_policy({"action": "degrade", "tiers": [
        {"modelId": "1", "maxCalls": 2, "maxTokens": 1000},
        {"modelId": "2", "maxCalls": 2, "maxTokens": 1000}]}),
        "history": {"calls": 0, "tokens": 0, "unknown": 0, "seconds": 0},
        "entries": {}, "turns": {}, "tier": 0, "reason": None}


def entry(tier=0):
    return {"kind": "model", "tier": tier, "status": "pending", "tokens": 200, "seconds": 20}


def test_same_credential_retains_persistent_fingerprint():
    encrypted = encrypt_secret("synthetic-key")
    assert encrypt_secret_if_changed("synthetic-key", encrypted) == encrypted
    changed = encrypt_secret_if_changed("synthetic-key-2", encrypted)
    assert changed != encrypted
    assert decrypt_secret(changed) == "synthetic-key-2"


@pytest.mark.parametrize("sink,expected", [({}, None), ({"_invoked": True}, "unknown"),
    ({"_invoked": True, "total_tokens": 5}, "settled")])
def test_common_settlement_keeps_unknown_and_releases_uninvoked(sink, expected):
    state = budget()
    state["entries"]["call"] = entry()
    settle_call_state(state, "call", sink, 2, False, reason="tier")
    assert state["entries"].get("call", {}).get("status") == expected
    if expected == "unknown":
        assert Account.totals(state)["tokens"] == 200


def test_retired_tier_counts_toward_threshold():
    state = budget()
    state["history"]["tiers"] = {"0": {"calls": 1, "tokens": 5}}
    state["entries"]["call"] = entry()
    settle_call_state(state, "call", {"_invoked": True, "total_tokens": 6}, 2, False, reason="tier")
    assert state["tier"] == 1


def test_late_old_tier_does_not_advance_current_tier():
    state = budget()
    state["tier"] = 1
    state["entries"]["call"] = entry(0)
    settle_call_state(state, "call", {"_invoked": True, "total_tokens": 6}, 2, True, reason="tier")
    assert state["tier"] == 1


def test_compaction_keeps_aggregate_usage():
    state = budget()
    state["policy"]["action"] = "stop"
    for number in range(200):
        state["entries"][str(number)] = entry()
        settle_call_state(state, str(number), {"_invoked": True, "total_tokens": 3}, 1, False, reason="tier")
    assert len(state["entries"]) == 128
    assert Account.totals(state)["tokens"] == 600
    assert Account.totals(state)["calls"] == 200


@pytest.mark.asyncio
async def test_pool_bounds_deduplicates_and_does_not_block_scan():
    release = asyncio.Event()
    entered = []
    async def operation(key):
        entered.append(key)
        await release.wait()
    pool = DispatchPool(2)
    assert pool.submit(1, operation)
    assert not pool.submit(1, operation)
    assert pool.submit(2, operation)
    assert not pool.submit(3, operation)
    await asyncio.sleep(0)
    assert entered == [1, 2] and pool.available == 0
    release.set()
    await asyncio.gather(*pool.active.values())
    await asyncio.sleep(0)
    assert pool.available == 2
    await pool.close()
    assert not pool.submit(3, operation)


@pytest.mark.asyncio
async def test_pool_shutdown_waits_for_consumer_cleanup():
    finalized = []
    async def operation(key):
        try:
            await asyncio.Event().wait()
        finally:
            finalized.append(key)
    pool = DispatchPool(1)
    pool.submit(1, operation)
    await asyncio.sleep(0)
    await pool.close()
    assert finalized == [1] and pool.active == {}


@pytest.mark.asyncio
async def test_retrieval_template_reuse_has_no_cross_request_state():
    graph = compiled_retrieval_graph()
    assert graph is compiled_retrieval_graph()
    assert graph.checkpointer is None
    async def run(marker):
        async def step(state):
            await asyncio.sleep(0)
            return {"envelope": {"marker": marker}}
        context = RetrievalContext(marker, {name: step for name in
            ("authorize", "dense_recall", "sparse_recall", "fusion", "rerank", "citations")})
        result = await graph.ainvoke({}, config={"callbacks": []}, context=context)
        return result["envelope"]["marker"]
    assert await asyncio.gather(*(run(marker) for marker in range(10))) == list(range(10))


@pytest.mark.asyncio
async def test_background_foreground_settlement_match(monkeypatch):
    from contextlib import asynccontextmanager
    from types import SimpleNamespace
    from eic.ai import runtime_policy as rp
    from eic.core.context_jobs import _BackgroundAccount
    original = budget()
    original["history"]["tiers"] = {"0": {"calls": 1, "tokens": 5}}
    original["entries"]["call"] = entry()
    class Session:
        async def commit(self):
            pass
    @asynccontextmanager
    async def sessions():
        yield Session()
    monkeypatch.setattr(rp, "SessionLocal", sessions)
    foreground = Account(1, 2, 3, original["policy"])
    background = _BackgroundAccount(sessions, 1, 2, 3, original["policy"])
    rows = []
    for account in (foreground, background):
        row = SimpleNamespace(state=deepcopy(original))
        if account is background:
            row.state["history"]["seconds"] = 20
            row.state["entries"]["call"]["backgroundSecondsReserved"] = 20
        async def locked(session, row=row):
            return row, deepcopy(row.state)
        monkeypatch.setattr(account, "locked", locked)
        await account.settle("call", {"_invoked": True, "total_tokens": 5}, 3, False)
        rows.append(row.state)
    assert rows[0]["entries"] == rows[1]["entries"]
    assert rows[0]["tier"] == rows[1]["tier"] == 1
    assert rows[1]["history"]["seconds"] == 3


@pytest.mark.asyncio
async def test_followups_isolate_enqueue_failure(monkeypatch):
    from contextlib import asynccontextmanager
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from eic.ai import chat_followups as followups
    from eic.ai import memory_candidates
    from eic.core.authentication import Principal
    class Session:
        async def commit(self):
            pass
    @asynccontextmanager
    async def sessions():
        yield Session()
    compact = AsyncMock(side_effect=ValueError("synthetic business failure"))
    extract = AsyncMock(return_value=123)
    save = AsyncMock(return_value=True)
    monkeypatch.setattr(followups.conversation_context, "stage_compaction", compact)
    monkeypatch.setattr(memory_candidates, "stage_extraction", extract)
    turn = followups.CompletedTurn(Principal(1, 2, "fixture", False), 3, 4, "request",
        {"contextSettings": {"automatic": True}, "memorySettings": {"mode": "review"}},
        {"memoryWrite": {"content": "synthetic"}}, True, False)
    await followups.stage_context_jobs(SimpleNamespace(sessions=sessions), turn,
        estimated_input=1000, input_limit=1000, memory_save=save)
    compact.assert_awaited_once()
    extract.assert_awaited_once()
    save.assert_awaited_once()


@pytest.mark.asyncio
async def test_history_projection_retains_order_hash_and_trim_metadata(tmp_path):
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from eic.ai import conversation_context as context
    from eic.models import Base, ChatMessage, ChatSession, Tenant, User
    engine = create_async_engine("sqlite+aiosqlite:///" + (tmp_path / "history.db").as_posix())
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with sessions() as session:
            session.add(Tenant(id=1, code=1, name="synthetic"))
            session.add(User(id=2, tenant_id=1, username="synthetic", password_hash="unused"))
            session.add(ChatSession(id=3, tenant_id=1, owner_id=2, title="synthetic"))
            for number in range(1, 21):
                session.add(ChatMessage(id=number + 10, tenant_id=1, session_id=3,
                    role="user" if number % 2 else "assistant", status="completed",
                    request_id=str((number + 1) // 2), content=f"message-{number}",
                    token_usage={"unused-large-payload": "x" * 1000}))
            await session.commit()
        async with sessions() as session:
            rows = await context._rows(session, 1, 3, before=31, limit=100)
            ids, fingerprint = await context._prefix_fingerprint(session, 1, 3, 30)
            assert ids == [row.id for row in rows]
            assert fingerprint == context._source_hash(rows)
            from eic.core.memory_lifecycle import forget_epoch
            epoch, _ = await forget_epoch(session, 1, 2)
            metadata = {"sourceIds": ids, "sourceHash": fingerprint, "forgetEpoch": epoch}
            assert await context.history_snapshot_valid(session, 1, 2, 3, metadata)
            assert "token_usage" not in rows[0].__dict__
            history, summary, details = await context.load_history(session,
                tenant_id=1, owner_id=2, session_id=3, before_id=31, config={})
            assert len(history) == 20 and summary is None
            assert details["sourceIds"] == ids
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_background_scan_stays_responsive_and_closes_active_work(monkeypatch, tmp_path):
    from unittest.mock import AsyncMock
    from test_transactional_outbox import fixture_db
    from eic.core import outbox
    from eic.knowledge import indexing
    entered, recovered, finalized = [], asyncio.Event(), []
    async def slow_dispatch(key):
        entered.append(key)
        try:
            await asyncio.Event().wait()
        finally:
            finalized.append(key)
    async def recover(**kwargs):
        recovered.set()
    async with fixture_db(monkeypatch, tmp_path) as (sessions, engine):
        async with sessions() as session:
            ids = [await outbox.stage(session, "memory-write", number, 1) for number in (1, 2, 3)]
            await session.commit()
        monkeypatch.setattr(outbox, "dispatch_one", slow_dispatch)
        monkeypatch.setattr(outbox, "recover_consumers", recover)
        monkeypatch.setattr(outbox, "recover_task_runs", AsyncMock())
        monkeypatch.setattr(indexing, "recover_index_runs", AsyncMock())
        monkeypatch.setattr(outbox.get_settings(), "outbox_dispatch_kinds", [])
        monkeypatch.setattr(outbox.get_settings(), "outbox_inline_concurrency", 2)
        loop = asyncio.create_task(outbox.dispatch_loop())
        await asyncio.wait_for(recovered.wait(), timeout=2)
        await asyncio.sleep(0)
        assert entered == ids[:2]
        loop.cancel()
        with pytest.raises(asyncio.CancelledError):
            await loop
        assert finalized == entered


@pytest.mark.asyncio
async def test_scheduler_excludes_already_active_ids(monkeypatch, tmp_path):
    from test_transactional_outbox import fixture_db
    from eic.core import outbox
    async def slow_dispatch(key):
        await asyncio.Event().wait()
    async with fixture_db(monkeypatch, tmp_path) as (sessions, engine):
        async with sessions() as session:
            ids = [await outbox.stage(session, "memory-write", number, 1) for number in (1, 2, 3)]
            await session.commit()
        monkeypatch.setattr(outbox, "dispatch_one", slow_dispatch)
        pool = DispatchPool(2)
        pool.submit(ids[0], slow_dispatch)
        assert await outbox.dispatch_pending(scheduler=pool) == 1
        assert set(pool.active) == set(ids[:2])
        assert await outbox.dispatch_pending(scheduler=pool) == 0
        await pool.close()
