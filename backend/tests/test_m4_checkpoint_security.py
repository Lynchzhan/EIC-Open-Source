from __future__ import annotations

from cryptography.fernet import Fernet
import pytest

from eic.ai import checkpointer as checkpointer_module
from eic.ai.checkpoint_security import CheckpointReadScope, sanitize_checkpoint_payload
from eic.ai.checkpoint_security import sanitize_checkpoint
from langchain_core.messages import HumanMessage
from eic.ai.checkpointer import DatabaseCheckpointer
from eic.models import AgentGraphCheckpoint, AgentGraphWrite, AgentRun


class FakeSession:
    def __init__(self, run, checkpoint=None, writes=()):
        self.run = run
        self.checkpoint = checkpoint
        self.writes = list(writes)
        self.commits = 0
        self.added = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False

    async def get(self, model, key):
        if model is AgentRun:
            return self.run if key == self.run.run_id else None
        return None

    def add(self, value):
        self.added.append(value)

    async def scalar(self, _query):
        return self.checkpoint

    async def scalars(self, _query):
        return self.writes

    async def commit(self):
        self.commits += 1


def make_run() -> AgentRun:
    return AgentRun(
        run_id="run-1", tenant_id=7, session_id=31, request_id="req-9",
        graph_version="graph-v2", trace_id="trace-1", status="interrupted",
    )


def make_scope(run: AgentRun, **changes) -> CheckpointReadScope:
    values = {
        "run_id": run.run_id,
        "tenant_id": run.tenant_id,
        "owner_id": 44,
        "session_id": run.session_id,
        "request_id": run.request_id,
        "graph_version": run.graph_version,
    }
    values.update(changes)
    return CheckpointReadScope(**values)


def install_fernet(monkeypatch):
    cipher = Fernet(Fernet.generate_key())
    monkeypatch.setattr(checkpointer_module, "_fernet", lambda: cipher)
    return cipher


def make_checkpoint_row(saver, run, *, value=None, key=None):
    key = key or [run.run_id, "", "cp-1"]
    if value is None:
        value = {
            "checkpoint": {
                "v": 1,
                "id": "cp-1",
                "channel_values": {"tool_results": [{"safe": "keep"}]},
                "channel_versions": {"tool_results": "1"},
                "versions_seen": {},
                "pending_sends": [],
            },
            "metadata": {"source": "loop", "step": 2},
        }
    kind, encrypted = saver._encode(key, value)
    return AgentGraphCheckpoint(
        run_id=run.run_id, namespace="", checkpoint_id="cp-1", parent_id=None,
        graph_version=run.graph_version, schema_version=1,
        payload_type=kind, payload_encrypted=encrypted,
    )


def test_sanitizer_preserves_safe_values_types_and_does_not_mutate_input():
    original = {
        "safe": "keep",
        "token_count": 12,
        "nested": [{"api_key": "secret-a", "access-token": "secret-b", "value": (1, 2)}],
        "control": {"__resume__": {"safe": True, "authorization": "secret-c"}},
    }

    cleaned = sanitize_checkpoint_payload(original)

    assert cleaned == {
        "safe": "keep",
        "token_count": 12,
        "nested": [{"value": (1, 2)}],
        "control": {"__resume__": {"safe": True}},
    }
    assert cleaned is not original
    assert original["nested"][0]["api_key"] == "secret-a"
    assert isinstance(cleaned["nested"][0]["value"], tuple)


def test_control_envelope_and_typed_messages_preserved_unknown_objects_rejected():
    message = HumanMessage(content="safe", additional_kwargs={"api_key": "secret", "safe": 1})
    original = {"id": "cp", "channel_versions": {"api_key": "version"},
                "channel_values": {"messages": [message]}}
    result = sanitize_checkpoint(original)
    assert result["channel_versions"] == original["channel_versions"]
    assert isinstance(result["channel_values"]["messages"][0], HumanMessage)
    assert result["channel_values"]["messages"][0].additional_kwargs == {"safe": 1}
    assert message.additional_kwargs["api_key"] == "secret"
    class Capability:
        pass
    with pytest.raises(TypeError, match="unknown_capability"):
        sanitize_checkpoint_payload(Capability())


@pytest.mark.asyncio
async def test_read_requires_real_run_scope_and_ignores_config_tenant(monkeypatch):
    run = make_run()
    saver = DatabaseCheckpointer()
    saver.sessions = lambda: FakeSession(run)
    called = []

    async def authorize(_session, actual_run, scope):
        called.append((actual_run.tenant_id, scope.owner_id))
        return scope.owner_id == 44

    saver.read_authorizer = authorize
    with pytest.raises(PermissionError, match="scope_required"):
        await saver.aget_tuple({"configurable": {"thread_id": run.run_id, "tenant_id": 7}})

    with saver.authorized_read_scope(make_scope(run, tenant_id=8)):
        with pytest.raises(PermissionError, match="scope_mismatch"):
            await saver.aget_tuple({"configurable": {"thread_id": run.run_id, "tenant_id": 7}})
    assert called == []

    with saver.authorized_read_scope(make_scope(run)):
        assert await saver.aget_tuple({"configurable": {"thread_id": run.run_id, "tenant_id": 999}}) is None
    assert called == [(7, 44)]

    with saver.authorized_read_scope(make_scope(run, owner_id=45)):
        with pytest.raises(PermissionError, match="denied"):
            await saver.aget_tuple({"configurable": {"thread_id": run.run_id}})


@pytest.mark.asyncio
async def test_checkpoint_listing_fails_closed_without_scope():
    saver = DatabaseCheckpointer(read_authorizer=_allow)
    saver.sessions = lambda: FakeSession(make_run())

    with pytest.raises(PermissionError, match="scope_required"):
        async for _ in saver.alist({"configurable": {"thread_id": "run-1"}}):
            pass


