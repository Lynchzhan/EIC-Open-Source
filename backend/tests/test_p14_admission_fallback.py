import asyncio
from datetime import timedelta
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from eic.core.dependency_controls import admit, release, DependencyDenied, acquire, finish, dependency_key
from eic.core.outbox import now
from eic.models import AdmissionLease, AdmissionWindow, DependencyCircuit
from test_id_generation_safety import fixture_db

LIMITS = SimpleNamespace(admission_global_concurrency=2, admission_tenant_concurrency=1,
                         admission_global_rate=2, admission_tenant_rate=1,
                         admission_global_units=10, admission_tenant_units=10)


def test_shared_budgets_reject_atomically_and_never_refund_unknown_cost(monkeypatch, tmp_path):
    async def run():
        async with fixture_db(monkeypatch, tmp_path) as sessions:
            token = await admit(sessions, 1, 1, LIMITS)
            with pytest.raises(DependencyDenied, match="并发"):
                await admit(sessions, 1, 1, LIMITS)
            await release(sessions, token)
            with pytest.raises(DependencyDenied, match="速率"):
                await admit(sessions, 1, 1, LIMITS)
            with pytest.raises(DependencyDenied, match="成本"):
                await admit(sessions, 2, 11, LIMITS)
            async with sessions() as session:
                assert (await session.get(AdmissionWindow, "global")).requests == 1
                assert (await session.get(AdmissionWindow, "global")).reserved_units == 1
                assert await session.get(AdmissionWindow, "tenant:2") is None
            second = await admit(sessions, 2, 1, LIMITS)
            await release(sessions, second)
            with pytest.raises(DependencyDenied, match="速率"):
                await admit(sessions, 3, 1, LIMITS)
    asyncio.run(run())


def test_expired_crash_lease_releases_capacity_without_cost_refund(monkeypatch, tmp_path):
    async def run():
        limits = SimpleNamespace(**{**vars(LIMITS), "admission_tenant_rate": 5, "admission_global_rate": 5})
        async with fixture_db(monkeypatch, tmp_path) as sessions:
            token = await admit(sessions, 1, 2, limits)
            async with sessions() as session:
                (await session.get(AdmissionLease, token)).expires_at = await now(session) - timedelta(seconds=1)
                await session.commit()
            await admit(sessions, 1, 2, limits)
            async with sessions() as session:
                assert (await session.get(AdmissionWindow, "global")).reserved_units == 4
    asyncio.run(run())


def test_half_open_probe_is_unique_across_three_processes(monkeypatch, tmp_path):
    async def run():
        async with fixture_db(monkeypatch, tmp_path) as sessions:
            settings = SimpleNamespace(circuit_failure_threshold=1, circuit_window_seconds=60,
                                       circuit_open_seconds=30, circuit_probe_seconds=210)
            key = dependency_key("provider", 1, model_id=201)
            await finish(sessions, await acquire(sessions, key, settings), False, settings)
            async with sessions() as session:
                (await session.get(DependencyCircuit, key)).open_until = await now(session) - timedelta(seconds=1)
                await session.commit()
            program = '''
import asyncio,json,sys
from sqlalchemy.ext.asyncio import create_async_engine,async_sessionmaker
from eic.core.dependency_controls import acquire,DependencyDenied
from eic.core.config import get_settings
async def run():
 e=create_async_engine(sys.argv[1],connect_args={"timeout":30})
 s=async_sessionmaker(e,expire_on_commit=False)
 try:
  try:
   p=await acquire(s,sys.argv[2],get_settings())
   print(json.dumps({"granted":True,"state":p.state}))
  except DependencyDenied as ex:
   print(json.dumps({"granted":False,"code":ex.code}))
 finally: await e.dispose()
asyncio.run(run())
'''
            url = "sqlite+aiosqlite:///" + (tmp_path / "fixture.db").as_posix()
            processes = [subprocess.Popen([sys.executable, "-B", "-c", program, url, key],
                                          stdout=subprocess.PIPE, stderr=subprocess.PIPE) for _ in range(3)]
            observations = []
            for process in processes:
                stdout, stderr = process.communicate(timeout=25)
                assert process.returncode == 0, stderr.decode()
                observations.append({"exitCode": process.returncode, "stdout": stdout.decode(),
                                     "stderr": stderr.decode(), **json.loads(stdout)})
            assert sum(item["granted"] for item in observations) == 1
            if os.environ.get("EIC_TEST_ARTIFACT_DIR"):
                Path(os.environ["EIC_TEST_ARTIFACT_DIR"], "three-worker-probe.json").write_text(
                    json.dumps({"programSha256": hashlib.sha256(program.encode()).hexdigest(),
                                "observations": observations}, indent=2), encoding="utf-8")
    asyncio.run(run())


@pytest.mark.parametrize("reason,degraded", [("provider_error", True), ("provider_outcome_uncertain", False)])
def test_rule_fallback_never_masks_uncertain_provider(monkeypatch, tmp_path, reason, degraded):
    from eic import main
    from eic.ai.providers import ModelProviderError
    from eic.models import ChatMessage
    from test_chat_subscriptions import collect
    from test_p12_cloud_regressions import seed_chat

    async def run():
        async def provider(*args, **kwargs):
            raise ModelProviderError("PRIVATE_ERROR", reason=reason, retryable=reason == "provider_error")
            yield ""
        monkeypatch.setattr(main, "stream_chat_completion", provider)
        async with fixture_db(monkeypatch, tmp_path, is_super_admin=True) as sessions:
            await seed_chat(sessions, app_config={"chatModelID": 201, "ruleFallbackEnabled": True})
            output = await collect(main.sse_events("fixture", main.Principal(11, 1, "fixture", True)))
            assert sum("event: done\n" in item for item in output) == 1
            async with sessions() as session:
                message = await session.get(ChatMessage, 301)
                assert message.token_usage["degraded"] is degraded
                assert message.status == ("completed" if degraded else "failed")
                assert message.token_usage["providerInvocations"] == 1
    asyncio.run(run())


