"""P5 有效 SQLite 事务、可控时钟与严格负控制；无真实模型或业务数据。"""
import asyncio
from datetime import UTC, datetime, timedelta
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

from arq.worker import Retry
import pytest
from sqlalchemy import select, update

from eic import main, workers
from eic.core import outbox, queue, tasks, task_schedule as schedule
from eic.models import (KnowledgeDocument, OutboxEvent, Permission, Resource, Role, RolePermission,
                        ScheduledTask, TaskRun, Tenant, User, UserRole)
from test_transactional_outbox import fixture_db, publisher, PRINCIPAL, count


@pytest.mark.parametrize("expression", [
    "", "* * * *", "* * * * * *", "60 * * * *", "* 24 * * *", "* * 0 * *",
    "* * * 13 *", "* * * * 8", "*/0 * * * *", "5-2 * * * *", "* * * jan *",
    "* * * * ?", "*/61 * * * *", "*,,1 * * * *", "1/0 * * * *",
])
def test_strict_cron_rejects_invalid(expression):
    with pytest.raises(ValueError):
        schedule.parse(expression)


@pytest.mark.parametrize("zone,expected", [
    ("UTC", datetime(2026, 10, 3, 2, tzinfo=UTC)),
    ("Asia/Shanghai", datetime(2026, 10, 3, 18, tzinfo=UTC)),
])
def test_timezone_changes_utc_plan(zone, expected):
    assert schedule.next_time("0 2 * * *", zone, datetime(2026, 10, 3, tzinfo=UTC)) == expected


def test_minute_and_range_step_and_dom_dow_or():
    after = datetime(2026, 10, 3, 0, 1, 17, tzinfo=UTC)
    assert schedule.next_time("* * * * *", "UTC", after) == after.replace(minute=2, second=0)
    assert schedule.matches("1-9/2 * * * *", after)
    assert not schedule.matches("1-9/2 * * * *", after.replace(minute=2))
    assert schedule.matches("0 2 15 * 6", datetime(2026, 10, 3, 2, tzinfo=UTC))
    assert schedule.matches("0 2 15 * 6", datetime(2026, 10, 15, 2, tzinfo=UTC))


def test_dst_hold_requires_explicit_policy_and_once_requires_offset():
    spring = datetime(2026, 3, 8, 5, tzinfo=UTC)
    autumn = datetime(2026, 11, 1, 4, tzinfo=UTC)
    with pytest.raises(ValueError, match="dst_policy_required"):
        schedule.next_time("30 2 * * *", "America/New_York", spring)
    with pytest.raises(ValueError, match="dst_policy_required"):
        schedule.next_time("30 1 * * *", "America/New_York", autumn)
    assert schedule.next_time("30 2 * * *", "America/New_York", spring, "skip") == datetime(2026, 3, 9, 6, 30, tzinfo=UTC)
    first = schedule.next_time("30 1 * * *", "America/New_York", autumn, "coalesce")
    assert first == datetime(2026, 11, 1, 5, 30, tzinfo=UTC)
    assert schedule.next_time("30 1 * * *", "America/New_York", first, "skip") == datetime(2026, 11, 2, 6, 30, tzinfo=UTC)
    for local in (datetime(2026, 3, 8, 2, 30), datetime(2026, 11, 1, 1, 30)):
        with pytest.raises(ValueError, match="once_requires_offset"):
            schedule.normalize_once(local, "America/New_York")
    assert schedule.normalize_once(datetime(2026, 10, 3, 8), "Asia/Shanghai") == datetime(2026, 10, 3, tzinfo=UTC)
    with pytest.raises(ValueError, match="invalid_time_zone"):
        schedule.zone("not/a/zone")


@pytest.mark.parametrize("data", [{}, {"appId": "1"}, {"handlerType": "shell", "knowledge_base_id": 100},
    {"knowledge_base_id": True}, {"knowledge_base_id": 0}, {"knowledge_base_id": 2**63},
    {"knowledge_base_id": 100, "command": "synthetic"}])
def test_unregistered_and_arbitrary_inputs_rejected(data):
    with pytest.raises(ValueError):
        tasks.normalize_input(data)


