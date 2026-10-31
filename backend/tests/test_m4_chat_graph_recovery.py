"""跨进程强制退出后走生产恢复路径；协议桩不等同真实模型质量验证。"""
import asyncio
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time

import pytest


async def child(mode, calls):
    import offline_network_guard
    import b4_run_guard
    from sqlalchemy import update
    from datetime import timedelta
    from eic import main
    from eic.core import generation
    from eic.core.agent_runtime import utcnow
    from eic.core.database import SessionLocal, engine
    from eic.core.ids import initialize_allocator
    from eic.core.security import encrypt_secret
    from eic.models import (
        AIModel, AgentRunLease, Base, ChatMessage, ChatSession, GenerationLease,
        Resource, Tenant, User,
    )

    async def provider(*args, **kwargs):
        with calls.open("a") as stream:
            stream.write("call\n")
        if mode == "crash_provider":
            calls.with_suffix('.ready').write_text('74')
            time.sleep(60)
        yield "恢复成功"

    main.stream_chat_completion = provider
    if mode.startswith("crash"):
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with SessionLocal() as session:
            await initialize_allocator(session)
            session.add(Tenant(id=1, code=1, name="fixture"))
            await session.flush()
            session.add(User(id=11, tenant_id=1, username="fixture", password_hash="unused",
                             is_super_admin=True, approval_status="approved", is_active=True))
            await session.flush()
            session.add(ChatSession(id=101, tenant_id=1, owner_id=11, title="fixture"))
            session.add(AIModel(id=501, tenant_id=1, model_name="fixture",
                                model_type="chat", endpoint="http://fixture.invalid/v1",
                                secret_encrypted=encrypt_secret("fixture-only-key")))
            await session.flush()
            session.add(ChatMessage(id=301, tenant_id=1, session_id=101, role="user",
                                    content="恢复测试", request_id="fixture", status="streaming"))
            session.add(Resource(id=301, tenant_id=1, owner_id=11, resource_type="chat_messages",
                                 data={"content": "恢复测试", "memoryEnabled": False}))
            token = await generation.claim(session, await session.get(ChatMessage, 301))
            await session.commit()
        if mode == "crash_graph":
            saver = main.ExecutorCheckpointer
            original = saver.aput

            async def crash_at_boundary(self, config, checkpoint, metadata, versions):
                result = await original(self, config, checkpoint, metadata, versions)
                if checkpoint["channel_values"].get("current_node") == "check_execution":
                    calls.with_suffix('.ready').write_text('73')
                    time.sleep(60)
                return result

            saver.aput = crash_at_boundary
        await main._run_chat_producer("fixture", main.Principal(11, 1, "fixture", True), 301, token)
        raise AssertionError("进程没有在指定边界退出")
    async with SessionLocal() as session:
        await session.execute(update(GenerationLease).values(
            expires_at=utcnow() - timedelta(seconds=1),
        ))
        await session.execute(update(AgentRunLease).values(
            expires_at=utcnow() - timedelta(seconds=1),
        ))
        await session.commit()
    await generation.recover_expired()
    await asyncio.wait_for(asyncio.gather(*list(generation.PRODUCERS)), 30)
    await generation.recover_expired()
    await engine.dispose()


@pytest.mark.parametrize("crash,code", [("crash_graph", 73), ("crash_provider", 74)])
def test_real_chat_producer_survives_safe_node_crash_without_duplicate_provider(
    tmp_path, crash, code,
):
    database, calls = tmp_path / "process.db", tmp_path / "calls.txt"
    env = {**os.environ, "EIC_DATABASE_URL": "sqlite+aiosqlite:///" + database.as_posix(),
           "EIC_OUTBOX_DISPATCH_ENABLED": "false", "EIC_TASK_EXECUTION_ENABLED": "false"}
    command = [sys.executable, "-X", "utf8", "-B", str(Path(__file__).resolve()), crash, str(calls)]
    first = subprocess.Popen(command, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    deadline = time.monotonic() + 40
    while not calls.with_suffix('.ready').exists() and first.poll() is None and time.monotonic() < deadline:
        time.sleep(.05)
    reached = calls.with_suffix('.ready').exists()
    first.kill()
    stdout, stderr = first.communicate(timeout=5)
    (tmp_path / 'first.stdout').write_bytes(stdout)
    (tmp_path / 'first.stderr').write_bytes(stderr)
    assert reached, stderr.decode(errors='replace')
    assert calls.with_suffix('.ready').read_text() == str(code)
    assert first.returncode != 0
    # Windows 上强杀 SQLite writer 可能短暂保留 WAL/SHM 锁；恢复进程必须能接管。
    for _ in range(40):
        try:
            with sqlite3.connect(database) as connection:
                connection.execute('pragma busy_timeout=1000')
                connection.execute('select 1 from agent_runs limit 1').fetchone()
            break
        except sqlite3.OperationalError:
            time.sleep(.25)
    if crash == 'crash_graph':
        assert not calls.exists()
    else:
        assert calls.read_text().splitlines() == ['call']
    with sqlite3.connect(database) as connection:
        original_run = connection.execute("select run_id from agent_runs").fetchone()[0]
        assert connection.execute("select status from chat_messages where id=301").fetchone() == ("generating",)
        assert connection.execute("select count(*) from agent_graph_checkpoints").fetchone()[0] >= 3
    second = subprocess.run(command[:-2] + ["recover", str(calls)], env=env,
                            capture_output=True, timeout=45)
    (tmp_path / 'recover.stdout').write_bytes(second.stdout)
    (tmp_path / 'recover.stderr').write_bytes(second.stderr)
    assert second.returncode == 0, second.stderr.decode(errors="replace")
    assert calls.read_text().splitlines() == ["call"]
    with sqlite3.connect(database) as connection:
        assert connection.execute("select run_id from agent_runs").fetchall() == [(original_run,)]
        expected = "completed" if crash == "crash_graph" else "failed"
        assert connection.execute("select status from chat_messages where id=301").fetchone() == (expected,)
        expected_run = "completed" if crash == "crash_graph" else "manual_review"
        assert connection.execute("select status from agent_runs").fetchone() == (expected_run,)
        assert connection.execute("select count(*) from chat_events where event_type='done'").fetchone()[0] == 1
        assert connection.execute("pragma integrity_check").fetchone() == ("ok",)
        assert connection.execute("pragma foreign_key_check").fetchall() == []
        if crash == "crash_graph":
            assert connection.execute("select content from chat_messages where role='assistant'").fetchone() == ("恢复成功",)
            payloads = [json.loads(row[0]) for row in connection.execute(
                "select payload from agent_events where event_type='node_completed'",
            )]
            # 恢复重新校验准备上下文；持久共享图和外呼次数才是恢复判据。
            assert connection.execute('select count(*) from agent_graph_checkpoints').fetchone()[0] >= 4


if __name__ == "__main__":
    asyncio.run(child(sys.argv[1], Path(sys.argv[2])))
