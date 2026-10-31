"""有界 Langfuse 传输；持久入口逐业务事件确认，仅接收已提交观测。"""

import asyncio
import ipaddress
import json
import math
import re
from datetime import UTC, datetime
from urllib.parse import urlsplit

import httpx

from eic.core.config import get_settings


_MAX_EVENT_BYTES = 262144
_MAX_BATCH_BYTES = 3000000
_MAX_RESPONSE_BYTES = 1048576
_CONFIG = re.compile(r"[a-z0-9][a-z0-9_-]{0,39}\Z")


def _integer(value, low, high):
    if type(value) is not int or not low <= value <= high:
        raise ValueError
    return value


def _seconds(value):
    if type(value) not in (int, float) or not math.isfinite(value) or not 0.1 <= value <= 10:
        raise ValueError
    return float(value)


def _endpoint(value):
    if not isinstance(value, str) or not value or any(c.isspace() for c in value):
        raise ValueError
    parts = urlsplit(value)
    if parts.username is not None or parts.password is not None or parts.query or parts.fragment:
        raise ValueError
    host = parts.hostname
    if not host or parts.path not in ("", "/") or "\\" in value:
        raise ValueError
    if parts.port is not None and not 1 <= parts.port <= 65535:
        raise ValueError
    try:
        loopback = ipaddress.ip_address(host).is_loopback
    except ValueError:
        loopback = host.lower() == "localhost"
    if parts.scheme != "https" and not (parts.scheme == "http" and loopback):
        raise ValueError
    return value.rstrip("/") + "/api/public/ingestion"


