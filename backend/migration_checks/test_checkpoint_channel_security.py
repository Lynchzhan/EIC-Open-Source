"""合成SQLite上的真实saver加密/恢复；不调用模型、MCP或外部工具。"""
import copy
from dataclasses import replace
from typing import TypedDict

import pytest
import pytest_asyncio
from langchain_core.messages import HumanMessage
from langgraph.checkpoint.base import empty_checkpoint
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, Interrupt, Send, interrupt
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from eic.ai.checkpoint_security import CheckpointReadScope, sanitize_checkpoint
from eic.ai.checkpointer import DatabaseCheckpointer
from eic.core import agent_worker
from eic.core.agent_runtime import runtime_store
from eic.core.ids import initialize_allocator
from eic.core.security import _fernet
from eic.models import AgentGraphCheckpoint, AgentGraphWrite, Base, Tenant, User

MARKER = 'synthetic-never-persist-credential-9f2741'


@pytest.mark.parametrize('channel', ['password', 'api_key', 'API-Key', 'Authorization',
                                    'client_secret', 'access-token', 'credentials', 'session_cookie'])
def test_sensitive_channel_name_removed_without_mutation(channel):
    cp = empty_checkpoint()
    cp['channel_values'] = {channel: MARKER, 'safe': 7, 'token_count': 12}
    cp['channel_versions'] = {channel: 'version-1', 'safe': 'version-2'}
    cp['versions_seen'] = {'node': {channel: 'version-1'}}
    before = copy.deepcopy(cp)
    safe = sanitize_checkpoint(cp)
    assert channel not in safe['channel_values']
    assert safe['channel_values'] == {'safe': 7, 'token_count': 12}
    assert {k:v for k,v in safe.items() if k != 'channel_values'} == {
        k:v for k,v in before.items() if k != 'channel_values'}
    assert cp == before


def test_typed_control_channels_and_nested_credentials_preserved_safely():
    cp = empty_checkpoint()
    message = HumanMessage(content='safe', additional_kwargs={'api_key': MARKER, 'safe': True})
    cp['channel_values'] = {'messages': [message], '__interrupt__': (Interrupt({'password':MARKER,'ok':1}),),
                            '__resume__': {'authorization':MARKER,'approved':True},
                            '__tasks__': [Send('node', {'api_key':MARKER,'keep':2})]}
    safe = sanitize_checkpoint(cp)
    assert isinstance(safe['channel_values']['messages'][0], HumanMessage)
    assert safe['channel_values']['messages'][0].additional_kwargs == {'safe':True}
    assert isinstance(safe['channel_values']['__interrupt__'][0], Interrupt)
    assert safe['channel_values']['__interrupt__'][0].value == {'ok':1}
    assert safe['channel_values']['__resume__'] == {'approved':True}
    assert safe['channel_values']['__tasks__'][0].arg == {'keep':2}
    assert MARKER not in repr(safe)
    assert message.additional_kwargs['api_key'] == MARKER


