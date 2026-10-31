"""基于应用数据库的加密异步 LangGraph Checkpointer。

完整 checkpoint 和 pending writes 一并持久化；thread_id 唯一绑定 AgentRun。
密钥沿用部署密钥，不写入公开源码，也不使用 pickle 反序列化。
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Awaitable, Callable, Iterator

from langgraph.checkpoint.base import WRITES_IDX_MAP, BaseCheckpointSaver, CheckpointTuple
from sqlalchemy import select

from eic.core import agent_worker, generation
from eic.core.agent_runtime import AgentLeaseLost, runtime_store
from eic.core.ids import new_id
from eic.core.security import _fernet
from eic.models import AgentGraphCheckpoint, AgentGraphWrite, AgentRun, ChatSession, User, ChatMessage
from eic.ai.checkpoint_security import (
    CheckpointReadScope,
    CheckpointReadScopeBinding,
    is_credential_channel,
    sanitize_checkpoint,
    sanitize_checkpoint_payload,
)


ReadAuthorizer = Callable[[object, AgentRun, CheckpointReadScope], Awaitable[bool]]


class DatabaseCheckpointer(BaseCheckpointSaver):
    def __init__(self, sessions=None, *, read_authorizer: ReadAuthorizer | None = None):
        super().__init__()
        self.sessions = sessions
        self.read_authorizer = read_authorizer
        self._read_scope = CheckpointReadScopeBinding()
        self._scope_authorizer = ContextVar(f"checkpoint_authorizer_{id(self)}", default=None)

    def _sessions(self):
        return self.sessions or generation.SessionLocal

    @contextmanager
    def authorized_read_scope(self, scope: CheckpointReadScope, *, authorizer=None) -> Iterator[None]:
        """Bind a service-resolved scope for saver reads in this async task."""
        token = self._scope_authorizer.set(authorizer)
        try:
            with self._read_scope.bind(scope):
                yield
        finally:
            self._scope_authorizer.reset(token)

    async def _authorize_read(self, session, run_id):
        scope = self._read_scope.current
        run = await session.get(AgentRun, run_id)
        if run is None:
            return None
        owner = agent_worker.OWNER.get()
        if scope is None and owner is not None and owner[0] == run_id:
            await agent_worker.fence(session)
            if agent_worker.OWNER.get() != owner:
                raise AgentLeaseLost("graph_checkpoint_worker_missing")
            if run.session_id is not None:
                chat = await session.get(ChatSession, run.session_id)
                if chat is None or chat.tenant_id != run.tenant_id:
                    raise PermissionError("graph_checkpoint_chat_mismatch")
                user = await session.get(User, chat.owner_id)
                if user is None or user.tenant_id != run.tenant_id or not user.is_active:
                    raise PermissionError("graph_checkpoint_owner_mismatch")
                if run.request_id is not None:
                    message = await session.scalar(select(ChatMessage).where(
                        ChatMessage.session_id == run.session_id,
                        ChatMessage.tenant_id == run.tenant_id,
                        ChatMessage.request_id == run.request_id,
                    ).limit(1))
                    if message is None:
                        raise PermissionError("graph_checkpoint_request_mismatch")
            return run
        authorizer = self._scope_authorizer.get() or self.read_authorizer
        if scope is None or authorizer is None or scope.run_id != run_id:
            raise PermissionError("graph_checkpoint_read_scope_required")
        if (
            scope.tenant_id != run.tenant_id
            or scope.session_id != run.session_id
            or scope.request_id != run.request_id
            or scope.graph_version != run.graph_version
        ):
            raise PermissionError("graph_checkpoint_read_scope_mismatch")
        authorized = await authorizer(session, run, scope)
        if authorized is not True:
            raise PermissionError("graph_checkpoint_read_denied")
        return run

    def _encode(self, binding, value):
        kind, payload = self.serde.dumps_typed({"binding": binding, "value": value})
        return kind, _fernet().encrypt(payload).decode("ascii")

    def _decode(self, binding, kind, encrypted):
        envelope = self.serde.loads_typed((kind, _fernet().decrypt(encrypted.encode("ascii"))))
        if envelope["binding"] != binding:
            raise ValueError("graph_checkpoint_binding_mismatch")
        return envelope["value"]

    @staticmethod
    def _config(run_id, namespace, checkpoint_id):
        return {"configurable": {
            "thread_id": run_id, "checkpoint_ns": namespace, "checkpoint_id": checkpoint_id,
        }}

    async def _guard(self, session, run_id):
        # 与事件写入保持 allocator -> generation -> worker 的固定加锁顺序。
        await new_id(session)
        await generation.fence(session)
        await agent_worker.fence(session)
        owner = agent_worker.OWNER.get()
        if owner is None or owner[0] != run_id:
            raise AgentLeaseLost("graph_checkpoint_worker_missing")
        run = await session.get(AgentRun, run_id)
        if run is None or run.status != "running":
            raise AgentLeaseLost("graph_checkpoint_run_not_running")
        return run

    async def aget_tuple(self, config):
        if not isinstance(config, dict) or not isinstance(config.get("configurable"), dict):
            raise PermissionError("graph_checkpoint_read_scope_required")
        c = config["configurable"]
        run_id, ns = str(c["thread_id"]), c.get("checkpoint_ns", "")
        async with self._sessions()() as session:
            run = await self._authorize_read(session, run_id)
            if run is None:
                return None
            query = select(AgentGraphCheckpoint).where(
                AgentGraphCheckpoint.run_id == run_id, AgentGraphCheckpoint.namespace == ns,
            )
            if c.get("checkpoint_id"):
                query = query.where(AgentGraphCheckpoint.checkpoint_id == c["checkpoint_id"])
            row = await session.scalar(query.order_by(AgentGraphCheckpoint.checkpoint_id.desc()).limit(1))
            if row is None:
                return None
            if row.schema_version != 1 or run is None or row.graph_version != run.graph_version:
                raise ValueError("graph_checkpoint_version_mismatch")
            key = [run_id, ns, row.checkpoint_id]
            value = self._decode(key, row.payload_type, row.payload_encrypted)
            checkpoint = sanitize_checkpoint(value["checkpoint"])
            metadata = sanitize_checkpoint_payload(value["metadata"])
            writes = list(await session.scalars(select(AgentGraphWrite).where(
                AgentGraphWrite.run_id == run_id, AgentGraphWrite.namespace == ns,
                AgentGraphWrite.checkpoint_id == row.checkpoint_id,
            ).order_by(AgentGraphWrite.task_id, AgentGraphWrite.write_index)))
            pending_writes = []
            for write in writes:
                # Authenticate even discarded legacy credential writes before filtering.
                value = self._decode(
                    [*key, write.task_id, write.write_index, write.channel],
                    write.payload_type, write.payload_encrypted,
                )
                if not is_credential_channel(write.channel):
                    pending_writes.append((write.task_id, write.channel, sanitize_checkpoint_payload(value)))
            return CheckpointTuple(
                config=self._config(*key),
                checkpoint=checkpoint,
                metadata=metadata,
                parent_config=self._config(run_id, ns, row.parent_id) if row.parent_id else None,
                pending_writes=pending_writes,
            )

    async def aput(self, config, checkpoint, metadata, new_versions):
        c = config["configurable"]
        key = [str(c["thread_id"]), c.get("checkpoint_ns", ""), checkpoint["id"]]
        safe_checkpoint = sanitize_checkpoint(checkpoint)
        safe_metadata = sanitize_checkpoint_payload(metadata)
        kind, encrypted = self._encode(key, {"checkpoint": safe_checkpoint, "metadata": safe_metadata})
        async with self._sessions()() as session:
            run = await self._guard(session, key[0])
            existing = await session.get(AgentGraphCheckpoint, tuple(key))
            if existing is None:
                session.add(AgentGraphCheckpoint(
                    run_id=key[0], namespace=key[1], checkpoint_id=key[2],
                    parent_id=c.get("checkpoint_id"), graph_version=run.graph_version,
                    schema_version=1, payload_type=kind, payload_encrypted=encrypted,
                ))
                values = safe_checkpoint.get("channel_values", {})
                await runtime_store.append_checkpoint(
                    session, run_id=run.run_id,
                    node_name=str(values.get("current_node") or "start"),
                    state={"graphVersion": run.graph_version, "graphCheckpointId": key[2],
                           "providerStarted": False, "providerOutcome": "not_started"},
                    resume_policy="safe_replay",
                )
            await session.commit()
        return self._config(*key)

    async def aput_writes(self, config, writes, task_id, task_path=""):
        c = config["configurable"]
        key = [str(c["thread_id"]), c.get("checkpoint_ns", ""), c["checkpoint_id"]]
        async with self._sessions()() as session:
            await self._guard(session, key[0])
            for index, (channel, value) in enumerate(writes):
                if is_credential_channel(channel):
                    continue
                index = WRITES_IDX_MAP.get(channel, index)
                primary = (*key, task_id, index)
                existing = await session.get(AgentGraphWrite, primary)
                if existing is not None and index >= 0:
                    continue
                safe_value = sanitize_checkpoint_payload(value)
                kind, encrypted = self._encode([*primary, channel], safe_value)
                if existing is None:
                    session.add(AgentGraphWrite(
                        run_id=key[0], namespace=key[1], checkpoint_id=key[2],
                        task_id=task_id, write_index=index, channel=channel,
                        payload_type=kind, payload_encrypted=encrypted,
                    ))
                else:
                    existing.channel = channel
                    existing.payload_type, existing.payload_encrypted = kind, encrypted
            await session.commit()

    async def alist(self, config, *, filter=None, before=None, limit=None):
        if not isinstance(config, dict) or not isinstance(config.get("configurable"), dict):
            raise PermissionError("graph_checkpoint_read_scope_required")
        c = config["configurable"]
        async with self._sessions()() as session:
            run = await self._authorize_read(session, str(c["thread_id"]))
            if run is None:
                return
            query = select(AgentGraphCheckpoint.checkpoint_id).where(
                AgentGraphCheckpoint.run_id == str(c["thread_id"]),
                AgentGraphCheckpoint.namespace == c.get("checkpoint_ns", ""),
            )
            if before:
                query = query.where(AgentGraphCheckpoint.checkpoint_id < before["configurable"]["checkpoint_id"])
            ids = list(await session.scalars(query.order_by(AgentGraphCheckpoint.checkpoint_id.desc())))
        count = 0
        for checkpoint_id in ids:
            item = await self.aget_tuple(self._config(
                str(c["thread_id"]), c.get("checkpoint_ns", ""), checkpoint_id,
            ))
            if item is not None and all(item.metadata.get(k) == v for k, v in (filter or {}).items()):
                if limit is not None and count >= limit:
                    return
                count += 1
                yield item
