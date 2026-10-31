"""Real SQLite failure injection; no paid providers, real tasks or external services."""
import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import BackgroundTasks
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from eic import main, workers
from eic.core import outbox, queue
from eic.core.ids import initialize_allocator
from eic.knowledge import indexing
from eic.models import (Base, KnowledgeBase, KnowledgeDocument, KnowledgeIngestionRun,
                        AIModel, OutboxEvent, Resource, ScheduledTask, TaskRun, Tenant, User)
from test_index_lifecycle import projection

PRINCIPAL = main.Principal(11, 1, "outbox-fixture", True)


@asynccontextmanager
async def fixture_db(monkeypatch, tmp_path):
    engine = create_async_engine("sqlite+aiosqlite:///" + (tmp_path / "fixture.db").as_posix(),
                                 connect_args={"timeout": 30})
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with sessions() as session:
        session.add(Tenant(id=1, code=1, name="synthetic"))
        await session.flush()
        session.add(User(id=11, tenant_id=1, username="fixture", password_hash="no-login", is_super_admin=True))
        await session.flush()
        session.add(Resource(id=100, tenant_id=1, owner_id=11, name="fixture",
                             resource_type="knowledge_bases", data={"indexStatus": "ready"}))
        session.add(KnowledgeBase(id=100, tenant_id=1, name="fixture"))
        session.add(ScheduledTask(id=200, tenant_id=1, owner_id=11, name="fixture",
                                  cron="* * * * *", payload={"handlerType": "knowledge-check", "knowledge_base_id": 100}))
        await session.flush()
        await initialize_allocator(session)
        await session.commit()
    for module in (main, outbox, workers, indexing):
        monkeypatch.setattr(module, "SessionLocal", sessions)
    monkeypatch.setattr(main, "ensure_permission", AsyncMock())
    monkeypatch.setattr(outbox, "_inline_kinds", lambda *_: {"knowledge-document", "document-embeddings"})
    monkeypatch.setattr(queue, "task_capabilities", AsyncMock(return_value={"execution": True, "reason": ""}))
    monkeypatch.setattr(workers.get_settings(), "task_execution_enabled", True)
    try:
        yield sessions, engine
    finally:
        await engine.dispose()


class CommitFault(RuntimeError):
    pass


def fault_commit(session):
    async def fail():
        raise CommitFault("synthetic-first-commit")
    session.commit = fail


async def count(session, model):
    return await session.scalar(select(func.count()).select_from(model))


def publisher(monkeypatch, *, unavailable=False):
    attempts, jobs = [], set()
    async def publish(event_id, tenant_id, **kwargs):
        key = f"eic-outbox:{tenant_id}:{event_id}"
        attempts.append(key)
        if unavailable:
            return None
        jobs.add(key)
        return key
    monkeypatch.setattr(queue, "enqueue_outbox", publish)
    return attempts, jobs


def test_kb_commit_failure_never_publishes(monkeypatch, tmp_path):
    attempts, _ = publisher(monkeypatch)
    async def run():
        async with fixture_db(monkeypatch, tmp_path) as (sessions, _):
            async with sessions() as session:
                fault_commit(session)
                with pytest.raises(CommitFault):
                    await main.knowledge_queue_index(100, BackgroundTasks(), PRINCIPAL, session)
                await session.rollback()
            async with sessions() as session:
                assert (await session.get(Resource, 100)).data == {"indexStatus": "ready"}
                assert await count(session, OutboxEvent) == 0
            assert attempts == []
    asyncio.run(run())


def test_kb_queue_failure_keeps_recoverable_same_intent(monkeypatch, tmp_path):
    attempts, _ = publisher(monkeypatch, unavailable=True)
    async def run():
        async with fixture_db(monkeypatch, tmp_path) as (sessions, _):
            async with sessions() as session:
                background = BackgroundTasks()
                result = await main.knowledge_queue_index(100, background, PRINCIPAL, session)
            assert result["queued"] and attempts == []
            await background()
            assert len(attempts) == 1
            async with sessions() as session:
                event = await session.scalar(select(OutboxEvent))
                original = event.id
                assert event.status == "pending" and event.last_error == "publish_unavailable"
            recovery_attempts, jobs = publisher(monkeypatch)
            assert await outbox.dispatch_one(original)
            async with sessions() as session:
                assert await count(session, OutboxEvent) == 1
                assert (await session.get(Resource, 100)).data["indexStatus"] == "queued"
            assert recovery_attempts == attempts and len(jobs) == 1
    asyncio.run(run())


