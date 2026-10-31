import asyncio
from unittest.mock import AsyncMock

import pytest
from eic.ai.providers import ModelProviderError
from eic.ai.reliable_calls import reliable_stream
from eic.core.config import get_settings
from eic.core import dependency_controls
from eic.models import AIModel
from test_id_generation_safety import fixture_db


@pytest.mark.parametrize("reason,partial,expected_calls", [
    ("provider_rate_limited", False, 2),
    ("provider_outcome_uncertain", False, 1),
    ("provider_rate_limited", True, 1),
])
def test_only_known_unstarted_rate_limit_is_retried(monkeypatch, tmp_path, reason, partial, expected_calls):
    async def run():
        monkeypatch.setattr(get_settings(), "dependency_controls_enabled", True)
        async with fixture_db(monkeypatch, tmp_path) as sessions:
            model = AIModel(id=201, tenant_id=1, model_type="chat", provider="openai",
                            model_name="fixture", endpoint="http://fixture")
            # 未落库的纯协议 fixture 必须给出参数预算。
            model.max_input_tokens, model.max_output_tokens = 1024, 512
            calls = []
            async def provider():
                calls.append(1)
                if len(calls) == 1:
                    if partial:
                        yield "partial"
                    raise ModelProviderError("PRIVATE_PROVIDER_ERROR", reason=reason,
                                             retryable=reason != "provider_outcome_uncertain")
                yield "answer"
            metadata = {}
            if expected_calls == 2:
                assert [item async for item in reliable_stream(provider, model, sessions=sessions,
                                                              metadata=metadata)] == ["answer"]
                assert metadata["retryCount"] == 1
            else:
                with pytest.raises(ModelProviderError):
                    _ = [item async for item in reliable_stream(provider, model, sessions=sessions, metadata=metadata)]
            assert len(calls) == expected_calls
            assert "PRIVATE_PROVIDER_ERROR" not in str(metadata)
    asyncio.run(run())


def test_chat_provider_guard_is_used_and_open_skips_primary(monkeypatch, tmp_path):
    from eic import main
    from eic.core.dependency_controls import acquire, dependency_key, finish
    from test_chat_subscriptions import collect
    from test_p12_cloud_regressions import seed_chat

    async def run():
        monkeypatch.setattr(get_settings(), "dependency_controls_enabled", True)
        monkeypatch.setattr(get_settings(), "circuit_failure_threshold", 1)
        async with fixture_db(monkeypatch, tmp_path, is_super_admin=True) as sessions:
            await seed_chat(sessions, two_models=True, app_config={"chatModelID": 201, "fallbackModelIds": [202]})
            key = dependency_key("provider", 1, "openai", 201, "http://fixture.invalid")
            await finish(sessions, await acquire(sessions, key, get_settings()), False, get_settings())
            calls = []
            async def provider(model, question, **kwargs):
                calls.append(model.id)
                yield "备用模型答案"
            monkeypatch.setattr(main, "stream_chat_completion", provider)
            await collect(main.sse_events("fixture", main.Principal(11, 1, "fixture", True)))
            assert calls == [202]
    asyncio.run(run())


def test_invalid_tool_args_and_user_cancel_do_not_poison_health(monkeypatch, tmp_path):
    from eic.ai.reliable_calls import reliable_tool
    from eic.ai.tool_center import ToolExecutionError
    from eic.core.dependency_controls import guard, dependency_key
    from eic.models import DependencyCircuit
    from test_p12_tool_calling import calculator_center

    async def run():
        monkeypatch.setattr(get_settings(), "dependency_controls_enabled", True)
        async with fixture_db(monkeypatch, tmp_path) as sessions:
            with pytest.raises(ToolExecutionError) as error:
                await reliable_tool(calculator_center(), "calculator", {"bad": 1}, {"calculator"},
                                    tenant_id=1, sessions=sessions)
            assert error.value.code == "invalid_tool_arguments"
            key = dependency_key("tool", 1, model_id="calculator")
            async with sessions() as session:
                assert await session.get(DependencyCircuit, key) is None
            with pytest.raises(asyncio.CancelledError):
                async with guard("provider", 1, model_id=201, sessions=sessions):
                    raise asyncio.CancelledError()
            async with sessions() as session:
                row = await session.get(DependencyCircuit, dependency_key("provider", 1, model_id=201))
                assert row.state == "CLOSED" and row.failures == 0
    asyncio.run(run())


