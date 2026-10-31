import asyncio

import pytest

from eic.knowledge import indexing
from eic.models import KnowledgeBase, KnowledgeDocument, Resource
from test_index_lifecycle import fixture, projection


@pytest.mark.parametrize("state", ["failed", "uncertain"])
def test_base_summary_preserves_failure_after_source_revision_change(tmp_path, monkeypatch, state):
    async def run():
        async with fixture(tmp_path, monkeypatch) as sessions:
            async with sessions() as session:
                document = await session.get(KnowledgeDocument, 20)
                document.source_revision += 1
                document.index_status = state
                document.status = state
                await indexing.refresh_base_status(session, 10, 1)
                await session.commit()
            async with sessions() as session:
                assert (await session.get(KnowledgeBase, 10)).status == state
                assert (await session.get(Resource, 10)).data["indexStatus"] == state
    asyncio.run(run())


@pytest.mark.parametrize("embedding", [True, False])
def test_empty_base_rebuild_reports_empty_not_ready(tmp_path, monkeypatch, embedding):
    async def run():
        async with fixture(tmp_path, monkeypatch, embedding=embedding) as sessions:
            projection(monkeypatch)
            async with sessions() as session:
                (await session.get(KnowledgeDocument, 20)).is_deleted = True
                await session.commit()
            result = await indexing.process_knowledge_base({}, 10, 1)
            assert result["status"] == "empty" and result["document_count"] == 0
            async with sessions() as session:
                assert (await session.get(KnowledgeBase, 10)).status == "empty"
                resource = await session.get(Resource, 10)
                assert resource.data["indexStatus"] == "empty"
                assert resource.data["indexVersions"] == {}
                assert resource.data["indexLeaseUntil"] is None
            indexing.embed_texts.assert_not_awaited()
    asyncio.run(run())