def test_task_first_commit_failure_has_no_half_run_or_job(monkeypatch, tmp_path):
    attempts, _ = publisher(monkeypatch)
    async def run():
        async with fixture_db(monkeypatch, tmp_path) as (sessions, _):
            async with sessions() as session:
                fault_commit(session)
                with pytest.raises(CommitFault):
                    await main.task_trigger("fixture", PRINCIPAL, session)
                await session.rollback()
            async with sessions() as session:
                assert await count(session, TaskRun) == await count(session, OutboxEvent) == 0
                assert (await session.get(ScheduledTask, 200)).status == "ready"
            assert not attempts
    asyncio.run(run())


def test_unavailable_task_retry_reuses_run_and_event(monkeypatch, tmp_path):
    publisher(monkeypatch, unavailable=True)
    async def run():
        async with fixture_db(monkeypatch, tmp_path) as (sessions, _):
            async with sessions() as session:
                first = await main.task_trigger("fixture", PRINCIPAL, session)
            attempts, jobs = publisher(monkeypatch)
            async with sessions() as session:
                retried = await main.task_trigger("fixture", PRINCIPAL, session)
            assert not first["queued"] and retried["queued"]
            assert first["runId"] == retried["runId"]
            async with sessions() as session:
                assert await count(session, TaskRun) == await count(session, OutboxEvent) == 1
            assert len(attempts) == len(jobs) == 1
    asyncio.run(run())


def test_task_business_transaction_has_only_one_commit(monkeypatch, tmp_path):
    publisher(monkeypatch)
    async def run():
        async with fixture_db(monkeypatch, tmp_path) as (sessions, _):
            async with sessions() as session:
                original = session.commit
                commits = []
                async def observe():
                    commits.append(1)
                    assert len(commits) == 1, "Legacy second-commit failure window returned"
                    await original()
                session.commit = observe
                assert (await main.task_trigger("fixture", PRINCIPAL, session))["queued"]
            assert commits == [1]
    asyncio.run(run())


def test_publish_success_ack_failure_retries_same_job(monkeypatch, tmp_path):
    attempts, jobs = publisher(monkeypatch)
    async def run():
        async with fixture_db(monkeypatch, tmp_path) as (sessions, engine):
            failed = False
            class AckSession(AsyncSession):
                async def execute(self, statement, *args, **kwargs):
                    if getattr(statement, "is_update", False):
                        self.ack = (getattr(self, "ack", False)
                                    or statement.compile().params.get("status") == "sent")
                    return await super().execute(statement, *args, **kwargs)
                async def commit(self):
                    nonlocal failed
                    if getattr(self, "ack", False) and not failed:
                        failed = True
                        raise CommitFault("synthetic-publish-ack")
                    await super().commit()
            monkeypatch.setattr(outbox, "SessionLocal", async_sessionmaker(
                engine, class_=AckSession, expire_on_commit=False))
            async with sessions() as session:
                with pytest.raises(CommitFault):
                    await main.task_trigger("fixture", PRINCIPAL, session)
            assert failed and len(attempts) == len(jobs) == 1
            async with sessions() as session:
                event = await session.scalar(select(OutboxEvent))
                assert event.status == "sending"
                run_id = event.entity_id
                event.lease_until = datetime.now(UTC) - timedelta(seconds=60)
                await session.commit()
            async with sessions() as session:
                retried = await main.task_trigger("fixture", PRINCIPAL, session)
            assert retried["runId"] == run_id
            assert len(attempts) == 2 and len(jobs) == 1 and attempts[0] == attempts[1]
            async with sessions() as session:
                assert await count(session, TaskRun) == await count(session, OutboxEvent) == 1
    asyncio.run(run())


@pytest.mark.parametrize("concurrent", [False, True])
def test_durable_receipt_stops_duplicate_consumption(monkeypatch, tmp_path, concurrent):
    publisher(monkeypatch)
    async def run():
        async with fixture_db(monkeypatch, tmp_path) as (sessions, _):
            async with sessions() as session:
                triggered = await main.task_trigger("fixture", PRINCIPAL, session)
            async with sessions() as session:
                event_id = await session.scalar(select(OutboxEvent.id))
            if concurrent:
                await asyncio.gather(outbox.consume_event({}, event_id, 1),
                                     outbox.consume_event({}, event_id, 1))
            else:
                await outbox.consume_event({}, event_id, 1)
                await outbox.consume_event({}, event_id, 1)
            async with sessions() as session:
                run = await session.get(TaskRun, triggered["runId"])
                assert run.attempt == 1 and run.status == "success"
                event = await session.get(OutboxEvent, event_id)
                assert event.consume_status == "completed"
    asyncio.run(run())


