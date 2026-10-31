"""整体 Review：运行身份、崩溃收口和锁版本构建的发布回归。"""
import asyncio
from datetime import timedelta
import json
from pathlib import Path
import sys

import pytest
from sqlalchemy import func, select, update

from test_id_generation_safety import claim, fixture_db


def test_runtime_request_identity_is_idempotent(monkeypatch, tmp_path):
    from eic.core.agent_runtime import AgentRuntimeStore
    from eic.models import AgentRun

    async def run():
        async with fixture_db(monkeypatch, tmp_path) as sessions:
            store = AgentRuntimeStore()
            async with sessions() as session:
                first = await store.create_run(
                    session, tenant_id=1, session_id=101, request_id="fixture",
                    graph_version="review-v1",
                )
                await session.commit()
            async with sessions() as session:
                repeated = await store.create_run(
                    session, tenant_id=1, session_id=101, request_id="fixture",
                    graph_version="review-v1",
                )
                assert repeated.run_id == first.run_id
                assert await session.scalar(select(func.count()).select_from(AgentRun)) == 1
                with pytest.raises(ValueError, match="request_identity_conflict"):
                    await store.create_run(
                        session, tenant_id=1, session_id=101, request_id="fixture",
                        graph_version="changed-v2",
                    )
    asyncio.run(run())


@pytest.mark.parametrize("already_terminal", [False, True])
def test_generation_crash_closes_matching_agent_run_without_replay(
    monkeypatch, tmp_path, already_terminal,
):
    from eic.core import generation
    from eic.core.agent_runtime import AgentRuntimeStore
    from eic.models import AgentRun, AgentRunLease, ChatEvent, GenerationLease

    async def run():
        async with fixture_db(monkeypatch, tmp_path) as sessions:
            await claim(sessions)
            async with sessions() as session:
                store = AgentRuntimeStore()
                agent = await store.create_run(
                    session, tenant_id=1, session_id=101, request_id="fixture",
                    graph_version="review-v1",
                )
                if already_terminal:
                    await store.finish_run(session, run_id=agent.run_id, status="completed")
                await session.execute(update(GenerationLease).values(
                    expires_at=await generation.database_now(session) - timedelta(seconds=1),
                ))
                await session.commit()
            assert await generation.recover_expired() == 1
            assert await generation.recover_expired() == 0
            async with sessions() as session:
                stored = await session.get(AgentRun, agent.run_id)
                assert stored.status == ("completed" if already_terminal else "failed")
                assert stored.finished_at is not None
                assert (await session.get(AgentRunLease, agent.run_id)).state == "finished"
                if not already_terminal:
                    assert stored.error_code == "producer_lease_expired"
                    assert stored.result["providerOutcome"] == "uncertain"
                    assert stored.result["automaticRetry"] is False
                assert await session.scalar(
                    select(func.count()).select_from(ChatEvent).where(ChatEvent.event_type == "done"),
                ) == 1
    asyncio.run(run())


def test_backend_container_installs_the_reviewed_lock():
    root = Path(__file__).parents[2]
    text = (root / "backend/Dockerfile").read_text()
    assert "COPY pyproject.toml uv.lock ./" in text
    assert "uv sync --locked --no-dev --no-editable" in text
    assert "/app/.venv/bin" in text
    assert "pip install --no-cache-dir ." not in text


@pytest.mark.parametrize(("suffix", "accepted"), [
    ("", True), (" (0:01:01)", True), (" (0:61:01)", False),
])
@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_harness_accepts_only_valid_pytest_duration_suffix(tmp_path, suffix, accepted, newline):
    root = Path(__file__).parents[2]
    sys.path.insert(0, str(root / "scripts"))
    from harness.boundaries import HarnessError
    from harness.gates import validate_tests
    from release_policy import public_source_sha256

    source, evidence = tmp_path / "source", tmp_path / "evidence"
    (source / "backend/tests").mkdir(parents=True)
    evidence.mkdir()
    (source / "backend/tests/test_alpha.py").write_text("pass\n")
    (evidence / "alpha.log").write_bytes(("2 passed in 61.00s" + suffix + newline).encode("utf-8"))
    report = {
        "all_modules": True, "exitCode": 0, "sourceSha256": public_source_sha256(source),
        "modules": [{
            "module": "test_alpha.py", "log": "alpha.log", "exitCode": 0,
            "command": ["python", "-B", "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests/test_alpha.py"],
        }],
    }
    (evidence / "result.json").write_text(json.dumps(report))
    if accepted:
        assert validate_tests(source, evidence, {"allowedSkips": {}})["passedCount"] == 2
    else:
        with pytest.raises(HarnessError, match="成功汇总"):
            validate_tests(source, evidence, {"allowedSkips": {}})