class LangfuseExporter:
    def __init__(self):
        self._queue = None
        self._client = None
        self._worker = None
        self._closing = None
        self._close_task = None
        self._loop = None
        self._build = None
        self._accepting = False
        self._enabled = False
        self._code = "DISABLED"
        self._shutdown_seconds = 2.0
        self._accepted = 0
        self._delivered = 0
        self._failed = 0
        self._unconfirmed = 0
        self._coalesced = 0
        self._rejected = 0
        self._retried = 0
        self._last_success = None
        self._pending_ids = set()

    async def start(self):
        if self._accepting or (self._worker is not None and not self._worker.done()):
            return
        if self._closing is not None and not self._closing.done():
            return
        if self._close_task is not None and not self._close_task.done():
            return
        if self._client is not None:
            return
        # 关闭分支在读取启用开关后立即返回，不加载转换器或构造异步资源。
        try:
            settings = get_settings()
            enabled = getattr(settings, "langfuse_enabled", False)
            self._enabled = enabled is True
            if type(enabled) is not bool:
                raise ValueError
            if not enabled:
                self._code = "DISABLED"
                return
            endpoint = _endpoint(settings.langfuse_base_url)
            public = settings.langfuse_public_key.get_secret_value()
            secret = settings.langfuse_secret_key.get_secret_value()
            if not public or not secret or ":" in public:
                raise ValueError
            if any(ord(c) < 32 or ord(c) == 127 for c in public + secret):
                raise ValueError
            capacity = _integer(settings.langfuse_queue_size, 1, 4096)
            batch_size = _integer(settings.langfuse_batch_size, 1, 128)
            retries = _integer(settings.langfuse_max_retries, 0, 2)
            timeout = _seconds(settings.langfuse_timeout_seconds)
            shutdown = _seconds(settings.langfuse_shutdown_seconds)
            environment = settings.environment
            namespace = settings.langfuse_namespace
            if not all(isinstance(v, str) and _CONFIG.fullmatch(v)
                       for v in (environment, namespace)):
                raise ValueError
        except Exception:
            self._code = "CONFIG_INVALID"
            return
        try:
            # A 由主集成；缺模块只停用此出口，不影响产品启动。
            from eic.core.langfuse_contract import build_ingestion_events
        except Exception:
            self._code = "CONTRACT_UNAVAILABLE"
            return
        client = None
        consumer = None
        try:
            self._shutdown_seconds = shutdown
            self._loop = asyncio.get_running_loop()
            self._endpoint = endpoint
            self._timeout = timeout
            self._retries = retries
            self._batch_size = batch_size
            self._environment = environment
            self._namespace = namespace
            self._build = build_ingestion_events
            self._queue = asyncio.Queue(maxsize=capacity)
            client = httpx.AsyncClient(
                auth=httpx.BasicAuth(public, secret),
                trust_env=False,
                follow_redirects=False,
                timeout=httpx.Timeout(timeout),
                limits=httpx.Limits(max_connections=1, max_keepalive_connections=1),
                transport=httpx.AsyncHTTPTransport(
                    retries=0, trust_env=False,
                    limits=httpx.Limits(max_connections=1, max_keepalive_connections=1),
                ),
            )
            self._client = client
            consumer = self._consume()
            self._worker = self._loop.create_task(consumer, name="langfuse-exporter")
            consumer = None
            self._accepting = True
            self._code = "READY"
        except BaseException as exc:
            self._accepting = False
            self._code = "START_FAILED"
            if consumer is not None:
                consumer.close()
            if client is not None:
                await self._close_client(client, shutdown)
            self._client = None
            self._queue = None
            if isinstance(exc, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
                raise

    def submit(self, row) -> bool:
        """只做纯转换和 put_nowait；不等待、不联网、不创建任务。"""
        if not self._accepting:
            self._rejected += 1
            return False
        try:
            if asyncio.get_running_loop() is not self._loop:
                self._code = "WRONG_LOOP"
                self._rejected += 1
                return False
            events = self._build(row, environment=self._environment, namespace=self._namespace)
            if not isinstance(events, list) or not events:
                self._rejected += 1
                return False
            if len(events) > 256 or len(events) > self._queue.maxsize - self._queue.qsize():
                self._code = "QUEUE_FULL"
                self._rejected += 1
                return False
            prepared = []
            ids = set()
            for event in events:
                event_id = event["id"]
                if not isinstance(event_id, str) or not event_id or len(event_id) > 256 or event_id in ids:
                    raise ValueError
                if not isinstance(event.get("body"), dict) or not isinstance(event.get("type"), str):
                    raise ValueError
                if not isinstance(event.get("timestamp"), str):
                    raise ValueError
                encoded = json.dumps(event, ensure_ascii=True, allow_nan=False, separators=(",", ":")).encode("ascii")
                if len(encoded) > _MAX_EVENT_BYTES:
                    raise ValueError
                ids.add(event_id)
                prepared.append((event_id, encoded))
            # 同一事件循环内没有 await，整行准入不会部分入队。
            for item in prepared:
                self._queue.put_nowait(item)
            self._accepted += len(prepared)
            return True
        except Exception:
            self._code = "SUBMIT_INVALID"
            self._rejected += 1
            return False

    def health(self) -> dict:
        """仅供可信管理员调用；接入路由不得向租户暴露进程诊断计数。"""
        return {
            "scope": "current_process",
            "enabled": self._enabled,
            "running": self._accepting and self._worker is not None and not self._worker.done(),
            "code": self._code,
            "queueDepth": self._queue.qsize() if self._queue is not None else 0,
            "queueCapacity": self._queue.maxsize if self._queue is not None else 0,
            "acceptedEvents": self._accepted,
            "deliveredEvents": self._delivered,
            "failedEvents": self._failed,
            "unconfirmedEvents": self._unconfirmed,
            "droppedEvents": self._failed + self._unconfirmed,
            "coalescedEvents": self._coalesced,
            "rejectedSubmissions": self._rejected,
            "retryEvents": self._retried,
            "lastSuccess": self._last_success,
            "inFlightEvents": len(self._pending_ids),
        }

    async def deliver_persisted(self, row):
        """兼容单事件调用；批量入口使用相同的逐项确认合同。"""
        key = row["event_key"]
        return (await self.deliver_persisted_batch([row]))[key]

    async def deliver_persisted_batch(self, rows):
        """一次有界 POST；每个业务事件的全部导出对象分别核对，不重发未知部分。"""
        outcomes = {row["event_key"]: "deferred" for row in rows}
        if not self._accepting:
            return outcomes
        pending, owners, size = [], {}, 1024
        for row in rows:
            key = row["event_key"]
            try:
                events = self._build(row, environment=self._environment, namespace=self._namespace)
                if not events:
                    outcomes[key] = "not_applicable"
                    continue
                items = [(event["id"], json.dumps(event, ensure_ascii=True, allow_nan=False,
                          separators=(",", ":")).encode("ascii")) for event in events]
                ids = {identity for identity, _ in items}
                if (len(items) > 256 or len(ids) != len(items)
                        or any(not isinstance(identity, str) or not identity or len(identity) > 256
                               or len(data) > _MAX_EVENT_BYTES for identity, data in items)):
                    raise ValueError
                added = sum(len(data) + 1 for _, data in items)
                if added + 1024 > _MAX_BATCH_BYTES:
                    raise ValueError
                # 一个业务事件不拆批；重复导出 ID 或容量不足者确定尚未发送。
                if ids.intersection(owners) or pending and (
                        len(pending) + len(items) > self._batch_size or size + added > _MAX_BATCH_BYTES):
                    continue
                pending.extend(items)
                owners.update({identity: key for identity in ids})
                size += added
                outcomes[key] = "unconfirmed"
            except Exception:
                outcomes[key] = "rejected"
        if not pending:
            return outcomes
        self._accepted += len(pending)
        try:
            async with asyncio.timeout(self._timeout):
                status, results = await self._request(pending)
            if results is None:
                if status == 429:
                    for key in set(owners.values()):
                        outcomes[key] = "retryable"
                elif status in {400, 401, 403, 404, 413, 422}:
                    for key in set(owners.values()):
                        outcomes[key] = "rejected"
            else:
                for key in set(owners.values()):
                    entries = [results.get(identity, (False, None))
                               for identity, owner in owners.items() if owner == key]
                    if all(confirmed for confirmed, _ in entries):
                        outcomes[key] = "acknowledged"
                    # 只有全部明确拒绝且均为 429，才能安全重试整个业务事件。
                    elif all(not confirmed and code == 429 for confirmed, code in entries):
                        outcomes[key] = "retryable"
                    elif all(not confirmed and code in {400, 401, 403, 404, 413, 422}
                             for confirmed, code in entries):
                        outcomes[key] = "rejected"
        except asyncio.CancelledError:
            # 领取的 sending 行保留，过期后统一转 unconfirmed。
            raise
        except Exception:
            pass
        self._delivered += sum(outcomes[owner] == "acknowledged" for owner in owners.values())
        self._failed += sum(outcomes[owner] == "rejected" for owner in owners.values())
        self._unconfirmed += sum(outcomes[owner] == "unconfirmed" for owner in owners.values())
        if any(value == "acknowledged" for value in outcomes.values()):
            self._last_success = datetime.now(UTC).isoformat()
        self._code = "DELIVERY_UNCONFIRMED" if "unconfirmed" in outcomes.values() else "READY"
        return outcomes

    async def _consume(self):
        queue = self._queue
        try:
            while True:
                batch = [await queue.get()]
                size = len(batch[0][1]) + 12
                ids = {batch[0][0]}
                # 不从队列提前取出下一批，保持 join 与计数一致。
                while len(batch) < self._batch_size and not queue.empty():
                    # 每个事件上界固定，保守预留，不读取 Queue 私有实现。
                    if size + _MAX_EVENT_BYTES + 1 > _MAX_BATCH_BYTES:
                        break
                    item = queue.get_nowait()
                    batch.append(item)
                    size += len(item[1]) + 1
                    if item[0] in ids:
                        break
                    ids.add(item[0])
                try:
                    # 重复 ID 保留一次；多个提交对应同一稳定事件，不重复入批。
                    unique = list({item[0]: item for item in batch}.values())
                    self._coalesced += len(batch) - len(unique)
                    self._pending_ids = {item[0] for item in unique}
                    try:
                        # 整批包含全部重试和退避，共享一份预算。
                        async with asyncio.timeout(self._timeout):
                            await self._send(unique, self._pending_ids)
                    except TimeoutError:
                        self._code = "BATCH_TIMEOUT"
                finally:
                    # 包括超时、取消、未知响应；仅结算尚未逐项确认的 ID。
                    # unconfirmed 不表示远端没有收到或持久化事件。
                    self._unconfirmed += len(self._pending_ids)
                    self._pending_ids.clear()
                    for _ in batch:
                        queue.task_done()
        except asyncio.CancelledError:
            raise
        except Exception:
            self._accepting = False
            self._code = "WORKER_FAILED"

    @staticmethod
    def _results(payload, expected):
        if not isinstance(payload, dict):
            raise ValueError
        successes, errors = payload.get("successes"), payload.get("errors")
        if not isinstance(successes, list) or not isinstance(errors, list):
            raise ValueError
        seen = {}
        for is_success, entries in ((True, successes), (False, errors)):
            for entry in entries:
                if not isinstance(entry, dict):
                    raise ValueError
                event_id, status = entry.get("id"), entry.get("status")
                if not isinstance(event_id, str) or event_id not in expected or event_id in seen:
                    raise ValueError
                if type(status) is not int or not 100 <= status <= 599:
                    raise ValueError
                seen[event_id] = (is_success and 200 <= status < 300 and status != 207, status)
        # 未列出的事件未知；同样不能把 errors 内的 2xx 当成成功。
        return seen

    async def _request(self, pending):
        content = b'{"batch":[' + b",".join(item[1] for item in pending) + b"]}"
        async with asyncio.timeout(self._timeout):
            async with self._client.stream(
                "POST", self._endpoint, content=content,
                headers={"Content-Type": "application/json", "Accept": "application/json"},
            ) as response:
                status = response.status_code
                if status not in (200, 207):
                    return status, None
                data = bytearray()
                async for chunk in response.aiter_bytes(chunk_size=65536):
                    if len(data) + len(chunk) > _MAX_RESPONSE_BYTES:
                        raise ValueError
                    data.extend(chunk)
                return status, self._results(json.loads(data), {item[0] for item in pending})

    async def _send(self, batch, unresolved):
        pending = batch
        for attempt in range(self._retries + 1):
            retry = []
            try:
                if attempt:
                    self._retried += len(pending)
                status, results = await self._request(pending)
                if results is None:
                    self._code = "HTTP_FAILED"
                    if status in (408, 425, 429) or 500 <= status <= 599:
                        retry = pending
                    else:
                        self._failed += len(pending)
                        unresolved.difference_update(item[0] for item in pending)
                else:
                    for item in pending:
                        confirmed, event_status = results.get(item[0], (False, None))
                        if confirmed:
                            self._delivered += 1
                            unresolved.remove(item[0])
                            self._last_success = datetime.now(UTC).isoformat()
                        elif event_status is None or event_status in (408, 425, 429) or 500 <= event_status <= 599:
                            retry.append(item)
                        else:
                            self._failed += 1
                            unresolved.remove(item[0])
                    self._code = "PARTIAL_FAILED" if len(results) != len(pending) or any(not r[0] for r in results.values()) else "READY"
            except (httpx.HTTPError, TimeoutError):
                self._code = "TRANSPORT_FAILED"
                retry = [item for item in pending if item[0] in unresolved]
            except Exception:
                self._code = "RESPONSE_INVALID"
                retry = [item for item in pending if item[0] in unresolved]
            if not retry:
                return
            if attempt == self._retries:
                self._code = "RETRIES_EXHAUSTED"
                return
            pending = retry
            await asyncio.sleep(min(0.1 * (2 ** attempt), 1.0))

    @staticmethod
    def _observe(task):
        if not task.cancelled():
            task.exception()

    async def _close_client(self, client, budget):
        task = asyncio.create_task(client.aclose(), name="langfuse-close")
        self._close_task = task
        task.add_done_callback(self._observe)
        try:
            done, _ = await asyncio.wait({task}, timeout=max(0.0, budget))
            if not done:
                task.cancel()
                self._code = "SHUTDOWN_TIMEOUT"
            elif task.cancelled() or task.exception() is not None:
                self._code = "SHUTDOWN_FAILED"
        except asyncio.CancelledError:
            task.cancel()
            raise

    async def _cleanup(self, worker, queue, client, deadline):
        try:
            if queue is not None and worker is not None and not worker.done():
                join = asyncio.create_task(queue.join(), name="langfuse-drain")
                try:
                    await asyncio.wait({join, worker}, timeout=max(0.0, (deadline - self._loop.time()) * 0.5), return_when=asyncio.FIRST_COMPLETED)
                finally:
                    join.cancel()
            if worker is not None and not worker.done():
                worker.cancel()
                await asyncio.wait({worker}, timeout=max(0.0, (deadline - self._loop.time()) * 0.5))
        finally:
            # 即使 drain/取消阶段失败，也发起 client 关闭；所有等待共用 deadline。
            try:
                if worker is not None and not worker.done():
                    worker.cancel()
                if client is not None:
                    await self._close_client(client, deadline - self._loop.time())
            finally:
                if queue is not None:
                    while not queue.empty():
                        queue.get_nowait()
                        queue.task_done()
                        self._unconfirmed += 1
                self._client = None
                self._queue = None
                self._build = None
                if worker is None or worker.done():
                    self._worker = None

    async def stop(self):
        self._accepting = False
        if self._closing is None or self._closing.done():
            if self._client is None and self._worker is None:
                return
            deadline = self._loop.time() + self._shutdown_seconds
            self._closing = self._loop.create_task(
                self._cleanup(self._worker, self._queue, self._client, deadline),
                name="langfuse-shutdown",
            )
            self._closing.add_done_callback(self._observe)
            self._stop_deadline = deadline
        try:
            done, _ = await asyncio.wait(
                {self._closing}, timeout=max(0.0, self._stop_deadline - self._loop.time())
            )
            if not done:
                self._closing.cancel()
                if self._worker is not None:
                    self._worker.cancel()
                self._code = "SHUTDOWN_TIMEOUT"
            elif self._closing.cancelled() or self._closing.exception() is not None:
                self._code = "SHUTDOWN_FAILED"
            elif self._code not in ("SHUTDOWN_TIMEOUT", "SHUTDOWN_FAILED"):
                self._code = "STOPPED"
        except asyncio.CancelledError:
            # 调用者取消不取消已启动的限时清理；清理只使用原 deadline。
            raise


class OtlpLangfuseExporter(LangfuseExporter):
    """显式启用的完整 span 出口；不向 v4 发送生命周期 upsert。"""

    async def start(self):
        if self._accepting:
            return
        try:
            from eic.core.langfuse_otlp import build_completed_spans
        except Exception:
            self._code = 'CONTRACT_UNAVAILABLE'
            return
        await super().start()
        if self._accepting:
            self._endpoint = self._endpoint.rsplit('/api/public/ingestion', 1)[0] + '/api/public/otel/v1/traces'
            self._build = build_completed_spans
            # OTLP 超时不能判定未收，不自动重发导致重复计费。
            self._retries = 0
            if not hasattr(self, '_seen_spans'):
                self._seen_spans = set()

    def submit(self, row):
        if not self._accepting:
            return super().submit(row)
        # 只处理终态；运行中生命周期继续保存在本地权威账本。
        try:
            events = self._build(row, environment=self._environment, namespace=self._namespace)
        except Exception:
            self._code = 'SUBMIT_INVALID'
            self._rejected += 1
            return False
        if not events:
            return False
        identity = events[0]['id']
        if identity in self._seen_spans:
            self._coalesced += 1
            return True
        # 有界去重；容量耗尽明确拒绝，不清空后重发旧 span。
        if len(self._seen_spans) >= 100000:
            self._code = 'DEDUP_CAPACITY'
            self._rejected += 1
            return False
        accepted = super().submit(row)
        if accepted:
            self._seen_spans.add(identity)
        return accepted

    async def _request(self, pending):
        spans = [json.loads(item[1])['body'] for item in pending]
        content = json.dumps({'resourceSpans': [{
            'resource': {'attributes': [{'key': 'service.name', 'value': {'stringValue': 'eic'}}]},
            'scopeSpans': [{'scope': {'name': 'eic.completed-observations', 'version': '1'}, 'spans': spans}],
        }]}, allow_nan=False, separators=(',', ':')).encode('utf-8')
        async with self._client.stream('POST', self._endpoint, content=content, headers={
            'Content-Type': 'application/json', 'Accept': 'application/json',
            'x-langfuse-ingestion-version': '4',
        }) as response:
            if response.status_code != 200:
                return response.status_code, None
            data = bytearray()
            async for chunk in response.aiter_bytes(chunk_size=65536):
                if len(data) + len(chunk) > _MAX_RESPONSE_BYTES:
                    raise ValueError
                data.extend(chunk)
            payload = json.loads(data)
            if not isinstance(payload, dict):
                raise ValueError
            standard = set(payload).issubset({'partialSuccess'})
            queued = (payload.get('name') == 'otel-ingestion-job'
                      and isinstance(payload.get('data'), dict)
                      and payload['data'].get('payload', {}).get('ingestionVersion') == '4')
            if not (standard or queued):
                raise ValueError
            partial = payload.get('partialSuccess', {})
            if not isinstance(partial, dict) or str(partial.get('rejectedSpans', 0)) != '0' or partial.get('errorMessage'):
                raise ValueError
            # delivered 只代表接收回执，业务可见性须独立查询核对。
            return 200, {item[0]: (True, 200) for item in pending}

    def health(self):
        return {**super().health(), 'transport': 'otlp_v4',
                'deliveryMeaning': 'transport_acknowledged_not_readback',
                'dedupScope': 'current_process', 'lifecycle': 'measured_spans_and_instant_events'}


import os
exporter = OtlpLangfuseExporter() if os.environ.get('EIC_LANGFUSE_TRANSPORT') == 'otlp_v4' else LangfuseExporter()