def test_dst_later_barrier_does_not_hide_earlier_valid_minute():
    assert schedule.next_time("30 0,1 * * *", "America/New_York",
        datetime(2026, 11, 1, 4, tzinfo=UTC)) == datetime(2026, 11, 1, 4, 30, tzinfo=UTC)
    assert schedule.next_time("30 0,2 * * *", "America/New_York",
        datetime(2026, 3, 8, 5, tzinfo=UTC)) == datetime(2026, 3, 8, 5, 30, tzinfo=UTC)


@pytest.mark.parametrize("policy,expected", [("hold", "cancelled"), ("skip", "cancelled"), ("coalesce", "success")])
def test_once_misfire_rechecks_policy_at_consumer(monkeypatch, tmp_path, policy, expected):
    publisher(monkeypatch)
    async def run():
        async with fixture_db(monkeypatch, tmp_path) as (sessions, _):
            async with sessions() as s:
                task = await s.get(ScheduledTask, 200)
                task.task_type, task.schedule_policy = "once", policy
                _, event_id = await tasks.stage_run(s, task,
                    scheduled_for=datetime.now(UTC) - timedelta(minutes=5))
                await s.commit()
            assert (await outbox.consume_event({}, event_id, 1))["status"] == expected
    asyncio.run(run())


def test_scan_batch_not_starved_by_one_hundred_uncertain_tasks(monkeypatch, tmp_path):
    async def run():
        async with fixture_db(monkeypatch, tmp_path) as (sessions, _):
            now = datetime.now(UTC).replace(second=0, microsecond=0)
            async with sessions() as s:
                for i in range(101):
                    s.add(ScheduledTask(id=1000 + i, tenant_id=1, owner_id=11, name=str(i),
                        cron="* * * * *", next_run_at=now - timedelta(seconds=1)))
                await s.flush()
                for i in range(101):
                    s.add(TaskRun(id=10000 + i, tenant_id=1, task_id=1000 + i, status="uncertain"))
                task = await s.get(ScheduledTask, 200)
                task.next_run_at = now
                await s.commit()
            async with sessions() as s:
                events = await tasks.scan_due(s, now)
                await s.commit()
                assert len(events) == 1
                stored = await s.get(OutboxEvent, events[0])
                assert (await s.get(TaskRun, stored.entity_id)).task_id == 200
    asyncio.run(run())


@pytest.mark.parametrize("kind", ["success", "missing", "locked", "failed", "accepted", "empty", "empty_report"])
def test_success_requires_business_report(monkeypatch, tmp_path, kind):
    publisher(monkeypatch)
    async def run():
        async with fixture_db(monkeypatch, tmp_path) as (sessions, _):
            async with sessions() as s:
                s.add(KnowledgeDocument(id=300, tenant_id=1, knowledge_base_id=100,
                    source_name="synthetic", content="body", index_status="parsed"))
                await s.commit()
                reply = await main.task_trigger("fixture", PRINCIPAL, s)
                event_id = await s.scalar(select(OutboxEvent.id))
            if kind != "success":
                monkeypatch.setattr(tasks, "execute_handler", AsyncMock(return_value=(
                    {"accepted": True} if kind == "accepted" else {} if kind == "empty" else
                    {"status": "success", "schemaVersion": 1, "handlerType": "knowledge-check", "report": {}}
                    if kind == "empty_report" else {"status": kind})))
            result = await outbox.consume_event({}, event_id, 1)
            async with sessions() as s:
                stored = await s.get(TaskRun, reply["runId"])
                if kind == "success":
                    assert stored.status == result["status"] == "success"
                    assert stored.result["report"]["documentCount"] == 1
                    assert stored.result["report"]["readOnly"] is True
                    assert stored.result["report"]["knowledgeBaseId"] == "100"
                else:
                    assert stored.status == result["status"] == "failed"
                    assert stored.error_message == "invalid_handler_result"
    asyncio.run(run())