async def _allow(*_args):
    return True


@pytest.mark.asyncio
async def test_valid_scope_restores_checkpoint_and_legacy_encrypted_writes(monkeypatch):
    cipher = install_fernet(monkeypatch)
    run = make_run()
    saver = DatabaseCheckpointer(read_authorizer=_allow)
    legacy_checkpoint = {
        "checkpoint": {
            "v": 1,
            "id": "cp-1",
            "channel_values": {
                "tool_results": [{"safe": "kept", "api_key": "old-secret"}],
                "__resume__": {"approved": True, "access_token": "resume-secret"},
            },
            "channel_versions": {"tool_results": "1"},
            "versions_seen": {},
            "pending_sends": [],
        },
        "metadata": {"source": "loop", "access_token": "metadata-secret"},
    }
    checkpoint_binding = [run.run_id, "", "cp-1"]
    kind, payload = saver.serde.dumps_typed({"binding": checkpoint_binding, "value": legacy_checkpoint})
    row = AgentGraphCheckpoint(
        run_id=run.run_id, namespace="", checkpoint_id="cp-1", parent_id=None,
        graph_version=run.graph_version, schema_version=1,
        payload_type=kind, payload_encrypted=cipher.encrypt(payload).decode("ascii"),
    )
    write_value = {"safe": "pending", "nested": [{"access_token": "pending-secret"}]}
    write_binding = [run.run_id, "", "cp-1", "task-1", 0, "tool_results"]
    write_kind, write_payload = saver.serde.dumps_typed({"binding": write_binding, "value": write_value})
    write = AgentGraphWrite(
        run_id=run.run_id, namespace="", checkpoint_id="cp-1", task_id="task-1",
        write_index=0, channel="tool_results", payload_type=write_kind,
        payload_encrypted=cipher.encrypt(write_payload).decode("ascii"),
    )
    saver.sessions = lambda: FakeSession(run, row, [write])

    with saver.authorized_read_scope(make_scope(run)):
        restored = await saver.aget_tuple({"configurable": {"thread_id": run.run_id}})

    assert restored.checkpoint["channel_values"]["tool_results"] == [{"safe": "kept"}]
    assert restored.checkpoint["channel_values"]["__resume__"] == {"approved": True}
    assert restored.metadata == {"source": "loop"}
    assert restored.pending_writes == [("task-1", "tool_results", {"safe": "pending", "nested": [{}]})]
    assert "old-secret" not in repr(restored)
    assert "metadata-secret" not in repr(restored)
    assert "pending-secret" not in repr(restored)
    assert "resume-secret" not in repr(restored)


@pytest.mark.asyncio
async def test_write_sanitizes_without_mutating_and_preserves_resume_control(monkeypatch):
    install_fernet(monkeypatch)
    run = make_run()
    session = FakeSession(run)
    saver = DatabaseCheckpointer()
    saver.sessions = lambda: session

    async def guard(_session, _run_id):
        return run

    async def append_checkpoint(*_args, **_kwargs):
        return None

    saver._guard = guard
    monkeypatch.setattr(checkpointer_module.runtime_store, "append_checkpoint", append_checkpoint)
    original = {
        "v": 1,
        "id": "cp-1",
        "channel_values": {
            "tool_results": [{"safe": "keep", "api_key": "write-secret"}],
            "__resume__": {"approved": True, "access_token": "resume-secret"},
        },
        "channel_versions": {"tool_results": "1", "__resume__": "2"},
        "versions_seen": {},
        "pending_sends": [],
    }
    await saver.aput({"configurable": {"thread_id": run.run_id}}, original, {"step": 1}, {})

    assert original["channel_values"]["tool_results"][0]["api_key"] == "write-secret"
    assert original["channel_values"]["__resume__"]["access_token"] == "resume-secret"
    stored = saver._decode(
        [run.run_id, "", "cp-1"],
        session.added[0].payload_type,
        session.added[0].payload_encrypted,
    )
    assert stored["checkpoint"]["channel_values"]["tool_results"] == [{"safe": "keep"}]
    assert stored["checkpoint"]["channel_values"]["__resume__"] == {"approved": True}
    assert session.commits == 1


@pytest.mark.asyncio
async def test_pending_write_path_sanitizes_before_encryption(monkeypatch):
    install_fernet(monkeypatch)
    run = make_run()
    session = FakeSession(run)
    saver = DatabaseCheckpointer()
    saver.sessions = lambda: session

    async def guard(_session, _run_id):
        return run

    saver._guard = guard
    original = {"safe": "keep", "nested": [{"access_token": "write-secret"}]}
    await saver.aput_writes(
        {"configurable": {"thread_id": run.run_id, "checkpoint_id": "cp-1"}},
        [("tool_results", original)],
        "task-1",
    )

    record = session.added[0]
    restored = saver._decode(
        [run.run_id, "", "cp-1", "task-1", 0, "tool_results"],
        record.payload_type,
        record.payload_encrypted,
    )
    assert restored == {"safe": "keep", "nested": [{}]}
    assert original["nested"][0]["access_token"] == "write-secret"


def test_envelope_binding_tampering_is_rejected(monkeypatch):
    install_fernet(monkeypatch)
    run = make_run()
    saver = DatabaseCheckpointer()
    kind, encrypted = saver._encode([run.run_id, "", "cp-1"], {"value": "ok"})

    with pytest.raises(ValueError, match="binding_mismatch"):
        saver._decode([run.run_id, "other", "cp-1"], kind, encrypted)
