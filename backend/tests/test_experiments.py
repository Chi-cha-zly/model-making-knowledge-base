from pg_test_support import test_database_url
import asyncio
from types import SimpleNamespace
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from app.models import Base


def test_experiment_service_exists():
    from app import experiments
    assert hasattr(experiments, 'ExperimentService')


class FakeClient:
    def __init__(self): self.datasets = []; self.examples = []
    def create_dataset(self, **kw):
        self.datasets.append(kw); return SimpleNamespace(id='00000000-0000-0000-0000-000000000001',url='https://smith.langchain.com/datasets/test')
    def create_examples(self, **kw): self.examples.append(kw)
    def flush(self): pass


async def fake_evaluate(target, **kw):
    assert kw['max_concurrency'] == 1 and kw['evaluators']
    for example in kw['data']:
        output = await target(example.inputs)
        result = kw['evaluators'][0](outputs=output, reference_outputs=example.outputs)
        assert result['results']
    return SimpleNamespace(experiment_name='test')


class Provider:
    configured = True
    model = 'fake-model'
    async def complete(self, messages, **kw):
        return {'content':'资料不足 [technique:1]', 'usage':None, 'latency_ms':1,'model':kw['model']}


def service(tmp_path, provider=None, configured=True):
    from app.experiments import ExperimentService
    engine = create_engine(test_database_url('' + str(tmp_path/'test.db')))
    return ExperimentService(engine,provider or Provider(),asyncio.Lock(),settings={'AI_MODEL':'fake-model','LANGSMITH_API_KEY':'test-only' if configured else ''},client=FakeClient() if configured else None,evaluate=fake_evaluate)


@pytest.mark.anyio
async def test_complete_and_idempotency(tmp_path):
    from app.experiments import ExperimentInput
    from fastapi import HTTPException
    s=service(tmp_path)
    data=ExperimentInput(dataset_id='demo-v1',mode='retrieval',request_id='a')
    first=await s.start(data)
    assert first['total']==6
    assert (await s.start(data))['id']==first['id']
    with pytest.raises(HTTPException) as e: await s.start(data.model_copy(update={'k':3}))
    assert e.value.status_code==409
    await s.tasks[first['id']]
    done=s.snapshot(first['id'])
    assert done['status']=='completed' and len(done['results'])==6
    assert len(s.client.datasets)==1
    assert len(s.client.examples[0]['examples'])==3


@pytest.mark.anyio
async def test_missing_configuration_and_bounds(tmp_path):
    from app.experiments import ExperimentInput
    from fastapi import HTTPException
    from pydantic import ValidationError
    s=service(tmp_path,configured=False)
    with pytest.raises(HTTPException) as e: await s.start(ExperimentInput(dataset_id='demo-v1',mode='retrieval',request_id='a'))
    assert e.value.status_code==503
    assert s.history()==[]
    with pytest.raises(ValidationError): ExperimentInput(dataset_id='demo-v1',mode='retrieval',request_id='a',limit=11)


@pytest.mark.anyio
async def test_stop_cancels_upstream_before_return(tmp_path):
    from app.experiments import ExperimentInput
    class Block(Provider):
        def __init__(self): self.started=asyncio.Event(); self.closed=False; self.calls=0
        async def complete(self,*args,**kw):
            self.calls+=1; self.started.set()
            try: await asyncio.Event().wait()
            finally: self.closed=True
    p=Block(); s=service(tmp_path,p)
    row=await s.start(ExperimentInput(dataset_id='demo-v1',mode='answers',request_id='a'))
    await p.started.wait()
    stopped=await s.stop(row['id'])
    assert stopped['status']=='stopped' and p.closed and p.calls==1
    await asyncio.sleep(0)
    assert p.calls==1 and not s.has_active()

@pytest.fixture
def anyio_backend(): return 'asyncio'

@pytest.mark.anyio
async def test_backup_validation_recovery_and_legacy_retention(tmp_path):
    from app.experiments import ExperimentInput,export_experiments,validate_experiment_backup,restore_experiments
    from app.experiment_models import ExperimentJob
    from copy import deepcopy
    s=service(tmp_path)
    row=await s.start(ExperimentInput(dataset_id='demo-v1',mode='retrieval',request_id='a'))
    await s.tasks[row['id']]
    with Session(s.engine) as db:
        backup={'version':4,**export_experiments(db)}
        validate_experiment_backup(backup)
        bad=deepcopy(backup); bad['experiments'][0]['snapshot']['cases'][0]['query']='tampered'
        with pytest.raises(ValueError): validate_experiment_backup(bad)
        restore_experiments(db,{'version':3}); db.commit()
        assert db.get(ExperimentJob,row['id']) is not None
    s.update(row['id'],status='running'); s.recover()
    assert s.snapshot(row['id'])['status']=='stopped'


