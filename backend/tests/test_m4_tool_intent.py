import asyncio
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.exc import SQLAlchemyError
from eic.models import Base, ToolExecutionIntent
from eic.core.tool_intent import (
    execute_with_tool_intent, begin_tool_intent, ToolIntentConflict, ToolIntentInProgress,
)


@pytest.mark.parametrize('mode', ['replay', 'conflict', 'concurrent', 'first_failure', 'late_failure', 'tenant', 'no_key', 'authorization', 'restart'])
def test_durable_intent(tmp_path, mode):
    async def run():
        engine = create_async_engine('sqlite+aiosqlite:///' + (tmp_path / 'intent.db').as_posix())
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        calls = []
        def handler():
            calls.append(1)
            return {'access_token': 'sensitive-sentinel', 'ok': True}
        args = dict(tenant_id=1, user_id=11, tool_name='local:write', normalized_input={'b': 2, 'a': 1}, idempotency_key='key', authorize=lambda: None)
        async def execute(**changes):
            async with sessions() as session:
                return await execute_with_tool_intent(session, handler=handler, **{**args, **changes})
        try:
            if mode == 'first_failure':
                async with sessions() as session:
                    session.commit = AsyncMock(side_effect=SQLAlchemyError('first'))
                    with pytest.raises(SQLAlchemyError):
                        await execute_with_tool_intent(session, handler=handler, **args)
                assert calls == []
                return
            if mode == 'late_failure':
                async with sessions() as session:
                    original = session.commit
                    count = 0
                    async def commit():
                        nonlocal count
                        count += 1
                        if count == 2:
                            raise SQLAlchemyError('late')
                        await original()
                    session.commit = commit
                    with pytest.raises(SQLAlchemyError):
                        await execute_with_tool_intent(session, handler=handler, **args)
                with pytest.raises(ToolIntentInProgress):
                    await execute()
                assert len(calls) == 1
                return
            if mode in {'concurrent', 'restart'}:
                async def claim():
                    async with sessions() as session:
                        return await begin_tool_intent(session, **args)
                if mode == 'concurrent':
                    results = await asyncio.gather(claim(), claim(), return_exceptions=True)
                    assert sum(isinstance(x, ToolIntentInProgress) for x in results) == 1
                else:
                    await claim()
                    await engine.dispose()
                    with pytest.raises(ToolIntentInProgress):
                        await execute()
                assert calls == []
                return
            first = await execute()
            assert not first.replayed
            if mode == 'conflict':
                with pytest.raises(ToolIntentConflict):
                    await execute(normalized_input={'a': 2})
            elif mode == 'authorization':
                def denied():
                    raise PermissionError('revoked')
                with pytest.raises(PermissionError):
                    await execute(authorize=denied)
            elif mode == 'tenant':
                assert not (await execute(tenant_id=2)).replayed
            elif mode == 'no_key':
                await execute(idempotency_key=None)
                await execute(idempotency_key=None)
            else:
                replay = await execute(normalized_input={'a': 1, 'b': 2})
                assert replay.replayed and replay.result == first.result
                async with sessions() as session:
                    row = await session.get(ToolExecutionIntent, first.intent_id)
                    assert 'sensitive-sentinel' not in row.result_encrypted
            assert len(calls) == (2 if mode == 'tenant' else 3 if mode == 'no_key' else 1)
        finally:
            await engine.dispose()
    asyncio.run(run())
