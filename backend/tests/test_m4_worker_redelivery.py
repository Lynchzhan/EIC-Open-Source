"""C4 自动扫描、真实 outbox 消费与隔离进程强杀恢复。"""
import asyncio, json, os, subprocess, sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from test_m4_worker_graph_recovery import db, child_guard
from eic.core import outbox, tasks, task_graph_runtime as runtime
from eic.models import OutboxEvent, TaskRun, TaskGraphLease, ScheduledTask, User

async def setup_event(sessions):
    async with sessions() as s:
        event_id=await outbox.stage(s,'scheduled-task',300,1)
        await s.commit()
        return event_id

async def child(path, marker, event_id, mode):
    from eic import workers
    from eic.ai import shared_executor as shared
    engine=create_async_engine('sqlite+aiosqlite:///'+Path(path).as_posix())
    sessions=async_sessionmaker(engine,expire_on_commit=False)
    workers.SessionLocal=outbox.SessionLocal=sessions
    original=tasks.execute_handler
    async def handler(*args):
        with Path(marker+'.calls').open('a') as f: f.write('handler\n')
        return await original(*args)
    tasks.execute_handler=handler
    if mode in {'kill','unknown','direct'}:
        async def blocked(state):
            child_guard()
            Path(marker).write_text('persisted')
            await asyncio.Event().wait()
        shared.record_tool_result=blocked
    if mode=='unknown':
        execute=shared.execute_tool
        async def uncertain(state):
            value=await execute(state)
            value['effect_status']='unknown'
            value['active_result']['effectStatus']='unknown'
            return value
        shared.execute_tool=uncertain
    if mode=='resume':
        async def enqueue(event,tenant,**kwargs):
            await workers.consume_outbox_event({},event,tenant)
            return f'eic-outbox:{tenant}:{event}'
        outbox.queue.enqueue_outbox=enqueue
        await workers.process_due_tasks({})
    elif mode=='direct':
        await workers.run_scheduled_task({},200,1,300)
    else:
        await workers.consume_outbox_event({},int(event_id),1)
    await engine.dispose()

@pytest.mark.asyncio
@pytest.mark.parametrize('mode',['kill','unknown','direct'])
async def test_two_process_kill_and_scanner_redelivery(tmp_path,mode):
    async with db(tmp_path) as sessions:
        event_id=await setup_event(sessions) if mode!='direct' else 0
        marker=tmp_path/'marker'
        command=[sys.executable,'-X','utf8','-B',str(Path(__file__).resolve()),str(tmp_path/'c3.db'),str(marker),str(event_id),mode]
        (tmp_path/'child-command.json').write_text(json.dumps({'first':command,'second':[*command[:-1],'resume'],
            'cwd':os.getcwd(),'database':str(tmp_path/'c3.db')}),encoding='utf-8')
        env={**os.environ,'C3_CHILD_NETWORK':str(tmp_path/'first-network.json')}
        with (tmp_path/'first.stdout').open('w') as out,(tmp_path/'first.stderr').open('w') as err:
            process=subprocess.Popen(command,env=env,stdout=out,stderr=err)
            try:
                async with asyncio.timeout(30):
                    while not marker.exists():
                        assert process.poll() is None,(tmp_path/'first.stderr').read_text()
                        await asyncio.sleep(.05)
                process.kill();await asyncio.to_thread(process.wait,15)
            finally:
                if process.poll() is None: process.kill();process.wait()
        (tmp_path/'kill.json').write_text(json.dumps({'pid':process.pid,'returnCode':process.returncode,'method':'Popen.kill'}))
        if mode=='kill':
            # 实际数据库时钟到期，不编辑租约，以验证自动恢复判据。
            async with asyncio.timeout(40):
                while True:
                    async with sessions() as s:
                        timestamp=await runtime.database_now(s)
                        lease=await s.get(TaskGraphLease,300)
                        receipt=await s.get(OutboxEvent,event_id)
                        if runtime.task_schedule.utc(lease.expires_at)<=timestamp and outbox.utc(receipt.consume_until)<=timestamp:
                            break
                    await asyncio.sleep(.25)
        else:
            async with sessions() as s:
                (await s.get(TaskGraphLease,300)).expires_at=datetime.now(UTC)-timedelta(seconds=60)
                if event_id:
                    (await s.get(OutboxEvent,event_id)).consume_until=datetime.now(UTC)-timedelta(seconds=60)
                await s.commit()
        env['C3_CHILD_NETWORK']=str(tmp_path/'second-network.json')
        result=await asyncio.to_thread(subprocess.run,[*command[:-1],'resume'],env=env,capture_output=True,text=True,timeout=40)
        (tmp_path/'second.stdout').write_text(result.stdout)
        (tmp_path/'second.stderr').write_text(result.stderr)
        assert result.returncode==0,result.stderr
        async with sessions() as s:
            run=await s.get(TaskRun,300)
            assert run.status==('uncertain' if mode=='unknown' else 'success')
            events=list(await s.scalars(select(OutboxEvent)))
            assert len(events)==(2 if mode=='kill' else 1)
            if mode!='unknown':
                assert run.result['report']['readOnly'] is True
                assert (await s.get(TaskGraphLease,300)).fencing_version==2
        assert Path(str(marker)+'.calls').read_text().splitlines()==['handler']
        for name in ('first-network.json','second-network.json'):
            proof=json.loads((tmp_path/name).read_text())
            assert proof['guardLoaded'] and proof['external']=={} and proof['realHttp']==[]

