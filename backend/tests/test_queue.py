import asyncio

from eic.core import queue


class FakeJob:
    job_id = "job-eic-test-1"


class FakeQueue:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[int, ...], dict]] = []

    async def enqueue_job(self, name: str, *args: int, **kwargs: object) -> FakeJob:
        self.calls.append((name, args, kwargs))
        return FakeJob()


def test_scheduled_task_queue_uses_deterministic_run_id(monkeypatch) -> None:
    fake = FakeQueue()

    async def fake_pool() -> FakeQueue:
        return fake

    monkeypatch.setattr(queue, "get_queue", fake_pool)
    job_id = asyncio.run(queue.enqueue_scheduled_task(7, 1000, 8, defer_by=2))

    assert job_id == "job-eic-test-1"
    assert fake.calls == [("run_scheduled_task", (7, 1000, 8), {"_job_id": "eic-task-run:8", "_defer_by": 2})]


def test_knowledge_base_queue_preserves_tenant_binding(monkeypatch) -> None:
    fake = FakeQueue()

    async def fake_pool() -> FakeQueue:
        return fake

    monkeypatch.setattr(queue, "get_queue", fake_pool)
    job_id = asyncio.run(queue.enqueue_knowledge_base(42, 1000))

    assert job_id == "job-eic-test-1"
    assert fake.calls == [("process_knowledge_base", (42, 1000), {})]
