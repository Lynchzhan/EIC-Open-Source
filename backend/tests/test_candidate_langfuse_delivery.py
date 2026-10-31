"""真实隔离 SQLite 与投递桩；不接入真实 Langfuse。"""
import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from eic.core import langfuse_delivery as delivery, langfuse_reconcile as reconcile
from eic.models import LangfuseDelivery, ObservabilityEvent, Resource
from test_index_lifecycle import fixture

TARGET = "a" * 64


def exporter(monkeypatch, outcome="acknowledged"):
    monkeypatch.setattr(delivery, "destination", lambda _: TARGET)
    return SimpleNamespace(_accepting=True, _environment="test", _namespace="fixture",
        _build=lambda row, **kw: [{"id": row.get("stable_id", row["event_key"])}],
        deliver_persisted=AsyncMock(return_value=outcome))


async def staged(sessions, sender, key="fixture", stable_id=None):
    async with sessions() as s:
        s.add(ObservabilityEvent(event_key=key, tenant_id=1, category="agent_run", stage="terminal",
            status="completed", source="fixture", occurred_at=datetime.now(UTC), summary={}))
        await delivery.stage(s, {"event_key": key, "stable_id": stable_id or key}, sender)
        await s.commit()


def test_stage_is_transactional_and_export_identity_is_unique(tmp_path, monkeypatch):
    async def run():
        async with fixture(tmp_path, monkeypatch, embedding=False) as sessions:
            sender = exporter(monkeypatch)
            await staged(sessions, sender, "one", "same-span")
            await staged(sessions, sender, "two", "same-span")
            async with sessions() as s:
                assert len(list(await s.scalars(select(LangfuseDelivery)))) == 1
                assert await s.get(ObservabilityEvent, "two") is not None
                await delivery.stage(s, {"event_key": "rollback"}, sender)
                await s.rollback()
                assert await s.get(LangfuseDelivery, ("rollback", TARGET)) is None
            assert await delivery.dispatch_one(sessions, sender)
            assert not await delivery.dispatch_one(sessions, sender)
            assert sender.deliver_persisted.await_count == 1
            async with sessions() as s:
                value = await s.get(LangfuseDelivery, ("one", TARGET))
                assert value.status == "acknowledged" and value.attempts == 1
    asyncio.run(run())


def test_rate_limit_retry_has_finite_attempts(tmp_path, monkeypatch):
    async def run():
        async with fixture(tmp_path, monkeypatch, embedding=False) as sessions:
            sender = exporter(monkeypatch, "retryable")
            await staged(sessions, sender)
            for attempt in range(1, 6):
                assert await delivery.dispatch_one(sessions, sender)
                async with sessions() as s:
                    row = await s.get(LangfuseDelivery, ("fixture", TARGET))
                    assert row.attempts == attempt
                    assert row.status == ("pending" if attempt < 5 else "rejected")
                    row.next_attempt_at = datetime.now(UTC) - timedelta(seconds=1)
                    await s.commit()
            assert not await delivery.dispatch_one(sessions, sender)
            assert sender.deliver_persisted.await_count == 5
    asyncio.run(run())


@pytest.mark.parametrize("outcome", ["unconfirmed", "cancelled"])
def test_unknown_and_expired_lease_are_not_automatically_replayed(tmp_path, monkeypatch, outcome):
    async def run():
        async with fixture(tmp_path, monkeypatch, embedding=False) as sessions:
            sender = exporter(monkeypatch, outcome)
            await staged(sessions, sender)
            if outcome == "cancelled":
                sender.deliver_persisted.side_effect = asyncio.CancelledError
                with pytest.raises(asyncio.CancelledError):
                    await delivery.dispatch_one(sessions, sender)
                async with sessions() as s:
                    row = await s.get(LangfuseDelivery, ("fixture", TARGET))
                    assert row.status == "sending" and row.attempts == 1
                    row.lease_until = datetime.now(UTC) - timedelta(seconds=1)
                    await s.commit()
            else:
                assert await delivery.dispatch_one(sessions, sender)
            assert not await delivery.dispatch_one(sessions, sender)
            assert sender.deliver_persisted.await_count == 1
            async with sessions() as s:
                assert (await s.get(LangfuseDelivery, ("fixture", TARGET))).status == "unconfirmed"
    asyncio.run(run())


def test_manual_reconciliation_checks_cas_and_audits_without_sending(tmp_path, monkeypatch):
    async def run():
        async with fixture(tmp_path, monkeypatch, embedding=False) as sessions:
            monkeypatch.setattr(reconcile, "SessionLocal", sessions)
            sender = exporter(monkeypatch, "unconfirmed")
            await staged(sessions, sender)
            await delivery.dispatch_one(sessions, sender)
            listing = await reconcile.execute(SimpleNamespace(command="list", destination=TARGET, limit=10))
            args = SimpleNamespace(command="resolve", destination=TARGET, event_key="fixture",
                actor="isolated-test", reason="synthetic receipt", evidence="fixture://receipt",
                expected_status="unconfirmed", expected_updated_at=listing[0]["updatedAt"],
                decision="retry_uncertain", allow_duplicate_risk=False)
            with pytest.raises(ValueError, match="重复投递风险"):
                await reconcile.execute(args)
            args.decision = "confirm_received"
            result = await reconcile.execute(args)
            assert result["networkRequestPerformed"] is False and result["attemptsReset"] is False
            with pytest.raises(ValueError, match="已变化"):
                await reconcile.execute(args)
            async with sessions() as s:
                row = await s.get(LangfuseDelivery, ("fixture", TARGET))
                assert row.status == "acknowledged" and row.attempts == 1
                audits = list(await s.scalars(select(Resource).where(Resource.resource_type == "langfuseDeliveryReviews")))
                assert len(audits) == 1 and audits[0].data["attempts"] == 1
            assert sender.deliver_persisted.await_count == 1
    asyncio.run(run())