@pytest.mark.asyncio
async def test_stale_consumer_cannot_commit(tmp_path,monkeypatch):
    async with db(tmp_path) as sessions:
        monkeypatch.setattr(outbox,'SessionLocal',sessions)
        event_id=await setup_event(sessions)
        async with sessions() as s:
            event=await s.get(OutboxEvent,event_id)
            event.consume_status='running';event.consume_token='old'
            event.consume_until=datetime.now(UTC)+timedelta(minutes=1)
            await s.commit()
        worker=runtime.TaskRuntime(sessions,200,1,300,consumer=(event_id,'old'))
        assert await worker.claim()=='claimed'
        async with sessions() as s:
            (await s.get(OutboxEvent,event_id)).consume_token='new'
            await s.commit()
        with pytest.raises(runtime.TaskLeaseLost): await worker.finish('success',{})
        with pytest.raises(runtime.TaskLeaseLost):
            await runtime.TaskGraphCheckpointer(worker).aput(
                runtime.TaskGraphCheckpointer(worker).config(),{'id':'stale','channel_values':{}},{},{})
        with pytest.raises(runtime.TaskLeaseLost):
            await runtime.TaskRuntime(sessions,200,1,300,consumer=(event_id,'old')).claim()
        async with sessions() as s: assert (await s.get(TaskRun,300)).status=='running'

@pytest.mark.asyncio
@pytest.mark.parametrize('change',['permission','cancel','schedule'])
async def test_recovery_rechecks_authorization(tmp_path,monkeypatch,change):
    async with db(tmp_path) as sessions:
        monkeypatch.setattr(outbox,'SessionLocal',sessions)
        event_id=await setup_event(sessions)
        worker=runtime.TaskRuntime(sessions,200,1,300)
        assert await worker.claim()=='claimed'
        async with sessions() as s:
            event=await s.get(OutboxEvent,event_id)
            event.consume_status='running';event.consume_token='old';event.consume_until=datetime.now(UTC)-timedelta(seconds=1)
            if change=='permission': (await s.get(User,11)).is_active=False
            elif change=='cancel': (await s.get(TaskRun,300)).status='cancelled'
            else: (await s.get(ScheduledTask,200)).schedule_revision=1
            await s.commit()
        assert await outbox.recover_consumers()==1
        async with sessions() as s:
            assert (await s.get(TaskRun,300)).status==('failed' if change=='permission' else 'cancelled')
            assert len(list(await s.scalars(select(OutboxEvent))))==1

