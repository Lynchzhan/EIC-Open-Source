"""有界后台派发池；数据库仍是任务队列，内存中不积压等待者。"""
import asyncio
import logging
from collections.abc import Awaitable, Callable

log = logging.getLogger(__name__)


class DispatchPool:
    def __init__(self, capacity: int):
        if capacity < 1:
            raise ValueError("dispatch_capacity_must_be_positive")
        self.capacity = capacity
        self.active: dict[int, asyncio.Task] = {}
        self.closed = False

    @property
    def available(self):
        return 0 if self.closed else self.capacity - len(self.active)

    def submit(self, key: int, operation: Callable[[int], Awaitable]):
        if key in self.active or not self.available:
            return False
        task = asyncio.create_task(operation(key), name=f"outbox-dispatch:{key}")
        self.active[key] = task
        task.add_done_callback(lambda completed: self._finished(key, completed))
        return True

    def _finished(self, key, task):
        self.active.pop(key, None)
        if not task.cancelled() and task.exception() is not None:
            log.error("Background dispatch failed event_id=%s", key)

    async def close(self):
        self.closed = True
        tasks = list(self.active.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.active.clear()