def test_wrong_tenant_does_not_consume(monkeypatch, tmp_path):
    publisher(monkeypatch)
    async def run():
        async with fixture_db(monkeypatch, tmp_path) as (sessions, _):
            async with sessions() as session:
                await main.task_trigger("fixture", PRINCIPAL, session)
            async with sessions() as session:
                event_id = await session.scalar(select(OutboxEvent.id))
            assert await outbox.consume_event({}, event_id, 2) == {"status": "missing"}
            async with sessions() as session:
                assert (await session.get(OutboxEvent, event_id)).consume_status == "pending"
                assert (await session.scalar(select(TaskRun))).attempt == 0
    asyncio.run(run())


def test_cancelled_task_never_invokes_handler(monkeypatch, tmp_path):
    publisher(monkeypatch)
    invoke = AsyncMock()
    monkeypatch.setattr(outbox, "_invoke", invoke)
    async def run():
        async with fixture_db(monkeypatch, tmp_path) as (sessions, _):
            async with sessions() as session:
                await main.task_trigger("fixture", PRINCIPAL, session)
            async with sessions() as session:
                await main.task_cancel(200, PRINCIPAL, session)
                event_id = await session.scalar(select(OutboxEvent.id))
            assert await outbox.consume_event({}, event_id, 1) == {"status": "cancelled"}
            assert invoke.await_count == 0
    asyncio.run(run())


def test_expired_consumer_is_uncertain_never_automatically_reexecuted(monkeypatch, tmp_path):
    publisher(monkeypatch)
    invoke = AsyncMock()
    monkeypatch.setattr(outbox, "_invoke", invoke)
    async def run():
        async with fixture_db(monkeypatch, tmp_path) as (sessions, _):
            async with sessions() as session:
                await main.task_trigger("fixture", PRINCIPAL, session)
                event = await session.scalar(select(OutboxEvent))
                event_id = event.id
                event.consume_status = "running"
                event.consume_until = datetime.now(UTC) - timedelta(seconds=60)
                await session.commit()
            assert await outbox.recover_consumers() == 1
            result = await outbox.consume_event({}, event_id, 1)
            assert result == {"status": "uncertain", "automaticRetry": False}
            assert invoke.await_count == 0
    asyncio.run(run())


def test_consumer_exception_is_durable_uncertain(monkeypatch, tmp_path):
    publisher(monkeypatch)
    invoke = AsyncMock(side_effect=RuntimeError("synthetic-external-outcome-unknown"))
    monkeypatch.setattr(outbox, "_invoke", invoke)
    async def run():
        async with fixture_db(monkeypatch, tmp_path) as (sessions, _):
            async with sessions() as session:
                await main.task_trigger("fixture", PRINCIPAL, session)
                event_id = await session.scalar(select(OutboxEvent.id))
            with pytest.raises(RuntimeError):
                await outbox.consume_event({}, event_id, 1)
            assert (await outbox.consume_event({}, event_id, 1))["status"] == "uncertain"
            assert invoke.await_count == 1
    asyncio.run(run())


def test_resource_dedicated_second_step_failure_rolls_back(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "apply_knowledge_payload", lambda *_: (_ for _ in ()).throw(
        ValueError("synthetic-second-write")))
    publisher(monkeypatch)
    async def run():
        async with fixture_db(monkeypatch, tmp_path) as (sessions, _):
            async with sessions() as session:
                with pytest.raises(ValueError, match="synthetic-second-write"):
                    await main.knowledge_base_add_edit({"id": 500, "name": "new"}, BackgroundTasks(), PRINCIPAL, session)
                await session.rollback()
            async with sessions() as session:
                assert await session.get(Resource, 500) is None
                assert await session.get(KnowledgeBase, 500) is None
                assert await count(session, OutboxEvent) == 0
    asyncio.run(run())