@pytest.mark.anyio
async def test_cloud_failure_precedes_paid_calls_and_error_is_safe(tmp_path):
    from app.experiments import ExperimentInput
    class P(Provider):
        calls=0
        async def complete(self,*a,**kw): self.calls+=1; return await super().complete(*a,**kw)
    class Broken(FakeClient):
        def create_dataset(self,**kw): raise RuntimeError('secret-key body')
    p=P(); s=service(tmp_path,p); s.client=Broken()
    row=await s.start(ExperimentInput(dataset_id='demo-v1',mode='answers',request_id='a'))
    await s.tasks[row['id']]
    assert p.calls==0
    assert s.snapshot(row['id'])['status']=='failed'
    assert 'secret' not in str(s.snapshot(row['id']))


def test_app_backup_v4_and_restore_lock(tmp_path,monkeypatch):
    from app import main
    from fastapi.testclient import TestClient
    from zipfile import ZipFile
    from io import BytesIO
    import json
    from app import runtime_config
    monkeypatch.setattr(runtime_config,'read_settings',lambda:{})
    app=main.create_app(test_database_url(str(tmp_path/'app.db')), tmp_path/'store', provider=Provider(), runtime_settings={})
    with TestClient(app) as client:
        assert hasattr(app.state,'experiments')
        backup=client.get('/api/v1/backup')
        with ZipFile(BytesIO(backup.content)) as z: data=json.loads(z.read('manifest.json'))
        assert data['version']==6 and data['experiments']==[]
        assert client.post('/api/v1/backup',files={'file':('backup.zip',backup.content)}).status_code==200

@pytest.mark.anyio
async def test_actual_sdk_evaluator_offline(monkeypatch):
    from langsmith import Client, aevaluate, tracing_context
    from langsmith.schemas import Example
    from app.experiments import evaluator
    from datetime import datetime, timezone
    import requests, uuid
    def no_network(*a,**kw): raise AssertionError('offline SDK test attempted network')
    monkeypatch.setattr(requests.Session,'request',no_network)
    client=Client(api_url='https://api.smith.langchain.com',api_key='fake-only',auto_batch_tracing=False)
    monkeypatch.setattr(Client,'create_run',lambda *a,**kw:None)
    monkeypatch.setattr(Client,'update_run',lambda *a,**kw:None)
    example=Example(id=uuid.uuid4(),dataset_id=uuid.uuid4(),created_at=datetime.now(timezone.utc),inputs={'query':'demo'},outputs={'judgments':[]})
    async def target(inputs): return {'metrics':{'answer_nonempty':1.,'citation_validity':None}}
    with tracing_context(enabled='local',replicas=[]):
        result=await aevaluate(target,data=[example],evaluators=[evaluator],client=client,upload_results=False,max_concurrency=1,disable_evaluator_tracing=True)
    rows=[row async for row in result]
    assert len(rows)==1
    assert rows[0]['evaluation_results']['results'][0].key=='answer_nonempty', rows
    assert rows[0]['evaluation_results']['results'][0].score==1.
    client.close()

@pytest.mark.anyio
async def test_invalid_workspace_rejected_before_persistence(tmp_path):
    from app.experiments import ExperimentInput
    from fastapi import HTTPException
    s=service(tmp_path);s.settings['LANGSMITH_WORKSPACE_ID']='not-uuid'
    with pytest.raises(HTTPException) as e: await s.start(ExperimentInput(dataset_id='demo-v1',mode='retrieval',request_id='a'))
    assert e.value.status_code==503 and not s.history()

