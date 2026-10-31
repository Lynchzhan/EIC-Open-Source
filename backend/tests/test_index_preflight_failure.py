"""入库版本建立前失败必须落盘，旧事件不得覆盖新输入或其他租户。"""
import asyncio

import pytest

from eic.knowledge import indexing
from eic.models import AIModel, KnowledgeDocument
from test_index_lifecycle import fixture


def test_invalid_model_preflight_ends_queued_document(tmp_path, monkeypatch):
    async def run():
        async with fixture(tmp_path, monkeypatch) as sessions:
            async with sessions() as session:
                (await session.get(AIModel, 9)).enabled = False
                await session.commit()
            result = await indexing.process_knowledge_document({'source_revision': 0}, 20, 1)
            assert result['status'] == 'failed'
            async with sessions() as session:
                document = await session.get(KnowledgeDocument, 20)
                assert document.status == document.index_status == 'failed'
                assert document.parse_status == 'failed'
                assert document.pending_index_version is None
                assert document.index_error == {'stage': 'parse', 'code': 'parse_failed:HTTPException', 'automaticRetry': False}
    asyncio.run(run())


@pytest.mark.parametrize('mode', ['new_revision', 'ready', 'cancelled', 'new_version', 'wrong_tenant', 'deleted'])
def test_preflight_failure_cas_preserves_newer_or_foreign_state(tmp_path, monkeypatch, mode):
    async def run():
        async with fixture(tmp_path, monkeypatch) as sessions:
            async with sessions() as session:
                document = await session.get(KnowledgeDocument, 20)
                if mode == 'new_revision':
                    document.source_revision += 1
                elif mode in {'ready', 'cancelled'}:
                    document.status = mode
                elif mode == 'new_version':
                    document.pending_index_version = 99
                elif mode == 'deleted':
                    document.is_deleted = True
                await session.commit()
                before = (document.status, document.index_status, dict(document.index_error or {}))
            await indexing.record_failure(20, 2 if mode == 'wrong_tenant' else 1, None,
                'parse_failed:ValueError', 'parse', preflight_revision=0, preflight_pending=None)
            async with sessions() as session:
                document = await session.get(KnowledgeDocument, 20)
                assert (document.status, document.index_status, dict(document.index_error or {})) == before
    asyncio.run(run())


def test_superseded_event_does_not_inspect_invalid_model(tmp_path, monkeypatch):
    async def run():
        async with fixture(tmp_path, monkeypatch) as sessions:
            async with sessions() as session:
                (await session.get(AIModel, 9)).enabled = False
                (await session.get(KnowledgeDocument, 20)).source_revision = 1
                await session.commit()
            result = await indexing.process_knowledge_document({'source_revision': 0}, 20, 1)
            assert result['status'] == 'superseded'
            async with sessions() as session:
                assert (await session.get(KnowledgeDocument, 20)).status == 'queued_text'
    asyncio.run(run())