def test_resource_dedicated_document_intent_commit_failure_rolls_back(monkeypatch, tmp_path):
    publisher(monkeypatch)
    async def run():
        async with fixture_db(monkeypatch, tmp_path) as (sessions, _):
            async with sessions() as session:
                fault_commit(session)
                with pytest.raises(CommitFault):
                    await main.knowledge_base_add_edit({
                        "id": 500, "name": "new", "aIKmssDetailsList": [{"content": "synthetic"}],
                    }, BackgroundTasks(), PRINCIPAL, session)
                await session.rollback()
            async with sessions() as session:
                assert await session.get(Resource, 500) is None
                assert await session.get(KnowledgeBase, 500) is None
                assert await count(session, KnowledgeDocument) == await count(session, OutboxEvent) == 0
    asyncio.run(run())


def test_document_inline_parse_is_receipted_and_idempotent(monkeypatch, tmp_path):
    async def run():
        async with fixture_db(monkeypatch, tmp_path) as (sessions, _):
            projection(monkeypatch)
            async with sessions() as session:
                session.add(AIModel(id=9, tenant_id=1, provider="openai", model_type="embedding",
                    model_name="fixture", endpoint="http://fixture.invalid", embedding_dimensions=2))
                await session.flush()
                (await session.get(KnowledgeBase, 100)).embedding_model_id = 9
                session.add(KnowledgeDocument(id=300, tenant_id=1, knowledge_base_id=100,
                                              source_name="synthetic", content="fixture paragraph",
                                              status="queued_text"))
                await session.flush()
                event_id = await outbox.stage_document(session, 300, 1)
                await session.commit()
            assert await outbox.dispatch_one(event_id)
            await outbox.consume_event({}, event_id, 1)
            async with sessions() as session:
                assert (await session.get(KnowledgeDocument, 300)).status == "ready"
                assert await count(session, KnowledgeIngestionRun) == 1
    asyncio.run(run())


def test_stage_dedup_key_and_rollback(monkeypatch, tmp_path):
    async def run():
        async with fixture_db(monkeypatch, tmp_path) as (sessions, _):
            async with sessions() as session:
                first = await outbox.stage(session, "knowledge-base", 100, 1, event_key="stable")
                assert await outbox.stage(session, "knowledge-base", 100, 1, event_key="stable") == first
                with pytest.raises(ValueError, match="another operation"):
                    await outbox.stage(session, "knowledge-base", 101, 1, event_key="stable")
                await session.rollback()
            async with sessions() as session:
                assert await count(session, OutboxEvent) == 0
    asyncio.run(run())


def test_background_dispatch_recovers_committed_unsent_event(monkeypatch, tmp_path):
    attempts, _ = publisher(monkeypatch)
    async def run():
        async with fixture_db(monkeypatch, tmp_path) as (sessions, _):
            async with sessions() as session:
                event_id = await outbox.stage(session, "knowledge-base", 100, 1)
                await session.commit()
            assert await outbox.dispatch_pending() == 1
            async with sessions() as session:
                assert (await session.get(OutboxEvent, event_id)).status == "sent"
            assert len(attempts) == 1
    asyncio.run(run())


def test_future_task_cannot_be_consumed_before_due(monkeypatch, tmp_path):
    publisher(monkeypatch)
    invoke = AsyncMock()
    monkeypatch.setattr(outbox, "_invoke", invoke)
    async def run():
        async with fixture_db(monkeypatch, tmp_path) as (sessions, _):
            async with sessions() as session:
                task = await session.get(ScheduledTask, 200)
                task.task_type, task.run_at = "once", datetime.now(UTC) + timedelta(hours=1)
                await session.commit()
                await main.task_trigger("fixture", PRINCIPAL, session)
                event_id = await session.scalar(select(OutboxEvent.id))
            assert await outbox.consume_event({}, event_id, 1) == {"status": "pending"}
            assert invoke.await_count == 0
    asyncio.run(run())


def test_queue_index_retry_reuses_same_event(monkeypatch, tmp_path):
    publisher(monkeypatch, unavailable=True)
    async def run():
        async with fixture_db(monkeypatch, tmp_path) as (sessions, _):
            async with sessions() as session:
                background = BackgroundTasks()
                assert (await main.knowledge_queue_index(100, background, PRINCIPAL, session))["queued"]
            await background()
            attempts, jobs = publisher(monkeypatch)
            async with sessions() as session:
                background = BackgroundTasks()
                assert (await main.knowledge_queue_index(100, background, PRINCIPAL, session))["queued"]
            await background()
            async with sessions() as session:
                assert await count(session, OutboxEvent) == 1
            assert len(attempts) == len(jobs) == 1
    asyncio.run(run())