def test_rag_excerpt_rechecks_deleted_document_and_cites_only_used_rows(monkeypatch, tmp_path):
    from eic.ai.local_fallback import authorized_excerpt
    from eic.models import KnowledgeBase, KnowledgeChunk, KnowledgeDocument

    async def run():
        async with fixture_db(monkeypatch, tmp_path) as sessions:
            async with sessions() as session:
                session.add(KnowledgeBase(id=601, tenant_id=1, name="fixture"))
                await session.flush()
                session.add(KnowledgeDocument(id=701, tenant_id=1, knowledge_base_id=601, source_name="fixture",
                                              content="当前事实", source_revision=1, active_source_revision=1,
                                              active_index_version=801))
                await session.flush()
                session.add(KnowledgeChunk(id=901, tenant_id=1, knowledge_base_id=601, document_id=701,
                                           chunk_index=0, index_version=801, content="当前事实", content_hash="fixture"))
                await session.commit()
                rows = [{"documentId": 701, "chunkId": 901, "content": "缓存过时或注入内容"}]
                text, used = await authorized_excerpt(session, 1, [601], rows)
                assert text == "当前事实" and len(used) == 1
                document = await session.get(KnowledgeDocument, 701)
                document.is_deleted = True
                await session.commit()
                assert await authorized_excerpt(session, 1, [601], rows) == ("", [])
    asyncio.run(run())


def test_unconfigured_admission_rejects_before_provider(monkeypatch, tmp_path):
    from eic.ai.reliable_calls import reliable_stream
    from eic.ai.providers import ModelProviderError
    from eic.core.config import get_settings
    from eic.models import AIModel

    async def run():
        monkeypatch.setattr(get_settings(), "admission_enabled", True)
        async with fixture_db(monkeypatch, tmp_path) as sessions:
            model = AIModel(id=201, tenant_id=1, provider="openai", model_name="fixture",
                            endpoint="http://fixture", max_input_tokens=1024, max_output_tokens=512)
            calls = []
            async def provider():
                calls.append(1)
                yield "not called"
            with pytest.raises(ModelProviderError) as error:
                _ = [item async for item in reliable_stream(provider, model, sessions=sessions, metadata={})]
            assert error.value.reason == "budget_not_configured" and calls == []
    asyncio.run(run())


@pytest.mark.parametrize("deleted,degraded", [(False, True), (True, False)])
def test_chat_rag_fallback_uses_current_authorized_database(monkeypatch, tmp_path, deleted, degraded):
    from unittest.mock import AsyncMock
    from eic import main
    from eic.ai.providers import ModelProviderError
    from eic.models import ChatMessage, KnowledgeBase, KnowledgeChunk, KnowledgeDocument
    from test_chat_subscriptions import collect
    from test_p12_cloud_regressions import seed_chat

    async def run():
        async with fixture_db(monkeypatch, tmp_path, is_super_admin=True) as sessions:
            await seed_chat(sessions, app_config={
                "chatModelID": 201, "ragFallbackEnabled": True, "kmsIds": [601],
            })
            async with sessions() as session:
                session.add(KnowledgeBase(id=601, tenant_id=1, name="fixture"))
                await session.flush()
                session.add(KnowledgeDocument(id=701, tenant_id=1, knowledge_base_id=601,
                                              source_name="fixture", content="当前授权事实",
                                              source_revision=1, active_source_revision=1, active_index_version=801))
                await session.flush()
                session.add(KnowledgeChunk(id=901, tenant_id=1, knowledge_base_id=601, document_id=701,
                                           chunk_index=0, index_version=801, content="当前授权事实", content_hash="fixture"))
                await session.commit()
            monkeypatch.setattr(main, "retrieve_chat_documents_many", AsyncMock(return_value=[
                {"documentId": 701, "chunkId": 901, "knowledgeBaseId": 601, "content": "缓存内容"},
            ]))
            async def provider(*args, **kwargs):
                if deleted:
                    async with sessions() as session:
                        (await session.get(KnowledgeDocument, 701)).is_deleted = True
                        await session.commit()
                raise ModelProviderError("PRIVATE_ERROR", reason="provider_error", retryable=True)
                yield ""
            monkeypatch.setattr(main, "stream_chat_completion", provider)
            output = await collect(main.sse_events("fixture", main.Principal(11, 1, "fixture", True)))
            async with sessions() as session:
                message = await session.get(ChatMessage, 301)
                assert message.token_usage["degraded"] is degraded
                assert message.status == ("completed" if degraded else "failed")
                if degraded:
                    assert message.token_usage["model"] is None
                    assert message.token_usage["fallbackType"] == "rag_excerpt"
                    assert message.token_usage["citations"][0]["content"] == "当前授权事实"
                    assert any("当前授权事实" in item for item in output)
                else:
                    assert not any("当前授权事实" in item for item in output)
    asyncio.run(run())
