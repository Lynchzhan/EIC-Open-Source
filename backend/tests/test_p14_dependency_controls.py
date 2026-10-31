import asyncio
from datetime import timedelta
from types import SimpleNamespace

import pytest

from eic.core.dependency_controls import acquire, dependency_key, finish, DependencyDenied
from eic.core.outbox import now
from eic.models import DependencyCircuit
from test_id_generation_safety import fixture_db

SETTINGS = SimpleNamespace(circuit_failure_threshold=2, circuit_window_seconds=60,
                           circuit_open_seconds=30, circuit_probe_seconds=60)


def test_shared_single_probe_and_late_result_fenced(monkeypatch, tmp_path):
    async def run():
        async with fixture_db(monkeypatch, tmp_path) as sessions:
            key = dependency_key("provider", 1, "fixture", 201, "http://fixture")
            for _ in range(2):
                permit = await acquire(sessions, key, SETTINGS)
                assert await finish(sessions, permit, False, SETTINGS)
            with pytest.raises(DependencyDenied):
                await acquire(sessions, key, SETTINGS)
            async with sessions() as session:
                row = await session.get(DependencyCircuit, key)
                row.open_until = await now(session) - timedelta(seconds=1)
                await session.commit()
            results = await asyncio.gather(*(acquire(sessions, key, SETTINGS) for _ in range(4)),
                                            return_exceptions=True)
            probes = [item for item in results if not isinstance(item, Exception)]
            assert len(probes) == 1 and probes[0].state == "HALF_OPEN"
            assert await finish(sessions, probes[0], True, SETTINGS)
            assert not await finish(sessions, probes[0], False, SETTINGS)
            assert (await acquire(sessions, key, SETTINGS)).state == "CLOSED"
            assert (await acquire(sessions, dependency_key("tool", 1, model_id="calculator"), SETTINGS)).state == "CLOSED"
            assert (await acquire(sessions, dependency_key("provider", 2, "fixture", 201, "http://fixture"), SETTINGS)).state == "CLOSED"
    asyncio.run(run())


def test_expired_probe_cannot_close_circuit(monkeypatch, tmp_path):
    async def run():
        async with fixture_db(monkeypatch, tmp_path) as sessions:
            key = dependency_key("redis", 1, endpoint="redis://fixture")
            for _ in range(2):
                await finish(sessions, await acquire(sessions, key, SETTINGS), False, SETTINGS)
            async with sessions() as session:
                (await session.get(DependencyCircuit, key)).open_until = await now(session) - timedelta(seconds=1)
                await session.commit()
            permit = await acquire(sessions, key, SETTINGS)
            async with sessions() as session:
                (await session.get(DependencyCircuit, key)).probe_until = await now(session) - timedelta(seconds=1)
                await session.commit()
            assert not await finish(sessions, permit, True, SETTINGS)
            with pytest.raises(DependencyDenied):
                await acquire(sessions, key, SETTINGS)
            async with sessions() as session:
                assert (await session.get(DependencyCircuit, key)).state == "OPEN"
    asyncio.run(run())