def test_redis_failure_opens_only_redis_and_keeps_job_identity(monkeypatch, tmp_path):
    from eic.core import queue
    from redis.exceptions import RedisError
    from eic.core.dependency_controls import dependency_key
    from eic.models import DependencyCircuit

    async def run():
        monkeypatch.setattr(get_settings(), "dependency_controls_enabled", True)
        monkeypatch.setattr(get_settings(), "circuit_failure_threshold", 1)
        async with fixture_db(monkeypatch, tmp_path) as sessions:
            monkeypatch.setattr(dependency_controls, "SessionLocal", sessions)
            pool = AsyncMock()
            pool.enqueue_job.side_effect = RedisError("PRIVATE_REDIS_ERROR")
            monkeypatch.setattr(queue, "get_queue", AsyncMock(return_value=pool))
            assert await queue.enqueue_outbox(701, 1) is None
            assert await queue.enqueue_outbox(701, 1) is None
            pool.enqueue_job.assert_awaited_once()
            assert pool.enqueue_job.call_args.kwargs["_job_id"] == "eic-outbox:1:701"
            async with sessions() as session:
                key = dependency_key("redis", 1, endpoint=get_settings().redis_url)
                assert (await session.get(DependencyCircuit, key)).state == "OPEN"
                assert await session.get(DependencyCircuit, dependency_key("provider", 1, model_id=201)) is None
    asyncio.run(run())


def test_vector_connection_failure_is_lexical_fallback_without_domain_mix(monkeypatch, tmp_path):
    from eic.knowledge import ops
    from eic.models import DependencyCircuit, KnowledgeBase
    from eic.core.dependency_controls import dependency_key

    async def run():
        monkeypatch.setattr(get_settings(), "dependency_controls_enabled", True)
        monkeypatch.setattr(get_settings(), "circuit_failure_threshold", 1)
        async with fixture_db(monkeypatch, tmp_path) as sessions:
            monkeypatch.setattr(dependency_controls, "SessionLocal", sessions)
            monkeypatch.setattr(ops, "SessionLocal", sessions)
            monkeypatch.setattr(ops, "embed_texts", AsyncMock(return_value=[[1., 0.]]))
            connect = AsyncMock(side_effect=OSError("PRIVATE_VECTOR_ERROR"))
            monkeypatch.setattr(ops.asyncpg, "connect", connect)
            async with sessions() as session:
                session.add(AIModel(id=201, tenant_id=1, model_type="embedding", provider="openai",
                                    model_name="fixture", endpoint="http://fixture"))
                session.add(KnowledgeBase(id=601, tenant_id=1, name="fixture", embedding_model_id=201))
                await session.commit()
                base = await session.get(KnowledgeBase, 601)
                assert await ops._search_dense(base=base, tenant_id=1, query="fixture",
                                               limit=2, filters=None, session=session) == ([], "unavailable")
                url = get_settings().vector_database_url.replace("postgresql+asyncpg://", "postgresql://", 1)
                row = await session.get(DependencyCircuit, dependency_key("vector", 1, model_id=601, endpoint=url))
                assert row.state == "OPEN"
    asyncio.run(run())


def test_generator_close_and_embedding_error_have_distinct_health(monkeypatch, tmp_path):
    import httpx
    from eic.ai.embeddings import embed_texts
    from eic.core.dependency_controls import guard, dependency_key
    from eic.models import DependencyCircuit

    async def run():
        monkeypatch.setattr(get_settings(), "dependency_controls_enabled", True)
        monkeypatch.setattr(get_settings(), "circuit_failure_threshold", 1)
        async with fixture_db(monkeypatch, tmp_path) as sessions:
            monkeypatch.setattr(dependency_controls, "SessionLocal", sessions)
            with pytest.raises(GeneratorExit):
                async with guard("provider", 1, model_id=201, sessions=sessions):
                    raise GeneratorExit()
            model = AIModel(id=202, tenant_id=1, model_type="embedding", provider="openai",
                            model_name="fixture", endpoint="http://fixture", enabled=True)
            async with httpx.AsyncClient(transport=httpx.MockTransport(
                lambda _: httpx.Response(503, json={"error": "PRIVATE"})
            )) as client:
                with pytest.raises(ModelProviderError):
                    await embed_texts(model, ["中文"], tenant_id=1, client=client)
            async with sessions() as session:
                closed = await session.get(DependencyCircuit, dependency_key("provider", 1, model_id=201))
                opened = await session.get(DependencyCircuit, dependency_key(
                    "embedding", 1, provider="openai", model_id=202, endpoint="http://fixture"))
                assert closed.state == "CLOSED" and closed.failures == 0
                assert opened.state == "OPEN"
    asyncio.run(run())