@pytest.mark.parametrize("change", ["user", "tenant", "permission", "owner", "base", "disabled"])
def test_worker_rechecks_current_authorization(monkeypatch, tmp_path, change):
    publisher(monkeypatch)
    async def run():
        async with fixture_db(monkeypatch, tmp_path) as (sessions, _):
            async with sessions() as s:
                reply = await main.task_trigger("fixture", PRINCIPAL, s)
                event_id = await s.scalar(select(OutboxEvent.id))
                task = await s.get(ScheduledTask, 200)
                if change == "user":
                    (await s.get(User, 11)).is_active = False
                elif change == "tenant":
                    (await s.get(Tenant, 1)).active = False
                elif change == "permission":
                    (await s.get(User, 11)).is_super_admin = False
                elif change == "owner":
                    task.owner_id = None
                elif change == "base":
                    (await s.get(Resource, 100)).is_deleted = True
                else:
                    task.enabled = False
                await s.commit()
            result = await outbox.consume_event({}, event_id, 1)
            assert result["status"] == ("cancelled" if change == "disabled" else "failed")
            async with sessions() as s:
                stored = await s.get(TaskRun, reply["runId"])
                assert stored.status != "success"
                assert "report" not in stored.result
    asyncio.run(run())


@pytest.mark.parametrize("state", ["pending", "queued", "queue_unavailable", "retrying", "running"])
def test_cancel_all_active_states_and_late_delivery(monkeypatch, tmp_path, state):
    publisher(monkeypatch)
    invoke = AsyncMock()
    async def run():
        async with fixture_db(monkeypatch, tmp_path) as (sessions, _):
            async with sessions() as s:
                reply = await main.task_trigger("fixture", PRINCIPAL, s)
                stored = await s.get(TaskRun, reply["runId"])
                stored.status = state
                await s.commit()
                assert await main.task_cancel(200, PRINCIPAL, s)
                event_id = await s.scalar(select(OutboxEvent.id))
            monkeypatch.setattr(tasks, "execute_handler", invoke)
            assert (await outbox.consume_event({}, event_id, 1))["status"] == "cancelled"
            assert (await workers.run_scheduled_task({}, 200, 1, reply["runId"]))["status"] == "cancelled"
            async with sessions() as s:
                stored = await s.get(TaskRun, reply["runId"])
                assert stored.status == "cancelled" and stored.finished_at is not None
                assert stored.result["sideEffectsReverted"] is False
            assert invoke.await_count == 0
    asyncio.run(run())


def test_safe_read_only_retry_retains_receipt_then_cancel(monkeypatch, tmp_path):
    publisher(monkeypatch)
    invoke = AsyncMock(side_effect=OSError("synthetic-read-unavailable"))
    async def run():
        async with fixture_db(monkeypatch, tmp_path) as (sessions, _):
            monkeypatch.setattr(tasks, "execute_handler", invoke)
            async with sessions() as s:
                reply = await main.task_trigger("fixture", PRINCIPAL, s)
                event_id = await s.scalar(select(OutboxEvent.id))
            with pytest.raises(Retry):
                await outbox.consume_event({}, event_id, 1)
            async with sessions() as s:
                assert (await s.get(TaskRun, reply["runId"])).status == "retrying"
                assert (await s.get(OutboxEvent, event_id)).consume_status == "pending"
                repeat = await main.task_trigger("fixture", PRINCIPAL, s)
                assert repeat["runId"] == reply["runId"]
                await main.task_cancel(200, PRINCIPAL, s)
            assert (await outbox.consume_event({}, event_id, 1))["status"] == "cancelled"
            assert invoke.await_count == 1
    asyncio.run(run())


@pytest.mark.parametrize("state", ["success", "failed", "uncertain"])
def test_cancel_preserves_terminal_run(monkeypatch, tmp_path, state):
    publisher(monkeypatch)
    async def run():
        async with fixture_db(monkeypatch, tmp_path) as (sessions, _):
            async with sessions() as s:
                reply = await main.task_trigger("fixture", PRINCIPAL, s)
                stored = await s.get(TaskRun, reply["runId"])
                stored.status, stored.result = state, {"original": True}
                await s.commit()
                await main.task_cancel(200, PRINCIPAL, s)
            async with sessions() as s:
                stored = await s.get(TaskRun, reply["runId"])
                assert stored.status == state and stored.result == {"original": True}
    asyncio.run(run())


