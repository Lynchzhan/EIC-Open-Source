"""独立 review 回归：脱敏附件路径与回答来源，全部使用临时隔离资源。"""
from contextlib import asynccontextmanager
import json
import sqlite3
from types import SimpleNamespace
from urllib.parse import quote

import pytest
from fastapi import HTTPException

from test_experience_capabilities import (
    file_sandbox, sandbox, client, seed, authenticate,
)


@pytest.mark.parametrize("legacy", [False, True])
async def test_redacted_filename_storage_suffix_read_upload_delete(sandbox, client, legacy):
    exp = sandbox.exp
    record = seed(sandbox, allow_chat_attachments=True)
    authenticate(client, record)

    async def parse(path, name, timeout, check):
        await check()
        return path.read_text(encoding="utf-8")

    sandbox.monkeypatch.setattr(exp, "parse_attachment", parse)
    response = await client.post("/api/experience/attachments", content=b"first body",
                                 headers={"x-experience-filename": quote("alice@example.com.txt")})
    assert response.status_code == 200, response.text
    item = response.json()
    assert "alice" not in item["name"] and not item["name"].endswith(".txt")
    path = sandbox.files.attachment_path(record.key, item["id"], ".txt")
    assert path.read_bytes() == b"first body"
    with exp.database() as db:
        row = db.execute("SELECT * FROM ephemeral.attachments WHERE id=?", (item["id"],)).fetchone()
        assert row["storage_suffix"] == ".txt"
        if legacy:
            db.execute("UPDATE ephemeral.attachments SET storage_suffix='' WHERE id=?", (item["id"],))
        state = exp.session_state(db, record.token)
        assert "first body" in exp.attachment_text(db, state, [item["id"]])
        assert db.execute("SELECT storage_suffix FROM ephemeral.attachments WHERE id=?", (item["id"],)).fetchone()[0] == ".txt"
        if legacy:
            db.execute("UPDATE ephemeral.attachments SET storage_suffix='' WHERE id=?", (item["id"],))
    second = await client.post("/api/experience/attachments", content=b"second body",
                               headers={"x-experience-filename": "next.txt"})
    assert second.status_code == 200, second.text
    if legacy:
        with exp.database() as db:
            db.execute("UPDATE ephemeral.attachments SET storage_suffix='' WHERE id=?", (item["id"],))
    deleted = await client.delete("/api/experience/attachments/" + item["id"])
    assert deleted.status_code == 200, deleted.text
    assert not path.exists()
    assert [r["id"] for r in (await client.get("/api/experience/attachments")).json()] == [second.json()["id"]]


async def test_legacy_ambiguous_suffix_does_not_delete_arbitrary_file(sandbox):
    exp = sandbox.exp
    record = seed(sandbox, allow_chat_attachments=True)
    key = "attachment:" + "a" * 32
    paths = [sandbox.files.attachment_path(record.key, key, suffix) for suffix in (".txt", ".pdf")]
    paths[0].parent.mkdir(parents=True)
    for path in paths:
        path.write_bytes(b"synthetic")
    with exp.database() as db:
        db.execute("INSERT INTO ephemeral.attachments (id,session_id,expires,name,content) VALUES (?,?,?,?,?)",
                   (key, record.key, record.grant["expires"], exp.redact("alice@example.com.txt"), "body"))
        with pytest.raises(HTTPException) as error:
            exp.attachment_text(db, exp.session_state(db, record.token), [key])
        assert error.value.status_code == 409
    assert all(path.exists() for path in paths)


async def test_old_attachment_table_migrates_without_losing_metadata(sandbox):
    exp = sandbox.exp
    # 仅重建 sandbox 的临时表，模拟升级前的五列结构。
    with sqlite3.connect(sandbox.settings.runtime_path(sandbox.settings.experience_content_root) / "ephemeral.sqlite") as db:
        db.execute("DROP TABLE attachments")
        db.execute("CREATE TABLE attachments (id TEXT PRIMARY KEY, session_id TEXT NOT NULL, expires REAL NOT NULL, name TEXT NOT NULL, content TEXT NOT NULL)")
        db.execute("INSERT INTO attachments VALUES (?,?,?,?,?)", ("legacy-id", "legacy-session", 123, "hidden", "body"))
    for _ in range(2):
        with exp.database() as db:
            row = db.execute("SELECT * FROM ephemeral.attachments WHERE id='legacy-id'").fetchone()
            assert dict(row) == {"id": "legacy-id", "session_id": "legacy-session", "expires": 123,
                                 "name": "hidden", "content": "body", "storage_suffix": ""}