@pytest.mark.anyio
async def test_real_sdk_job_results_checked(tmp_path,monkeypatch):
    from app.experiments import ExperimentInput
    from langsmith import Client,aevaluate
    import requests
    monkeypatch.setattr(requests.Session,'request',lambda *a,**kw: (_ for _ in ()).throw(AssertionError('network forbidden')))
    monkeypatch.setattr(Client,'create_run',lambda *a,**kw:None)
    monkeypatch.setattr(Client,'update_run',lambda *a,**kw:None)
    sdk_client=Client(api_url='https://api.smith.langchain.com',api_key='fake-only',auto_batch_tracing=False)
    async def offline(target,**kw):
        kw.update(client=sdk_client,upload_results=False)
        return await aevaluate(target,**kw)
    s=service(tmp_path);s.evaluate=offline
    row=await s.start(ExperimentInput(dataset_id='demo-v1',mode='retrieval',request_id='a',limit=1))
    await s.tasks[row['id']]
    assert s.snapshot(row['id'])['status']=='completed'
    sdk_client.close()

@pytest.mark.anyio
@pytest.mark.parametrize('field,value',[('cost_usd','bad'),('latency_ms','bad'),('usage',{}),('cost_usd',float('inf'))])
async def test_malformed_result_backup_rejected(tmp_path,field,value):
    from app.experiments import ExperimentInput,export_experiments,validate_experiment_backup
    s=service(tmp_path)
    r=await s.start(ExperimentInput(dataset_id='demo-v1',mode='retrieval',request_id='a',limit=1))
    await s.tasks[r['id']]
    with Session(s.engine) as db: data={'version':4,**export_experiments(db)}
    data['experiments'][0]['payload']['results'][0][field]=value
    with pytest.raises(ValueError): validate_experiment_backup(data)

@pytest.mark.anyio
async def test_terminal_tasks_removed(tmp_path):
    from app.experiments import ExperimentInput
    s=service(tmp_path);r=await s.start(ExperimentInput(dataset_id='demo-v1',mode='retrieval',request_id='a',limit=1))
    await s.tasks[r['id']]; await asyncio.sleep(0)
    assert s.tasks=={}

@pytest.mark.anyio
async def test_owned_task_cleanup_blocks_restore(tmp_path):
    s=service(tmp_path)
    task=asyncio.create_task(asyncio.Event().wait())
    s.jobs.tasks[123]=task
    try: assert s.has_active()
    finally:
        task.cancel()
        from contextlib import suppress
        with suppress(asyncio.CancelledError): await task

@pytest.mark.anyio
@pytest.mark.parametrize('bad',['case','evidence'])
async def test_malformed_snapshot_rejected_as_value_error(tmp_path,bad):
    from app.experiments import ExperimentInput,export_experiments,validate_experiment_backup
    from app.evaluation import fingerprint
    s=service(tmp_path);r=await s.start(ExperimentInput(dataset_id='demo-v1',mode='retrieval',request_id='a',limit=1))
    await s.tasks[r['id']]
    with Session(s.engine) as db: data={'version':4,**export_experiments(db)}
    record=data['experiments'][0]
    if bad=='case': record['snapshot']['cases'][0]='bad'
    else: record['snapshot']['cases'][0]['evidence']=['baseline','ranked']
    record['payload']['snapshot_hash']=fingerprint(record['snapshot'])
    with pytest.raises(ValueError): validate_experiment_backup(data)


def test_uncertainty_marker_and_custom_endpoint_cost():
    from app.experiments import answer_metrics,cost
    assert answer_metrics('待确认',[])['uncertainty_marker']==1
    usage={'input_tokens':100,'output_tokens':10,'total_tokens':110}
    assert cost('gpt-6-luna',usage,'https://api.openai.com/v1')==0.000015
    assert cost('gpt-6-luna',usage,'https://proxy.example/v1') is None


@pytest.mark.anyio
@pytest.mark.parametrize('through_shared_jobs', [False, True])
async def test_queued_cancel_synchronizes_experiment_and_job(tmp_path, through_shared_jobs):
    from app.experiments import ExperimentInput
    s = service(tmp_path)
    row = await s.start(ExperimentInput(dataset_id='demo-v1', mode='retrieval', request_id='queued-stop'))
    job = s.jobs.history('experiment')[0]
    if through_shared_jobs:
        await s.jobs.cancel(job['id'])
    else:
        await s.stop(row['id'])
    assert s.snapshot(row['id'])['status'] == 'stopped'
    assert s.jobs.snapshot(job['id'])['status'] == 'stopped'
    assert not s.jobs.tasks
    assert s.client.datasets == []