@pytest.mark.asyncio
async def test_long_read_only_node_renews_and_ignores_redis_lock(tmp_path,monkeypatch):
    from eic import workers
    async with db(tmp_path) as sessions:
        monkeypatch.setattr(workers,'SessionLocal',sessions)
        monkeypatch.setattr(outbox,'SessionLocal',sessions)
        original=tasks.execute_handler
        async def slow(*args):
            await asyncio.sleep(32)
            return await original(*args)
        monkeypatch.setattr(tasks,'execute_handler',slow)
        event_id=await setup_event(sessions)
        result=await outbox.consume_event({'redis':object()},event_id,1)
        assert result['status']=='success'
        async with sessions() as s: assert (await s.get(TaskRun,300)).status=='success'

@pytest.mark.asyncio
async def test_unresponsive_read_only_node_has_finite_budget(tmp_path,monkeypatch):
    from eic import workers
    budget=workers.AgentBudget
    # 留出真实图加载/事务准入时间，必须进入handler后再验证总预算取消。
    monkeypatch.setattr(workers,'AgentBudget',lambda **kwargs: budget(**{**kwargs,'max_total_ms':5000}))
    entered=asyncio.Event()
    cancelled=asyncio.Event()
    async def stalled(*args):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
    monkeypatch.setattr(tasks,'execute_handler',stalled)
    async with db(tmp_path) as sessions:
        monkeypatch.setattr(workers,'SessionLocal',sessions)
        async with asyncio.timeout(15):
            result=await workers.run_scheduled_task({},200,1,300)
        assert entered.is_set() and cancelled.is_set()
        assert result['status'] in {'retrying','failed'}
        async with sessions() as s:
            assert (await s.get(TaskRun,300)).status!='running'
            assert (await s.get(TaskGraphLease,300)).state!='active'

@pytest.mark.asyncio
async def test_competing_scanners_stage_one_new_identity(tmp_path,monkeypatch):
    async with db(tmp_path) as sessions:
        monkeypatch.setattr(outbox,'SessionLocal',sessions)
        event_id=await setup_event(sessions)
        worker=runtime.TaskRuntime(sessions,200,1,300)
        assert await worker.claim()=='claimed'
        async with sessions() as s:
            event=await s.get(OutboxEvent,event_id)
            event.consume_status='running';event.consume_token='old'
            event.consume_until=datetime.now(UTC)-timedelta(seconds=1)
            await s.commit()
        await asyncio.gather(outbox.recover_consumers(),outbox.recover_consumers())
        async with sessions() as s:
            events=list(await s.scalars(select(OutboxEvent).order_by(OutboxEvent.id)))
            assert len(events)==2
            assert events[0].consume_status=='completed' and events[1].consume_status=='pending'
            assert events[0].id!=events[1].id

@pytest.mark.asyncio
async def test_scheduled_scanner_does_not_dispatch_other_kinds(tmp_path,monkeypatch):
    from eic import workers
    async with db(tmp_path) as sessions:
        monkeypatch.setattr(outbox,'SessionLocal',sessions)
        monkeypatch.setattr(workers,'SessionLocal',sessions)
        async with sessions() as s:
            event_id=await outbox.stage(s,'chat-compaction',999,1)
            await s.commit()
        async def forbidden(*args,**kwargs): raise AssertionError('other kind dispatched')
        monkeypatch.setattr(outbox.queue,'enqueue_outbox',forbidden)
        await workers.process_due_tasks({})
        async with sessions() as s: assert (await s.get(OutboxEvent,event_id)).status=='pending'

if __name__=='__main__':
    child_guard()
    asyncio.run(child(*sys.argv[1:]))
