"""生产 Worker 的持久领取和写入 fencing；不重发未知模型调用。"""
from contextvars import ContextVar

from eic.core.agent_runtime import AgentLeaseLost, runtime_store
from eic.core.ids import new_id

OWNER: ContextVar[tuple[str, str] | None] = ContextVar("eic_agent_worker", default=None)


async def fence(session):
    owner = OWNER.get()
    if owner is None:
        return
    await new_id(session)
    await runtime_store.renew_worker(session, run_id=owner[0], worker_token=owner[1])


async def claim(session, run_id):
    await new_id(session)
    token = await runtime_store.claim_worker(session, run_id=run_id)
    if token is None:
        raise AgentLeaseLost("agent_worker_already_claimed")
    return token


async def release(session, run_id, token):
    await new_id(session)
    return await runtime_store.release_worker(session, run_id=run_id, worker_token=token)
