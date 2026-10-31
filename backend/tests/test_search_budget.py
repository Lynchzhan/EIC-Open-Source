"""搜索预算使用真实 Account 记账逻辑；数据库与 HTTP 均替换为内存实现。"""
import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy
from types import SimpleNamespace
import time

import httpx
import pytest

from eic.ai import runtime_policy, search
from eic.ai.budget import ChatConfigurationError
from eic.core import dependency_controls


@pytest.fixture
def harness(monkeypatch):
    policy = {**runtime_policy.DEFAULTS, 'action': 'stop', 'tiers': [], 'maxCalls': 1}
    account = runtime_policy.Account(7, 8, 9, policy)
    row = SimpleNamespace(state={
        'policy': policy, 'history': {'calls': 0, 'tokens': 0, 'unknown': 0, 'seconds': 0},
        'tier': 0, 'entries': {}, 'reason': None,
    })

    class Session:
        async def commit(self):
            pass

    @asynccontextmanager
    async def sessions():
        yield Session()

    async def locked(session):
        return row, deepcopy(row.state)

    monkeypatch.setattr(runtime_policy, 'SessionLocal', sessions)
    monkeypatch.setattr(account, 'locked', locked)
    state = SimpleNamespace(account=account, row=row, requests=[], guard_calls=0,
                            deny=False, responder=None)

    @asynccontextmanager
    async def guard(*args, **kwargs):
        state.guard_calls += 1
        if state.deny:
            raise dependency_controls.DependencyDenied('circuit_open', 'fixture')
        yield {}

    async def respond(request):
        state.requests.append(request)
        if state.responder:
            return await state.responder(request)
        return httpx.Response(200, json={'results': [
            {'url': 'https://source.invalid/page', 'title': 'fixture', 'content': 'text'},
        ]})

    client_class = httpx.AsyncClient
    monkeypatch.setattr(search.httpx, 'AsyncClient', lambda **kwargs: client_class(
        **kwargs, transport=httpx.MockTransport(respond)))
    monkeypatch.setattr(dependency_controls, 'guard', guard)
    state.settings = SimpleNamespace(chat_search_backend='searxng',
        chat_search_endpoint='https://search.invalid', chat_search_timeout_seconds=10,
        chat_search_max_results=3, chat_search_max_chars=2000)
    token = runtime_policy.ACTIVE.set(account)
    try:
        yield state
    finally:
        runtime_policy.ACTIVE.reset(token)


async def invoke(h):
    return await search.independent_search('fixture', h.settings, tenant_id=7)


def assert_settled(h):
    entries = list(h.row.state['entries'].values())
    assert len(entries) == 1
    assert entries[0]['kind'] == 'search'
    assert entries[0]['modelId'] == 'search:searxng'
    assert entries[0]['status'] == 'settled'
    assert entries[0]['tokens'] == 0
    assert runtime_policy.Account.totals(h.row.state)['calls'] == 1


@pytest.mark.asyncio
async def test_exhausted_budget_never_enters_guard_or_sends(harness):
    h = harness
    h.row.state['history']['calls'] = 1
    for _ in range(2):
        with pytest.raises(runtime_policy.BudgetStopped):
            await invoke(h)
    assert h.guard_calls == 0 and h.requests == []
    assert h.row.state['entries'] == {}


@pytest.mark.asyncio
async def test_success_counts_once_and_blocks_next_search(harness):
    h = harness
    text, rows = await invoke(h)
    assert rows[0]['url'] in text
    assert_settled(h)
    with pytest.raises(runtime_policy.BudgetStopped):
        await invoke(h)
    assert len(h.requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['transport', 'status', 'empty'])
async def test_failed_search_is_settled_with_zero_model_tokens(harness, failure):
    h = harness

    async def fail(request):
        if failure == 'transport':
            raise httpx.ReadTimeout('fixture', request=request)
        return httpx.Response(503 if failure == 'status' else 200, json={'results': []})

    h.responder = fail
    with pytest.raises(ChatConfigurationError, match='search_empty' if failure == 'empty' else 'search_failed'):
        await invoke(h)
    assert len(h.requests) == 1
    assert_settled(h)


