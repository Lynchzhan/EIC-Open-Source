"""独立进程突然退出后的安全节点恢复探针，不重放模型或写工具。"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

from .boundaries import HarnessError, digest


PROGRAM = r'''
import asyncio,json,os,sys
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine,async_sessionmaker
from eic.core.agent_runtime import AgentRuntimeStore
from eic.core.ids import initialize_allocator
from eic.ai.context_manifest import prepare_context
from eic.models import Base,Tenant,AgentRun
async def main():
    engine=create_async_engine("sqlite+aiosqlite:///"+sys.argv[2])
    sessions=async_sessionmaker(engine,expire_on_commit=False)
    store=AgentRuntimeStore()
    if sys.argv[1]=="write":
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with sessions() as session:
            session.add(Tenant(id=1,code=1,name="synthetic"))
            await session.flush()
            await initialize_allocator(session)
            run=await store.create_run(session,tenant_id=1,session_id=None,request_id="crash-probe",graph_version="fixture-v1")
            await store.append_checkpoint(session,run_id=run.run_id,node_name="context",
                state={"providerStarted":False,"question":"恢复合成问题"})
            await session.commit()
        print(json.dumps({"checkpointCommitted":True}),flush=True)
        os._exit(17)
    async with sessions() as session:
        run=await session.scalar(select(AgentRun))
        decision=await store.decide_recovery(session,run_id=run.run_id,expected_graph_version="fixture-v1")
        if decision.action!="safe_replay":
            raise RuntimeError("safe recovery denied")
        messages,info=prepare_context([{"role":"system","content":"固定约束"}],[],
                                     decision.state["question"],512)
        assert messages[-1]["content"]=="恢复合成问题"
        await store.append_event(session,run_id=run.run_id,node_name="context",event_type="node_resumed",
                                 payload={"inputEstimate":info["inputEstimate"]})
        await store.finish_run(session,run_id=run.run_id,status="completed")
        await session.commit()
        print(json.dumps({"safeNodeResumed":True,"status":run.status}),flush=True)
    await engine.dispose()
asyncio.run(main())
'''


def crash_probe():
    root = Path(os.environ.get("EIC_HARNESS_CANDIDATE_SOURCE",
                               str(Path(__file__).resolve().parents[2]))).resolve()
    environment = {key: value for key, value in os.environ.items() if not key.upper().startswith("EIC_")}
    environment.update(PYTHONPATH=str(root / "backend/src"), PYTHONDONTWRITEBYTECODE="1",
                       PYTHONIOENCODING="utf-8", EIC_ENVIRONMENT="testing",
                       EIC_JWT_SECRET="synthetic-crash-probe-secret-at-least-32-characters",
                       EIC_SECRET_ENCRYPTION_KEY="synthetic-crash-probe-key-at-least-32-characters")
    with tempfile.TemporaryDirectory(prefix="eic-crash-probe-") as directory:
        db = str(Path(directory) / "probe.db")
        writer = subprocess.run([sys.executable, "-B", "-c", PROGRAM, "write", db], cwd=directory,
                                env=environment, capture_output=True, timeout=30)
        if writer.returncode != 17 or json.loads(writer.stdout) != {"checkpointCommitted": True}:
            raise HarnessError("崩溃探针未到达已提交 Checkpoint 边界")
        reader = subprocess.run([sys.executable, "-B", "-c", PROGRAM, "read", db], cwd=directory,
                                env=environment, capture_output=True, timeout=30)
        if reader.returncode != 0:
            raise HarnessError("新进程安全节点恢复失败")
        observation = json.loads(reader.stdout)
        return {"restored": observation == {"safeNodeResumed": True, "status": "completed"},
                "writerExit": writer.returncode, "readerExit": reader.returncode,
                "programSha256": digest(PROGRAM.encode()),
                "writerStdout": writer.stdout.decode().strip(), "readerStdout": reader.stdout.decode().strip()}
