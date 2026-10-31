"""补齐 P0-08 的公开演示完整流与真实体验字段白名单。"""
from contextlib import asynccontextmanager
import json
from types import SimpleNamespace

from test_experience_capabilities import file_sandbox, sandbox, client, seed, authenticate


async def test_public_entry_and_complete_chat_never_invoke_provider(sandbox, client):
    from eic.ai import providers

    def forbidden(*args, **kwargs):
        raise AssertionError("公开演示不可访问真实模型")

    sandbox.monkeypatch.setattr(providers, "stream_chat_completion", forbidden)
    response = await client.post("/api/experience/enter", json={"mode": "public"})
    assert response.status_code == 200
    assert response.json()["mode"] == "public"
    cookie = response.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie
    assert "path=/api/experience" in cookie
    response = await client.post("/api/experience/chat", json={"message": "介绍一下入职流程"})
    assert response.status_code == 200
    events = [json.loads(line) for line in response.text.splitlines()]
    assert sum(event["type"] == "done" for event in events) == 1
    assert not any(event["type"] == "error" for event in events)
    answer = "".join(event["text"] for event in events if event["type"] == "delta")
    assert "未调用真实模型" in answer
    restored = (await client.get("/api/experience/session")).json()
    assert restored["messages"][-1]["content"] == answer
    assert restored["usablePages"] == []
    with sandbox.exp.database() as db:
        assert sandbox.exp.load(db, "usage") is None
    assert (await client.post("/api/experience/leave")).status_code == 200
    assert (await client.get("/api/experience/session")).status_code == 401


async def test_real_catalog_exposes_only_authorized_display_fields(sandbox, client):
    exp = sandbox.exp
    authenticate(client, seed(sandbox, pages=["chat", "models"]))

    @asynccontextmanager
    async def session():
        yield object()

    async def validate(grant, session):
        return [SimpleNamespace(id=101, model_name="PRIVATE-MODEL",
                                endpoint="https://private.invalid", secret_encrypted="PRIVATE-KEY")], [], []

    sandbox.monkeypatch.setattr(exp, "SessionLocal", session)
    sandbox.monkeypatch.setattr(exp, "validate_resources", validate)
    response = await client.get("/api/experience/catalog?page=models")
    assert response.status_code == 200
    assert response.json()["models"] == [{"id": "101", "name": "体验模型 1", "readOnly": True}]
    assert all(value not in response.text for value in ("PRIVATE", "private.invalid", "endpoint", "secret"))
    assert response.headers["cache-control"] == "no-store"