@pytest_asyncio.fixture
async def store(tmp_path):
    engine = create_async_engine('sqlite+aiosqlite:///'+(tmp_path/'synthetic.db').as_posix())
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with sessions() as session:
        session.add(Tenant(id=7,code=7,name='synthetic'))
        await session.commit()
        session.add(User(id=44,tenant_id=7,username='synthetic',password_hash='disabled',is_active=True))
        await initialize_allocator(session)
        await session.commit()
        run = await runtime_store.create_run(session,tenant_id=7,session_id=None,request_id=None,
            graph_version='synthetic-v1',agent_run_id='synthetic-channel-run')
        await session.commit()
        token = await agent_worker.claim(session,run.run_id)
        await session.commit()
    assert token is not None
    async def authorize(session, run, scope):
        owner = await session.get(User,scope.owner_id)
        return bool(owner and owner.is_active and owner.tenant_id == scope.tenant_id == run.tenant_id)
    saver = DatabaseCheckpointer(sessions=sessions,read_authorizer=authorize)
    scope = CheckpointReadScope(run.run_id,7,44,None,None,'synthetic-v1')
    config = {'configurable':{'thread_id':run.run_id,'checkpoint_ns':''}}
    slot = agent_worker.OWNER.set((run.run_id,token))
    try:
        yield saver,sessions,scope,config
    finally:
        agent_worker.OWNER.reset(slot)
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize('phase', ['checkpoint','pending'])
async def test_real_persisted_payload_and_scoped_read_do_not_leak(store, phase):
    saver,sessions,scope,config = store
    cp = empty_checkpoint()
    cp['channel_values'] = {'safe': {'keep':1,'api_key':MARKER}, 'password':MARKER}
    cp['channel_versions'] = {'safe':'1','password':'2'}
    before = copy.deepcopy(cp)
    saved = await saver.aput(config,cp,{'source':'input','password':MARKER},{})
    writes = [('password',MARKER), ('safe',{'keep':2,'api_key':MARKER}),
              ('api_key',MARKER), ('__resume__',{'approved':True,'authorization':MARKER}),
              ('safe_tail',3)]
    await saver.aput_writes(saved,writes,'synthetic-task')
    async with sessions() as session:
        rows = list(await session.scalars(select(AgentGraphCheckpoint if phase == 'checkpoint' else AgentGraphWrite)))
        assert rows
        for row in rows:
            assert MARKER.encode() not in _fernet().decrypt(row.payload_encrypted.encode('ascii'))
        persisted_writes = list(await session.scalars(select(AgentGraphWrite).order_by(AgentGraphWrite.write_index)))
        assert [w.channel for w in persisted_writes] == ['__resume__','safe','safe_tail']
        assert next(w for w in persisted_writes if w.channel == 'safe_tail').write_index == 4
    with saver.authorized_read_scope(scope):
        item = await saver.aget_tuple(saved)
    assert item.checkpoint['channel_values'] == {'safe':{'keep':1}}
    assert item.checkpoint['channel_versions'] == cp['channel_versions']
    assert MARKER not in repr(item)
    assert cp == before and writes[0][1] == MARKER
    with saver.authorized_read_scope(replace(scope,tenant_id=8)):
        with pytest.raises(PermissionError,match='scope_mismatch'):
            await saver.aget_tuple(saved)


@pytest.mark.asyncio
@pytest.mark.parametrize('tamper', [False, True])
async def test_legacy_sensitive_write_read_filter_still_verifies_binding(store,tamper):
    saver,sessions,scope,config = store
    cp = empty_checkpoint()
    cp['channel_values'] = {'safe':1,'password':MARKER}
    key = [scope.run_id,'',cp['id']]
    kind,encrypted = saver._encode(key,{'checkpoint':cp,'metadata':{}})
    write_key = [*key,'legacy-task',0,'password']
    write_kind,write_encrypted = saver._encode([*write_key[:-1],'other'] if tamper else write_key,MARKER)
    async with sessions() as session:
        session.add(AgentGraphCheckpoint(run_id=scope.run_id,namespace='',checkpoint_id=cp['id'],
            graph_version=scope.graph_version,schema_version=1,payload_type=kind,payload_encrypted=encrypted))
        session.add(AgentGraphWrite(run_id=scope.run_id,namespace='',checkpoint_id=cp['id'],
            task_id='legacy-task',write_index=0,channel='password',payload_type=write_kind,payload_encrypted=write_encrypted))
        await session.commit()
    with saver.authorized_read_scope(scope):
        if tamper:
            with pytest.raises(ValueError,match='binding_mismatch'):
                await saver.aget_tuple(saver._config(*key))
        else:
            item = await saver.aget_tuple(saver._config(*key))
            assert item.checkpoint['channel_values'] == {'safe':1}
            assert item.pending_writes == [] and MARKER not in repr(item)


class ResumeState(TypedDict):
    safe: str


@pytest.mark.asyncio
async def test_actual_langgraph_interrupt_and_resume_with_database_saver(store):
    saver,_,scope,config = store
    def approve(state):
        value = interrupt({'safe':state['safe']})
        return {'safe':value['safe']}
    graph = StateGraph(ResumeState)
    graph.add_node('approve',approve)
    graph.add_edge(START,'approve')
    graph.add_edge('approve',END)
    compiled = graph.compile(checkpointer=saver)
    with saver.authorized_read_scope(scope):
        paused = await compiled.ainvoke({'safe':'before'},config)
        assert paused['__interrupt__']
        item = await saver.aget_tuple(config)
        assert any(w[1] == '__interrupt__' for w in item.pending_writes)
        result = await compiled.ainvoke(Command(resume={'safe':'approved'}),config)
    assert result == {'safe':'approved'}
