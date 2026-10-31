import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from eic.knowledge import indexing, ops
from eic.models import AIModel, KnowledgeBase, KnowledgeDocument, KnowledgeIngestionRun, Resource
from test_index_lifecycle import fixture, projection


def test_share_revocation_between_parse_and_embedding(tmp_path, monkeypatch):
    async def run():
        async with fixture(tmp_path, monkeypatch) as sessions:
            projection(monkeypatch)
            await indexing.process_knowledge_document({}, 20, 1)
            async with sessions() as s:
                s.add(Resource(id=71, tenant_id=1, resource_type="files", name="f", data={"accessMode": "private"}))
                (await s.get(KnowledgeDocument, 20)).file_id = 71
                await s.commit()
            provider = indexing.embed_texts
            result = await indexing.process_document_embeddings({}, 20, 1)
            assert result["status"] == "embedding_failed" and provider.await_count == 0
    asyncio.run(run())


def test_expired_lease_blocks_late_completion_and_automatic_replay(tmp_path, monkeypatch):
    async def run():
        async with fixture(tmp_path, monkeypatch) as sessions:
            projection(monkeypatch)
            version = (await indexing.process_knowledge_document({}, 20, 1))["index_version"]
            async with sessions() as s:
                r = await s.get(KnowledgeIngestionRun, version)
                r.status = "embedding"
                r.lease_until = datetime.now(UTC) - timedelta(seconds=10)
                await s.commit()
            assert await indexing.recover_index_runs() == 1
            assert await indexing.recover_index_runs() == 0
            async with sessions() as s:
                d = await s.get(KnowledgeDocument, 20)
                assert d.status == "uncertain" and not d.index_error["automaticRetry"]
                with pytest.raises(ValueError):
                    await indexing.valid_run(s, 20, 1, version)
            assert (await indexing.process_knowledge_document({}, 20, 1))["status"] == "uncertain"
            assert indexing.embed_texts.await_count == 0
            await indexing.record_failure(20, 1, version, "embedding_failed:ValueError", "embedding")
            async with sessions() as s:
                assert (await s.get(KnowledgeIngestionRun, version)).status == "uncertain"
                assert (await s.get(KnowledgeDocument, 20)).status == "uncertain"
    asyncio.run(run())


def test_stale_revision_event_does_not_parse_new_input(tmp_path, monkeypatch):
    async def run():
        async with fixture(tmp_path, monkeypatch) as sessions:
            async with sessions() as s:
                (await s.get(KnowledgeDocument, 20)).source_revision = 1
                await s.commit()
            result = await indexing.process_knowledge_document({"source_revision": 0}, 20, 1)
            assert result["status"] == "superseded"
            async with sessions() as s:
                assert not list(await s.scalars(select(KnowledgeIngestionRun)))
    asyncio.run(run())


def test_changed_configuration_supersedes_pending_version(tmp_path, monkeypatch):
    async def run():
        async with fixture(tmp_path, monkeypatch) as sessions:
            projection(monkeypatch)
            first = (await indexing.process_knowledge_document({}, 20, 1))["index_version"]
            async with sessions() as s:
                kb = await s.get(KnowledgeBase, 10)
                kb.retrieval_config = {**kb.retrieval_config, "childSize": 20}
                await s.commit()
            second = (await indexing.process_knowledge_document({}, 20, 1))["index_version"]
            assert first != second
            assert (await indexing.process_document_embeddings({"index_version": first}, 20, 1))["status"] == "embedding_failed"
            async with sessions() as s:
                assert (await s.get(KnowledgeDocument, 20)).pending_index_version == second
            assert (await indexing.process_document_embeddings({"index_version": second}, 20, 1))["status"] == "ready"
            async with sessions() as s:
                assert (await s.get(KnowledgeIngestionRun, first)).status == "superseded"
    asyncio.run(run())