def test_once_save_edit_fences_old_plan_and_keeps_single_new_intent(monkeypatch, tmp_path):
    publisher(monkeypatch)
    async def run():
        async with fixture_db(monkeypatch, tmp_path) as (sessions, _):
            at = datetime.now(UTC) + timedelta(hours=2)
            data = {"name": "once", "taskType": "once", "runAt": at,
                    "payload": {"handlerType": "knowledge-check", "knowledge_base_id": 100}}
            async with sessions() as s:
                await main.task_add_edit(main.TaskInput(**data), PRINCIPAL, s, None)
                task = await s.scalar(select(ScheduledTask).where(ScheduledTask.name == "once"))
                old = await s.scalar(select(TaskRun).where(TaskRun.task_id == task.id))
                task_id, old_id = task.id, old.id
                assert old.scheduled_for is not None and old.schedule_revision == 1
                await main.task_add_edit(main.TaskInput(**{**data, "id": task_id,
                    "runAt": at + timedelta(hours=1)}), PRINCIPAL, s, None)
            assert (await workers.run_scheduled_task({}, task_id, 1, old_id))["status"] == "cancelled"
            async with sessions() as s:
                rows = list(await s.scalars(select(TaskRun).where(TaskRun.task_id == task_id)))
                assert len(rows) == 2 and sum(r.status == "pending" for r in rows) == 1
                latest = next(r for r in rows if r.id != old_id)
                assert latest.schedule_revision == 2 and latest.status == "pending"
                event = await s.scalar(select(OutboxEvent).where(OutboxEvent.entity_id == latest.id))
                assert (await outbox.consume_event({}, event.id, 1))["status"] == "pending"
                # 可控计划时间提前，只改变测试合成记录；原保存/编辑时刻仍在 evidence。
                latest.scheduled_for = datetime.now(UTC) - timedelta(seconds=2)
                event.available_at = latest.scheduled_for
                await s.commit()
            assert (await outbox.consume_event({}, event.id, 1))["status"] == "success"
            assert (await outbox.consume_event({}, event.id, 1))["status"] == "success"
            async with sessions() as s:
                assert (await s.get(TaskRun, latest.id)).attempt == 1
                assert (await s.get(TaskRun, old_id)).status == "cancelled"
    asyncio.run(run())


@pytest.mark.parametrize("policy,status,runs", [("hold", "schedule_held", 0), ("skip", "missed", 0),
                                              ("coalesce", "pending", 1)])
def test_downtime_policy_is_explicit_and_persistent(monkeypatch, tmp_path, policy, status, runs):
    async def run():
        async with fixture_db(monkeypatch, tmp_path) as (sessions, _):
            now = datetime(2026, 10, 3, 0, 6, 1, tzinfo=UTC)
            async with sessions() as s:
                task = await s.get(ScheduledTask, 200)
                task.next_run_at = now - timedelta(minutes=5)
                task.time_zone, task.schedule_policy = "UTC", policy
                await s.commit()
            for _ in range(2):
                async with sessions() as s:
                    await tasks.scan_due(s, now)
                    await s.commit()
            async with sessions() as s:
                task = await s.get(ScheduledTask, 200)
                assert task.status == status and await count(s, TaskRun) == runs
                if policy == "hold":
                    assert task.next_run_at is None
                else:
                    assert schedule.utc(task.next_run_at) == now.replace(minute=7, second=0)
                if runs:
                    assert (await s.scalar(select(TaskRun))).scheduled_for is not None
    asyncio.run(run())


def test_two_schedulers_share_plan_intent(monkeypatch, tmp_path):
    async def run():
        async with fixture_db(monkeypatch, tmp_path) as (sessions, _):
            now = datetime(2026, 10, 3, 0, 6, 1, tzinfo=UTC)
            async with sessions() as s:
                task = await s.get(ScheduledTask, 200)
                task.next_run_at, task.time_zone = now.replace(second=0), "UTC"
                await s.commit()
            async def scan():
                async with sessions() as s:
                    result = await tasks.scan_due(s, now)
                    await s.commit()
                    return result
            replies = await asyncio.gather(scan(), scan())
            assert sum(len(r) for r in replies) == 1
            async with sessions() as s:
                assert await count(s, TaskRun) == await count(s, OutboxEvent) == 1
    asyncio.run(run())


