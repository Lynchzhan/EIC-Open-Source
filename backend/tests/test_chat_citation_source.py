"""引用文件必须属于当前用户原请求、原引用及当前可访问文件。"""
import asyncio

import pytest
from fastapi import HTTPException
from test_id_generation_safety import fixture_db


@pytest.mark.parametrize('scenario', ['valid', 'other_owner', 'uncited', 'deleted_document', 'private_file', 'deleted_base', 'cross_tenant'])
def test_citation_source_scope(monkeypatch, tmp_path, scenario):
    from eic import main
    from eic.models import ChatMessage, KnowledgeBase, KnowledgeDocument, Resource, User

    async def run():
        async with fixture_db(monkeypatch, tmp_path, is_super_admin=True) as sessions:
            async with sessions() as session:
                session.add(User(id=12, tenant_id=1, username='other', password_hash='unused', is_super_admin=True))
                session.add(Resource(id=401, tenant_id=1, owner_id=11, resource_type='knowledge_bases',
                                     name='fixture', is_deleted=scenario == 'deleted_base'))
                session.add(KnowledgeBase(id=401, tenant_id=1, name='fixture'))
                session.add(Resource(id=403, tenant_id=1, owner_id=12 if scenario == 'private_file' else 11,
                                     resource_type='files', name='fixture.pdf', data={'accessMode': 'private'}))
                await session.flush()
                session.add(KnowledgeDocument(id=402, tenant_id=2 if scenario == 'cross_tenant' else 1,
                                             knowledge_base_id=401, file_id=403, source_name='fixture.pdf',
                                             is_deleted=scenario == 'deleted_document'))
                session.add(ChatMessage(id=302, tenant_id=1, session_id=101, role='assistant',
                                        request_id='fixture', content='fixture',
                                        token_usage={'citations': [{'documentId': '402', 'pageNumbers': [1]}]}))
                await session.commit()
            async with sessions() as session:
                principal = main.Principal(12 if scenario == 'other_owner' else 11, 1, 'fixture', True)
                if scenario == 'valid':
                    result = await main.chat_citation_source('fixture', '402', principal, session)
                    assert result == {'fileId': '403', 'documentId': '402'}
                else:
                    with pytest.raises(HTTPException) as error:
                        await main.chat_citation_source('fixture', '999' if scenario == 'uncited' else '402', principal, session)
                    assert error.value.status_code == 404
    asyncio.run(run())