def test_changed_embedding_model_excludes_old_dense_projection(tmp_path, monkeypatch):
    async def run():
        async with fixture(tmp_path, monkeypatch) as sessions:
            projection(monkeypatch)
            await indexing.process_knowledge_document({}, 20, 1)
            await indexing.process_document_embeddings({}, 20, 1)
            dense = AsyncMock(return_value=([], "ok"))
            monkeypatch.setattr(ops, "_search_dense", dense)
            async with sessions() as s:
                model = await s.get(AIModel, 9)
                model.model_name = "changed-same-dimensions"
                await s.commit()
            async with sessions() as s:
                kb = await s.get(KnowledgeBase, 10)
                result = await ops.retrieve_knowledge(s, tenant_id=1, base=kb, query="obsolete")
                assert result["denseStatus"] == "reindex_required"
                assert result["results"]  # 同正文的 BM25 保留，不混用新旧 embedding 空间。
            dense.assert_not_awaited()
    asyncio.run(run())


def test_full_build_expired_lease_recovers_without_provider_replay(tmp_path, monkeypatch):
    async def run():
        async with fixture(tmp_path, monkeypatch) as sessions:
            provider = AsyncMock()
            monkeypatch.setattr(indexing, "embed_texts", provider)
            async with sessions() as s:
                resource = await s.get(Resource, 10)
                resource.data = {"indexStatus": "building", "indexBuildId": "123",
                                 "indexLeaseUntil": (datetime.now(UTC) - timedelta(seconds=1)).isoformat()}
                (await s.get(KnowledgeBase, 10)).status = "building"
                await s.commit()
            assert await indexing.recover_index_runs() == 1
            assert await indexing.recover_index_runs() == 0
            async with sessions() as s:
                resource = await s.get(Resource, 10)
                assert resource.data["indexStatus"] == "uncertain"
                assert resource.data["indexBuildId"] == "123"
                assert not resource.data["indexError"]["automaticRetry"]
                assert (await s.get(KnowledgeBase, 10)).status == "uncertain"
            provider.assert_not_awaited()
    asyncio.run(run())


def test_lease_expiry_blocks_write_without_recovery_poll(tmp_path, monkeypatch):
    async def run():
        async with fixture(tmp_path, monkeypatch) as sessions:
            connection = projection(monkeypatch)
            await indexing.process_knowledge_document({}, 20, 1)
            async def provider(model, texts, **kwargs):
                async with sessions() as s:
                    d = await s.get(KnowledgeDocument, 20)
                    r = await s.get(KnowledgeIngestionRun, d.pending_index_version)
                    r.lease_until = datetime.now(UTC) - timedelta(seconds=1)
                    await s.commit()
                return [[1., 0.] for _ in texts]
            monkeypatch.setattr(indexing, "embed_texts", provider)
            assert (await indexing.process_document_embeddings({}, 20, 1))["status"] == "embedding_failed"
            assert not connection.rows
            async with sessions() as s:
                assert (await s.get(KnowledgeDocument, 20)).status == "uncertain"
    asyncio.run(run())


def test_legacy_ready_without_projection_evidence_is_not_ready():
    from types import SimpleNamespace
    assert ops.display_document_status(SimpleNamespace(status="ready", index_status="legacy"), 20) == "legacy"


def test_explicit_retry_requires_acknowledgment_and_preserves_history(tmp_path, monkeypatch):
    from eic import main
    from fastapi import BackgroundTasks
    async def run():
        async with fixture(tmp_path, monkeypatch) as sessions:
            projection(monkeypatch)
            version = (await indexing.process_knowledge_document({}, 20, 1))["index_version"]
            await indexing.record_failure(20, 1, version, "interrupted", "embedding")
            from eic.core import authorization
            monkeypatch.setattr(authorization, "has_permission", AsyncMock(return_value=True))
            endpoint = next(r.endpoint for r in main.app.routes if getattr(r, "path", "") == "/api/AIKmss/RetryDocument")
            actor = main.Principal(11, 1, "fixture", True)
            async with sessions() as s:
                with pytest.raises(main.HTTPException) as error:
                    await endpoint({"id": 20}, BackgroundTasks(), principal=actor, session=s)
                assert error.value.status_code == 409
            async with sessions() as s:
                result = await endpoint({"id": 20, "acknowledgeDuplicateCall": True}, BackgroundTasks(), principal=actor, session=s)
                assert result["automaticRetry"] is False
            next_version = (await indexing.process_knowledge_document({}, 20, 1))["index_version"]
            assert next_version != version
            async with sessions() as s:
                previous = await s.get(KnowledgeIngestionRun, version)
                assert previous.status == "superseded" and previous.error_message == "interrupted"
                assert (await s.get(KnowledgeDocument, 20)).pending_index_version == next_version
            assert indexing.embed_texts.await_count == 0
    asyncio.run(run())


