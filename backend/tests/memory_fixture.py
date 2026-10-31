"""显式启用合成智能体的记忆，保留产品默认关闭。"""
from eic.models import AIApp, ChatSession, Resource


async def enable_memory(session, *, agent_id=7, session_id=101):
    if await session.get(AIApp, agent_id) is None:
        session.add(AIApp(id=agent_id, tenant_id=1, name='memory-fixture'))
    if await session.get(Resource, agent_id) is None:
        session.add(Resource(id=agent_id, tenant_id=1, owner_id=11, resource_type='ai_apps',
            name='memory-fixture', data={'enabled':True, 'memorySettings':{'mode':'manual'}}))
    if session_id:
        (await session.get(ChatSession, session_id)).app_id=agent_id
    await session.flush()