def test_fast_kb_worker_completion_is_not_overwritten_by_publish_ack(monkeypatch, tmp_path):
    async def publish(event_id, tenant_id, **kwargs):
        await outbox.consume_event({}, event_id, tenant_id)
        return f"eic-outbox:{tenant_id}:{event_id}"
    monkeypatch.setattr(queue, "enqueue_outbox", publish)
    async def run():
        async with fixture_db(monkeypatch, tmp_path) as (sessions, _):
            projection(monkeypatch)
            async with sessions() as session:
                session.add(AIModel(id=9, tenant_id=1, provider="openai", model_type="embedding",
                    model_name="fixture", endpoint="http://fixture.invalid", embedding_dimensions=2))
                await session.flush()
                (await session.get(KnowledgeBase, 100)).embedding_model_id = 9
                session.add(KnowledgeDocument(id=await main.new_id(session), tenant_id=1,
                    knowledge_base_id=100, source_name="fixture.txt", content="synthetic lexical document"))
                await session.commit()
                background = BackgroundTasks()
                assert (await main.knowledge_queue_index(100, background, PRINCIPAL, session))["queued"]
            await background()
            async with sessions() as session:
                assert (await session.get(Resource, 100)).data["indexStatus"] == "ready"
                assert (await session.get(KnowledgeBase, 100)).status == "ready"
                assert (await session.scalar(select(KnowledgeDocument))).status == "ready"
                event = await session.scalar(select(OutboxEvent).where(OutboxEvent.kind == "knowledge-base"))
                assert event.consume_status == "completed"
    asyncio.run(run())


def test_fast_task_worker_completion_is_not_overwritten_by_publish_ack(monkeypatch, tmp_path):
    async def publish(event_id, tenant_id, **kwargs):
        await outbox.consume_event({}, event_id, tenant_id)
        return f"eic-outbox:{tenant_id}:{event_id}"
    monkeypatch.setattr(queue, "enqueue_outbox", publish)
    async def run():
        async with fixture_db(monkeypatch, tmp_path) as (sessions, _):
            async with sessions() as session:
                triggered = await main.task_trigger("fixture", PRINCIPAL, session)
            async with sessions() as session:
                assert (await session.get(TaskRun, triggered["runId"])).status == "success"
                assert (await session.get(ScheduledTask, 200)).status == "success"
    asyncio.run(run())


def test_concurrent_task_triggers_share_run_and_event(monkeypatch, tmp_path):
    attempts, jobs = publisher(monkeypatch)
    async def run():
        async with fixture_db(monkeypatch, tmp_path) as (sessions, _):
            async def trigger():
                async with sessions() as session:
                    return await main.task_trigger("fixture", PRINCIPAL, session)
            replies = await asyncio.gather(trigger(), trigger())
            assert replies[0]["runId"] == replies[1]["runId"]
            async with sessions() as session:
                assert await count(session, TaskRun) == await count(session, OutboxEvent) == 1
            assert len(attempts) == len(jobs) == 1
    asyncio.run(run())


def test_uncertain_task_retry_reports_original_run_without_new_effect(monkeypatch, tmp_path):
    publisher(monkeypatch)
    async def run():
        async with fixture_db(monkeypatch, tmp_path) as (sessions, _):
            async with sessions() as session:
                first = await main.task_trigger("fixture", PRINCIPAL, session)
                event = await session.scalar(select(OutboxEvent))
                event.consume_status = "running"
                event.consume_until = datetime.now(UTC) - timedelta(seconds=60)
                await session.commit()
            assert await outbox.recover_consumers() == 1
            attempts, _ = publisher(monkeypatch)
            async with sessions() as session:
                reply = await main.task_trigger("fixture", PRINCIPAL, session)
                assert reply["runId"] == first["runId"] and reply["status"] == "uncertain"
                assert reply["automaticRetry"] is False
                assert (await session.get(TaskRun, first["runId"])).status == "uncertain"
                assert (await session.get(ScheduledTask, 200)).status == "uncertain"
                assert await count(session, OutboxEvent) == await count(session, TaskRun) == 1
            assert attempts == []
    asyncio.run(run())