def test_failed_projection_cleanup_preserves_active_and_relational_history(tmp_path, monkeypatch):
    from eic.models import KnowledgeChunk
    async def run():
        async with fixture(tmp_path, monkeypatch) as sessions:
            connection = projection(monkeypatch)
            await indexing.process_knowledge_document({}, 20, 1)
            await indexing.process_document_embeddings({}, 20, 1)
            original_rows = set(connection.rows)
            async with sessions() as s:
                kb = await s.get(KnowledgeBase, 10)
                kb.retrieval_config = {**kb.retrieval_config, "childSize": 20}
                await s.commit()
            version = (await indexing.process_knowledge_document({}, 20, 1))["index_version"]
            original = indexing.PgVectorStore.upsert_many
            async def changed(store, pg, chunks):
                await original(store, pg, chunks)
                async with sessions() as s:
                    (await s.get(AIModel, 9)).model_name = "changed-before-activation"
                    await s.commit()
            monkeypatch.setattr(indexing.PgVectorStore, "upsert_many", changed)
            assert (await indexing.process_document_embeddings({}, 20, 1))["status"] == "embedding_failed"
            assert set(connection.rows) > original_rows
            connection.fetchval = AsyncMock(return_value=True)
            async def remove(store, pg, *, tenant_id, chunk_ids):
                assert tenant_id == 1
                for chunk_id in chunk_ids:
                    pg.rows.pop(chunk_id, None)
            monkeypatch.setattr(indexing.PgVectorStore, "delete_chunks", remove)
            await indexing.cleanup_document_index({}, 20, 1)
            assert set(connection.rows) == original_rows
            await indexing.cleanup_document_index({}, 20, 1)
            assert set(connection.rows) == original_rows
            async with sessions() as s:
                assert (await s.get(KnowledgeIngestionRun, version)).status == "failed"
                assert list(await s.scalars(select(KnowledgeChunk).where(KnowledgeChunk.index_version == version)))
    asyncio.run(run())


def test_file_and_text_full_rebuild_uses_dedicated_documents(tmp_path, monkeypatch):
    async def run():
        async with fixture(tmp_path, monkeypatch) as sessions:
            projection(monkeypatch)
            path = tmp_path / "fixture.txt"
            path.write_text("synthetic file policy " * 10)
            monkeypatch.setattr(indexing, "file_source_path", lambda file: path)
            async with sessions() as s:
                s.add(Resource(id=71, tenant_id=1, resource_type="files", name="fixture.txt",
                               data={"path": str(path), "accessMode": "tenant"}))
                await s.flush()
                s.add(KnowledgeDocument(id=21, tenant_id=1, knowledge_base_id=10, file_id=71,
                                       source_name="fixture.txt", content="", status="queued"))
                await s.commit()
            result = await indexing.process_knowledge_base({}, 10, 1)
            assert result["status"] == "ready" and result["document_count"] == 2
            async with sessions() as s:
                file = await s.get(KnowledgeDocument, 21)
                assert "synthetic file policy" in file.content and file.parse_status == "ready"
                (await s.get(AIModel, 9)).model_name = "new-model-same-dimensions"
                await s.commit()
            again = await indexing.process_knowledge_base({}, 10, 1)
            assert again["status"] == "ready"
            assert all(again["versions"][k] != result["versions"][k] for k in result["versions"])
    asyncio.run(run())