async def test_web_sources_persist_restore_and_stay_out_of_model_history(sandbox, client):
    from eic.ai import providers, search
    from eic.models import AIModel

    exp = sandbox.exp
    record = seed(sandbox, allow_web_search=True, tokens=3000000)
    authenticate(client, record)
    values = {column.name: None for column in AIModel.__table__.columns}
    values.update(id=101, tenant_id=1, enabled=True, model_type="chat", model_name="synthetic-model",
                  endpoint="https://model.invalid/v1", secret_encrypted=None,
                  max_input_tokens=131072, max_output_tokens=1024, search_mode="independent")
    model = SimpleNamespace(**values, __table__=AIModel.__table__)

    @asynccontextmanager
    async def session():
        async def rollback():
            pass
        yield SimpleNamespace(rollback=rollback)

    async def validate(grant, session):
        return [model], [], []

    async def independent(query, settings, *, tenant_id):
        assert tenant_id == 1
        return "synthetic search context", [
            {"title": "Safe reference", "content": "Synthetic excerpt", "url": "https://reference.invalid/article"},
            {"title": "Blocked", "url": "https://model.invalid/private"},
            {"title": "Blocked token", "url": "https://reference.invalid/?api_key=private"},
        ]

    seen = []

    async def stream(model, prompt, *, messages, usage_sink, native_search, citation_sink):
        seen.append(messages)
        usage_sink["total_tokens"] = 12
        yield "Synthetic answer"

    sandbox.monkeypatch.setattr(exp, "SessionLocal", session)
    sandbox.monkeypatch.setattr(exp, "validate_resources", validate)
    sandbox.monkeypatch.setattr(search, "validate_mode", lambda model, settings: "independent")
    sandbox.monkeypatch.setattr(search, "independent_search", independent)
    sandbox.monkeypatch.setattr(providers, "stream_chat_completion", stream)
    response = await client.post("/api/experience/chat", json={"message": "first", "modelId": "101", "web_search": True})
    assert response.status_code == 200
    events = [json.loads(line) for line in response.text.splitlines()]
    done = next(event for event in events if event["type"] == "done")
    assert [s["url"] for s in done["webSources"]] == ["https://reference.invalid/article"]
    with exp.database() as db:
        saved = json.loads(db.execute("SELECT data FROM ephemeral.content WHERE id=?", (record.key,)).fetchone()[0])
        assert saved[-1]["content"] == "Synthetic answer"
        assert saved[-1]["webSources"] == done["webSources"]
    for _ in range(2):
        restored = (await client.get("/api/experience/session")).json()["messages"][-1]
        assert restored["content"].count("https://reference.invalid/article") == 1
        assert restored["webSources"] == done["webSources"]
        assert "api_key" not in json.dumps(restored)
    second = await client.post("/api/experience/chat", json={"message": "second", "modelId": "101"})
    assert any(json.loads(line)["type"] == "done" for line in second.text.splitlines())
    assert seen[1][2] == {"role": "assistant", "content": "Synthetic answer"}
    restored = (await client.get("/api/experience/session")).json()["messages"]
    assert restored[1]["webSources"] == done["webSources"]
    assert restored[-1]["webSources"] == []


async def test_old_history_without_sources_restores_unchanged(sandbox, client):
    record = seed(sandbox)
    authenticate(client, record)
    messages = [{"role": "user", "content": "old"}, {"role": "assistant", "content": "old answer"}]
    with sandbox.exp.database() as db:
        db.execute("INSERT INTO ephemeral.content VALUES (?,?,?)", (record.key, record.grant["expires"], json.dumps(messages)))
    assert (await client.get("/api/experience/session")).json()["messages"] == messages