def test_resource_lock_failure_is_retryable_not_a_completed_receipt(monkeypatch, tmp_path):
    from arq.worker import Retry
    invoke = AsyncMock(side_effect=[{"status": "locked"}, {"status": "synthetic-success"}])
    monkeypatch.setattr(outbox, "_invoke", invoke)
    publisher(monkeypatch)
    async def run():
        async with fixture_db(monkeypatch, tmp_path) as (sessions, _):
            async with sessions() as session:
                await main.knowledge_queue_index(100, BackgroundTasks(), PRINCIPAL, session)
                event_id = await session.scalar(select(OutboxEvent.id))
            with pytest.raises(Retry):
                await outbox.consume_event({}, event_id, 1)
            async with sessions() as session:
                assert (await session.get(OutboxEvent, event_id)).consume_status == "pending"
            assert (await outbox.consume_event({}, event_id, 1))["status"] == "synthetic-success"
            assert invoke.await_count == 2
    asyncio.run(run())


def test_live_consumer_renews_lease(monkeypatch, tmp_path):
    monkeypatch.setattr(outbox, "CONSUMER_LEASE_SECONDS", 3)
    observed = []
    async def invoke(ctx, event):
        for _ in range(4):
            await asyncio.sleep(1.1)
            observed.append(await outbox.recover_consumers())
        return {"status": "synthetic-success"}
    monkeypatch.setattr(outbox, "_invoke", invoke)
    publisher(monkeypatch)
    async def run():
        async with fixture_db(monkeypatch, tmp_path) as (sessions, _):
            async with sessions() as session:
                await main.task_trigger("fixture", PRINCIPAL, session)
                event_id = await session.scalar(select(OutboxEvent.id))
            assert (await outbox.consume_event({}, event_id, 1))["status"] == "synthetic-success"
            assert observed == [0, 0, 0, 0]
            async with sessions() as session:
                event = await session.get(OutboxEvent, event_id)
                assert event.consume_token is None and event.consume_until is None
    asyncio.run(run())


def test_expired_receipt_fences_late_success(monkeypatch, tmp_path):
    publisher(monkeypatch)
    async def run():
        async with fixture_db(monkeypatch, tmp_path) as (sessions, _):
            async def invoke(ctx, event):
                async with sessions() as session:
                    await session.execute(update(OutboxEvent).where(
                        OutboxEvent.id == event.id).values(
                        consume_until=datetime.now(UTC) - timedelta(seconds=60)))
                    await session.commit()
                assert await outbox.recover_consumers() == 1
                return {"status": "late-success"}
            monkeypatch.setattr(outbox, "_invoke", invoke)
            async with sessions() as session:
                await main.task_trigger("fixture", PRINCIPAL, session)
                event_id = await session.scalar(select(OutboxEvent.id))
            assert (await outbox.consume_event({}, event_id, 1))["status"] == "uncertain"
            async with sessions() as session:
                assert (await session.get(OutboxEvent, event_id)).consume_status == "uncertain"
    asyncio.run(run())


def test_background_retry_obeys_backoff(monkeypatch, tmp_path):
    attempts, _ = publisher(monkeypatch, unavailable=True)
    async def run():
        async with fixture_db(monkeypatch, tmp_path) as (sessions, _):
            async with sessions() as session:
                await main.task_trigger("fixture", PRINCIPAL, session)
            assert len(attempts) == 1
            assert await outbox.dispatch_pending() == 0 and len(attempts) == 1
    asyncio.run(run())


def test_sent_but_unconsumed_event_is_reconciled_with_same_identity(monkeypatch, tmp_path):
    attempts, jobs = publisher(monkeypatch)
    async def run():
        async with fixture_db(monkeypatch, tmp_path) as (sessions, _):
            async with sessions() as session:
                first = await main.task_trigger("fixture", PRINCIPAL, session)
                event = await session.scalar(select(OutboxEvent))
                original_id = event.id
                event.next_attempt_at = datetime.now(UTC) - timedelta(seconds=60)
                await session.commit()
            # Broker state is lost after its positive acknowledgement.
            jobs.clear()
            assert await outbox.dispatch_pending() == 1
            assert attempts == [first["jobId"], first["jobId"]] and len(jobs) == 1
            async with sessions() as session:
                assert await count(session, TaskRun) == await count(session, OutboxEvent) == 1
                assert (await session.get(OutboxEvent, original_id)).status == "sent"
    asyncio.run(run())