@pytest.mark.asyncio
async def test_cancel_after_send_settles_and_propagates(harness):
    h = harness
    sent = asyncio.Event()

    async def wait(request):
        sent.set()
        await asyncio.Future()

    h.responder = wait
    task = asyncio.create_task(invoke(h))
    await asyncio.wait_for(sent.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert_settled(h)


@pytest.mark.asyncio
async def test_denied_guard_releases_uninvoked_reservation(harness):
    h = harness
    h.deny = True
    with pytest.raises(ChatConfigurationError, match='search_failed'):
        await invoke(h)
    assert h.requests == [] and h.row.state['entries'] == {}
    h.deny = False
    await invoke(h)
    assert_settled(h)


@pytest.mark.asyncio
async def test_cancel_before_send_releases_reservation(harness, monkeypatch):
    h = harness
    entered = asyncio.Event()

    @asynccontextmanager
    async def waiting_guard(*args, **kwargs):
        entered.set()
        await asyncio.Future()
        yield {}

    monkeypatch.setattr(dependency_controls, 'guard', waiting_guard)
    task = asyncio.create_task(invoke(h))
    await asyncio.wait_for(entered.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert h.requests == [] and h.row.state['entries'] == {}


@pytest.mark.asyncio
async def test_tenant_mismatch_never_reserves_or_sends(harness):
    with pytest.raises(runtime_policy.BudgetStopped):
        await search.independent_search('fixture', harness.settings, tenant_id=10)
    assert harness.requests == [] and harness.row.state['entries'] == {}


@pytest.mark.asyncio
async def test_expired_deadline_never_sends_and_releases(harness):
    h = harness
    h.account.deadline = time.monotonic() - 1
    with pytest.raises(runtime_policy.BudgetStopped):
        await invoke(h)
    assert h.guard_calls == 0 and h.requests == [] and h.row.state['entries'] == {}


@pytest.mark.asyncio
async def test_budget_deadline_limits_inflight_request(harness):
    h = harness
    h.account.deadline = time.monotonic() + 0.02

    async def wait(request):
        await asyncio.Future()

    h.responder = wait
    with pytest.raises(runtime_policy.BudgetStopped):
        await asyncio.wait_for(invoke(h), 1)
    assert len(h.requests) == 1
    assert_settled(h)


@pytest.mark.asyncio
async def test_inflight_reservation_blocks_another_search(harness):
    h = harness
    sent, release = asyncio.Event(), asyncio.Event()

    async def wait(request):
        sent.set()
        await release.wait()
        return httpx.Response(200, json={'results': [{'url': 'https://source.invalid'}]})

    h.responder = wait
    task = asyncio.create_task(invoke(h))
    try:
        await asyncio.wait_for(sent.wait(), 1)
        with pytest.raises(runtime_policy.BudgetStopped):
            await invoke(h)
        assert len(h.requests) == 1
    finally:
        release.set()
        await task
    assert_settled(h)


@pytest.mark.asyncio
async def test_guard_budget_stopped_is_not_wrapped(harness, monkeypatch):
    @asynccontextmanager
    async def denied(*args, **kwargs):
        raise runtime_policy.BudgetStopped('fixture')
        yield {}

    monkeypatch.setattr(dependency_controls, 'guard', denied)
    with pytest.raises(runtime_policy.BudgetStopped, match='fixture'):
        await invoke(harness)
    assert harness.requests == [] and harness.row.state['entries'] == {}


@pytest.mark.asyncio
async def test_experience_without_active_account_keeps_existing_budget(harness):
    token = runtime_policy.ACTIVE.set(None)
    try:
        await invoke(harness)
    finally:
        runtime_policy.ACTIVE.reset(token)
    assert len(harness.requests) == 1 and harness.row.state['entries'] == {}