def test_delete_keeps_run_and_receipt_history(monkeypatch, tmp_path):
    publisher(monkeypatch)
    async def run():
        async with fixture_db(monkeypatch, tmp_path) as (sessions, _):
            async with sessions() as s:
                await main.task_trigger("fixture", PRINCIPAL, s)
                assert await main.task_remove("fixture", PRINCIPAL, s)
                assert await main.task_list({}, PRINCIPAL, s) == []
            async with sessions() as s:
                assert (await s.get(ScheduledTask, 200)).deleted_at is not None
                assert await count(s, TaskRun) == await count(s, OutboxEvent) == 1
    asyncio.run(run())


def test_missing_dependency_trigger_has_no_business_write(monkeypatch, tmp_path):
    async def run():
        async with fixture_db(monkeypatch, tmp_path) as (sessions, _):
            monkeypatch.setattr(queue, "task_capabilities", AsyncMock(return_value={
                "execution": False, "reason": "worker_unavailable"}))
            async with sessions() as s:
                with pytest.raises(main.HTTPException) as error:
                    await main.task_trigger("fixture", PRINCIPAL, s)
                assert error.value.status_code == 503
                await s.rollback()
                assert await count(s, TaskRun) == await count(s, OutboxEvent) == 0
    asyncio.run(run())


@pytest.mark.parametrize("heartbeat,redis,expected", [
    (None, True, False), ({"contract": "wrong"}, True, False),
    ([], True, False), ({"contract": tasks.CONTRACT, "scope": tasks.worker_scope(), "handlers": None}, True, False),
    ({"contract": tasks.CONTRACT, "handlers": list(tasks.HANDLERS), "scope": "wrong"}, True, False),
    ("valid", False, False), ("valid", True, True),
])
def test_capability_requires_redis_worker_contract_and_database(monkeypatch, heartbeat, redis, expected):
    class Pool:
        async def ping(self): return redis
        async def scan_iter(self, **kwargs): yield b"eic:tasks:worker:fixture"
        async def get(self, key):
            item = ({"contract": tasks.CONTRACT, "handlers": list(tasks.HANDLERS), "scope": tasks.worker_scope()}
                    if heartbeat == "valid" else heartbeat)
            return json.dumps(item) if item else None
    monkeypatch.setattr(queue.get_settings(), "task_execution_enabled", True)
    monkeypatch.setattr(queue.get_settings(), "outbox_dispatch_enabled", True)
    monkeypatch.setattr(queue, "get_queue", AsyncMock(return_value=Pool()))
    result = asyncio.run(queue.task_capabilities())
    assert result["execution"] is expected and result["scheduling"] is expected
    assert result["agentExecution"] is expected


def test_disabled_capability_never_connects(monkeypatch):
    monkeypatch.setattr(queue.get_settings(), "task_execution_enabled", False)
    pool = AsyncMock()
    monkeypatch.setattr(queue, "get_queue", pool)
    assert asyncio.run(queue.task_capabilities())["execution"] is False
    pool.assert_not_called()


@pytest.mark.parametrize("cancel_first", [False, True])
def test_completion_cancel_race_has_one_immutable_run_terminal(monkeypatch, tmp_path, cancel_first):
    publisher(monkeypatch)
    async def run():
        async with fixture_db(monkeypatch, tmp_path) as (sessions, _):
            async with sessions() as s:
                reply = await main.task_trigger("fixture", PRINCIPAL, s)
            async def cancel():
                async with sessions() as s:
                    return await main.task_cancel(200, PRINCIPAL, s)
            if cancel_first:
                await cancel()
                await workers.run_scheduled_task({}, 200, 1, reply["runId"])
            else:
                await asyncio.gather(workers.run_scheduled_task({}, 200, 1, reply["runId"]), cancel())
            async with sessions() as s:
                stored = await s.get(TaskRun, reply["runId"])
                assert stored.status in {"success", "cancelled"}
                original = stored.status, stored.result
                assert not (await s.get(ScheduledTask, 200)).enabled
            await workers.run_scheduled_task({}, 200, 1, reply["runId"])
            async with sessions() as s:
                stored = await s.get(TaskRun, reply["runId"])
                assert (stored.status, stored.result) == original
    asyncio.run(run())
