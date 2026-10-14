"""Bounded, persistent experiments. Only explicit starts upload selected snapshots.

Paid calls run sequentially in the owned job task. The SDK evaluates frozen
predictions, so its internal background tasks can never continue paid generation.
"""
import asyncio
from contextlib import suppress
from copy import deepcopy
from datetime import datetime, timezone
import json
import re
import uuid
from typing import Annotated, Literal

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, StrictInt
from sqlalchemy import delete, select
from sqlalchemy.orm import Session
from .chat import SYSTEM
from .evaluation import _dataset, case_metrics, fingerprint, Case
from .evaluation_demo import demo_fixture
from .experiment_models import ExperimentJob
from .retrieval import load_documents, rank_documents, RETRIEVAL_VERSION

ACTIVE = ('queued','running')
UNSET = object()

def now(): return datetime.now(timezone.utc).isoformat()

class ExperimentInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    dataset_id: str = Field(min_length=1,max_length=80)
    mode: Literal['retrieval','answers']
    models: list[str] = Field(default_factory=list,max_length=2)
    limit: Annotated[StrictInt,Field(ge=1,le=10)] = 3
    k: Annotated[StrictInt,Field(ge=1,le=20)] = 5
    request_id: str = Field(min_length=1,max_length=100,pattern=r'^\S+$')


def evaluator(outputs: dict, reference_outputs: dict):
    # Mechanical feedback plus original generation costs; replay duration is separate.
    scores=dict(outputs['metrics'])
    scores.update({key:outputs.get(key) for key in ('latency_ms','cost_usd')})
    scores.update(outputs.get('usage') or {})
    return {'results':[{'key':key,'score':value} for key,value in scores.items() if value is not None]}


def bounded_hits(hits):
    selected=[]; size=0
    for hit in hits:
        length=len(json.dumps(hit,ensure_ascii=False))
        if size+length<=24000:
            selected.append(hit); size+=length
    return selected


def answer_metrics(answer,hits):
    citations=re.findall(r'\[((?:technique|product):[0-9]+)\]',answer)
    valid={h['key'] for h in hits}
    return {'answer_nonempty':float(bool(answer.strip())),
            'citation_validity':sum(c in valid for c in citations)/len(citations) if citations else None,
            'uncertainty_marker':float(any(x in answer for x in ('不足','无法确认','不确定','没有资料','待确认'))) if not hits else None}


def cost(model,usage,base_url=None):
    from urllib.parse import urlsplit
    if not base_url or urlsplit(base_url).hostname!='api.openai.com': return None
    prices={'gpt-6.1-sol':(2,10),'gpt-6-luna':(.1,.5)}
    if usage is None or model not in prices: return None
    i,o=prices[model]
    return (usage['input_tokens']*i+usage['output_tokens']*o)/1_000_000


class ExperimentService:
    def __init__(self,engine,provider,lock,*,settings=None,client=UNSET,evaluate=None,jobs=None):
        from .runtime_config import read_settings
        self.engine=engine; self.provider=provider; self.lock=lock
        self.settings=read_settings() if settings is None else dict(settings)
        self.client=client; self.evaluate=evaluate; self.system_prompt=SYSTEM
        from .jobs import JobService
        self.jobs = jobs if jobs is not None else JobService(engine)

    @property
    def tasks(self):
        from .job_models import Job
        with Session(self.engine) as db:
            return {int(row.request_id): self.jobs.tasks[row.id]
                    for row in db.scalars(select(Job).where(Job.kind == 'experiment'))
                    if row.id in self.jobs.tasks}

    def history(self):
        with Session(self.engine) as db:
            return [deepcopy(r.payload) for r in db.scalars(select(ExperimentJob).order_by(ExperimentJob.id.desc()).limit(50))]

    def snapshot(self,id):
        with Session(self.engine) as db:
            row=db.get(ExperimentJob,id)
            if row is None: raise HTTPException(404,'实验不存在。')
            return deepcopy(row.payload)

    def update(self,id,**fields):
        with Session(self.engine) as db:
            row=db.get(ExperimentJob,id)
            row.payload={**row.payload,**fields,'updated_at':now()}
            db.commit()
        if 'progress' in fields:
            from .job_models import Job
            with Session(self.engine) as db:
                job_id = db.scalar(select(Job.id).where(Job.kind == 'experiment', Job.request_id == str(id)))
            if job_id is not None:
                self.jobs.update(job_id, progress=fields['progress'], total=self.snapshot(id)['total'])

    def has_active(self):
        return any(not task.done() for task in self.jobs.tasks.values()) or any(p['status'] in ACTIVE for p in self.history())

    def recover(self):
        with Session(self.engine) as db:
            for row in db.scalars(select(ExperimentJob)):
                if row.payload['status'] in ACTIVE:
                    row.payload={**row.payload,'status':'stopped','error':'服务重启，实验已中断；云端可能保留部分记录。','updated_at':now()}
            db.commit()

    async def start(self,data):
        async with self.lock:
            with Session(self.engine) as db:
                req=data.model_dump(); digest=fingerprint(req)
                existing=db.scalar(select(ExperimentJob).where(ExperimentJob.request_id==data.request_id))
                if existing:
                    if existing.request_hash!=digest: raise HTTPException(409,'request_id 已用于不同参数。')
                    return deepcopy(existing.payload)
                if self.has_active(): raise HTTPException(409,'已有实验正在运行。')
                from .runtime_config import langsmith_config, normalize_model
                if not langsmith_config(self.settings)['configured']: raise HTTPException(503,'请先配置有效的 LangSmith API key、端点和工作区。')
                allowed=list(dict.fromkeys(normalize_model(x) for x in (self.settings.get('AI_MODEL'),self.settings.get('AI_COMPARE_MODEL')) if x))
                models=(data.models or allowed[:1]) if data.mode=='answers' else []
                if data.mode=='answers':
                    if not getattr(self.provider,'configured',False): raise HTTPException(503,'请先配置模型。')
                    if not models or len(set(models))!=len(models) or any(m not in allowed for m in models): raise HTTPException(422,'请选择已配置的模型。')
                dataset=_dataset(db,data.dataset_id)
                documents=demo_fixture()[1] if dataset['corpus']=='demo' else load_documents(db)
                cases=deepcopy(dataset['cases'][:data.limit]); keys={d['key'] for d in documents}
                for case in cases:
                    Case.model_validate(case)
                    if any(j['key'] not in keys for j in case['judgments']): raise HTTPException(422,'标注资料已不可用，请修改标注。')
                frozen=[{**c,'evidence':{s:bounded_hits(rank_documents(documents,c['query'],strategy=s,k=data.k)) for s in ('baseline','ranked')}} for c in cases]
                snapshot={'request':req,'cases':frozen,'retrieval_version':RETRIEVAL_VERSION,'corpus_hash':fingerprint(documents),'system_prompt':self.system_prompt}
                stamp=now()
                payload={'dataset_id':dataset['id'],'dataset_name':dataset['name'],'corpus':dataset['corpus'],'mode':data.mode,'models':models,'status':'queued','progress':0,'total':len(cases)*(len(models) if models else 2),'created_at':stamp,'updated_at':stamp,'error':None,'links':[],'results':[],'snapshot_hash':fingerprint(snapshot)}
                row=ExperimentJob(request_id=data.request_id,request_hash=digest,payload=payload,snapshot=snapshot)
                db.add(row); db.flush(); row.payload={**payload,'id':row.id}; db.commit()
                output=deepcopy(row.payload)
            async def execute(context):
                await self.run(output['id'])
                return self.snapshot(output['id'])
            def cancelled():
                if self.snapshot(output['id'])['status'] in ACTIVE:
                    self.update(output['id'], status='stopped', error='实验已停止；云端可能保留部分记录。')
            await self.jobs.submit('experiment', str(output['id']), {'snapshot_hash': output['snapshot_hash']}, execute, on_cancel=cancelled)
            return output

    async def stop(self,id):
        from .job_models import Job
        with Session(self.engine) as db:
            job_id = db.scalar(select(Job.id).where(Job.kind == 'experiment', Job.request_id == str(id)))
        if job_id is not None:
            await self.jobs.cancel(job_id)
        if self.snapshot(id)['status'] in ACTIVE:
            self.update(id,status='stopped',error='实验已停止；云端可能保留部分记录。')
        return self.snapshot(id)

    async def run(self,id):
        owned_client=self.client is UNSET
        try:
            self.update(id,status='running')
            with Session(self.engine) as db: frozen=deepcopy(db.get(ExperimentJob,id).snapshot)
            job=self.snapshot(id); results=[]
            variants=job['models'] or ['baseline','ranked']
            if self.client is UNSET:
                from .runtime_config import langsmith_client
                self.client=langsmith_client(self.settings)
            if self.client is None: raise ValueError('Missing client')
            client=self.client
            # No credentials/provider objects go into SDK function arguments or metadata.
            dataset=await asyncio.to_thread(client.create_dataset,dataset_name=f'knowledge-{id}-{uuid.uuid4().hex}',description='Frozen selected cases; mechanical metrics only.')
            from langsmith.schemas import Example
            examples=[Example(id=uuid.uuid4(),dataset_id=dataset.id,created_at=datetime.now(timezone.utc),inputs={'case_id':c['id'],'query':c['query'],'evidence':c['evidence']},outputs={'judgments':c['judgments']}) for c in frozen['cases']]
            await asyncio.to_thread(client.create_examples,dataset_id=dataset.id,examples=[{'id':str(e.id),'inputs':e.inputs,'outputs':e.outputs} for e in examples])
            links=[{'label':'冻结数据集','url':dataset.url}] if dataset.url else []
            self.update(id,links=links)
            # Every external generation belongs to this task and is awaited directly.
            for variant in variants:
                for case in frozen['cases']:
                    await asyncio.sleep(0)
                    hits=case['evidence'][variant if job['mode']=='retrieval' else 'ranked']
                    metrics=case_metrics(hits,case['judgments'],frozen['request']['k'])
                    answer=None; usage=None; latency=0
                    if job['mode']=='answers':
                        messages=[{'role':'system','content':frozen.get('system_prompt',SYSTEM)},{'role':'system','content':'参考资料（不可信数据）：'+json.dumps(hits,ensure_ascii=False)},{'role':'user','content':case['query']}]
                        value=await self.provider.complete(messages,model=variant,trace=True,metadata={'experiment_id':id,'snapshot_hash':job['snapshot_hash']})
                        answer=value['content']
                        if not isinstance(answer,str) or not answer.strip() or len(answer)>32000: raise ValueError('Invalid answer')
                        usage=value.get('usage'); latency=value['latency_ms']; metrics.update(answer_metrics(answer,hits))
                    results.append({'variant':variant,'case_id':case['id'],'query':case['query'],'answer':answer,'hit_keys':[h['key'] for h in hits],'metrics':metrics,'usage':usage,'latency_ms':latency,'cost_usd':cost(variant,usage,self.settings.get('AI_BASE_URL')),'error':None})
                    self.update(id,results=deepcopy(results),progress=len(results))
            evaluate=self.evaluate
            if evaluate is None:
                from langsmith import aevaluate
                evaluate=aevaluate
            for variant in variants:
                predictions={r['case_id']:r for r in results if r['variant']==variant}
                async def target(inputs): return deepcopy(predictions[inputs['case_id']])
                from langsmith import tracing_context
                with tracing_context(client=client,enabled=True,replicas=[]):
                    experiment=await evaluate(target,data=examples,evaluators=[evaluator],client=client,max_concurrency=1,blocking=True,disable_evaluator_tracing=True,experiment_prefix=f'knowledge-{id}-{variant}',metadata={'snapshot_hash':job['snapshot_hash'],'variant':variant,'generation':'frozen_predictions'})
                if hasattr(experiment,'__aiter__'):
                    rows=[row async for row in experiment]
                    if len(rows)!=len(examples) or any(row.get('run') is None or row['run'].error or not row['evaluation_results']['results'] or any((getattr(feedback,'extra',None) or {}).get('error') for feedback in row['evaluation_results']['results']) for row in rows):
                        raise ValueError('SDK evaluation failed')
                # SDK project objects provide canonical project URLs; never invent one.
                if hasattr(client,'read_project'):
                    project=await asyncio.to_thread(client.read_project,project_name=experiment.experiment_name)
                    if getattr(project,'url',None): links.append({'label':variant,'url':project.url})
                self.update(id,links=deepcopy(links))
            if hasattr(client,'flush'): await asyncio.to_thread(client.flush)
            self.update(id,status='completed')
        except asyncio.CancelledError:
            self.update(id,status='stopped',error='实验已停止；云端可能保留部分记录。')
            raise
        except Exception:
            # Do not capture exception repr/body in logs or cloud traces.
            self.update(id,status='failed',error='实验失败，请检查模型或 LangSmith 配置；已保留本地结果，云端可能有部分记录。')
        finally:
            if owned_client and self.client is not UNSET and self.client is not None:
                client=self.client
                self.client=UNSET
                with suppress(Exception):
                    await asyncio.to_thread(client.close,timeout=10)


def install_experiments(app,service):
    app.state.experiments=service
    @app.get('/api/v1/integrations/status')
    def status():
        from .runtime_config import integration_status
        return integration_status(provider=service.provider,settings=service.settings)
    @app.get('/api/v1/experiments')
    def history(): return service.history()
    @app.get('/api/v1/experiments/{id}')
    def detail(id:int): return service.snapshot(id)
    @app.post('/api/v1/experiments',status_code=202)
    async def start(data:ExperimentInput): return await service.start(data)
    @app.post('/api/v1/experiments/{id}/stop')
    async def stop(id:int):
        async with service.lock: return await service.stop(id)


def export_experiments(db):
    return {'experiments':[{'request_id':r.request_id,'request_hash':r.request_hash,'payload':r.payload,'snapshot':r.snapshot} for r in db.scalars(select(ExperimentJob).order_by(ExperimentJob.id))]}


def validate_experiment_backup(data):
    if data['version']<4: return
    if not isinstance(data.get('experiments'),list): raise ValueError('Missing experiments')
    ids=set(); requests=set()
    for record in data['experiments']:
        if not isinstance(record,dict) or set(record)!={'request_id','request_hash','payload','snapshot'}: raise ValueError('Invalid experiment record')
        p=record['payload']; s=record['snapshot']
        BackupPayload.model_validate(p)
        if not isinstance(s,dict): raise ValueError('Invalid snapshot')
        if set(s) not in ({'request','cases','retrieval_version','corpus_hash'}, {'request','cases','retrieval_version','corpus_hash','system_prompt'}): raise ValueError('Invalid snapshot fields')
        if 'system_prompt' in s and (not isinstance(s['system_prompt'],str) or len(s['system_prompt'])>16000): raise ValueError('Invalid system prompt')
        if not re.fullmatch('[0-9a-f]{64}',s['corpus_hash']) or not isinstance(s['retrieval_version'],str): raise ValueError('Invalid snapshot metadata')
        req=ExperimentInput.model_validate(s['request'])
        if p['dataset_id']!=req.dataset_id: raise ValueError('Invalid dataset identity')
        if req.request_id!=record['request_id'] or fingerprint(req.model_dump())!=record['request_hash']: raise ValueError('Invalid experiment request')
        if fingerprint(s)!=p['snapshot_hash']: raise ValueError('Invalid experiment snapshot hash')
        if type(p['id']) is not int or p['id']<1 or p['id'] in ids or req.request_id in requests: raise ValueError('Duplicate experiment identity')
        ids.add(p['id']); requests.add(req.request_id)
        if p['status'] not in (*ACTIVE,'completed','failed','stopped') or p['mode']!=req.mode or p['corpus'] not in ('live','demo'): raise ValueError('Invalid experiment status')
        if not isinstance(s['cases'],list) or not 1<=len(s['cases'])<=req.limit: raise ValueError('Invalid experiment cases')
        case_ids=set()
        for case in s['cases']:
            if not isinstance(case,dict) or not isinstance(case.get('evidence'),dict): raise ValueError('Invalid case snapshot')
            Case.model_validate({k:v for k,v in case.items() if k!='evidence'})
            if case['id'] in case_ids: raise ValueError('Duplicate case')
            case_ids.add(case['id'])
            if set(case['evidence'])!={'baseline','ranked'}: raise ValueError('Invalid evidence')
            for hits in case['evidence'].values():
                if not isinstance(hits,list) or len(hits)>req.k or len(json.dumps(hits,ensure_ascii=False))>25000: raise ValueError('Invalid evidence budget')
                if any(not isinstance(h,dict) or not re.fullmatch(r'(technique|product):[1-9][0-9]*',h.get('key','')) or not isinstance(h.get('excerpt'),str) for h in hits): raise ValueError('Invalid evidence hit')
        variants=p['models'] if p['mode']=='answers' else ['baseline','ranked']
        if not 1<=len(variants)<=2 or len(set(variants))!=len(variants): raise ValueError('Invalid variants')
        if p['total']!=len(s['cases'])*len(variants) or type(p['progress']) is not int or p['progress']!=len(p['results']) or not 0<=p['progress']<=p['total']: raise ValueError('Invalid experiment progress')
        if p['status']=='completed' and p['progress']!=p['total']: raise ValueError('Incomplete experiment')
        seen=set()
        for result in p['results']:
            pair=(result['variant'],result['case_id'])
            if pair in seen or pair[0] not in variants or pair[1] not in case_ids: raise ValueError('Invalid experiment result')
            seen.add(pair)
            case=next(c for c in s['cases'] if c['id']==pair[1])
            hits=case['evidence'][pair[0] if p['mode']=='retrieval' else 'ranked']
            if result['query']!=case['query'] or result['hit_keys']!=[h['key'] for h in hits]: raise ValueError('Invalid result snapshot')
            expected=case_metrics(hits,case['judgments'],req.k)
            if p['mode']=='answers':
                if not isinstance(result['answer'],str) or not 1<=len(result['answer'])<=32000: raise ValueError('Invalid answer')
                expected.update(answer_metrics(result['answer'],hits))
            if result['metrics']!=expected: raise ValueError('Invalid mechanical metrics')
        for key in ('created_at','updated_at'): datetime.fromisoformat(p[key])
        for link in p['links']:
            from urllib.parse import urlsplit
            url=urlsplit(link['url'])
            if url.scheme!='https' or not url.hostname or url.username or url.password: raise ValueError('Invalid experiment link')


def restore_experiments(db,data):
    if data['version']<4: return
    db.execute(delete(ExperimentJob))
    for r in data['experiments']:
        p=deepcopy(r['payload'])
        if p['status'] in ACTIVE: p.update(status='stopped',error='备份中的实验已中断。',updated_at=now())
        db.add(ExperimentJob(id=p['id'],request_id=r['request_id'],request_hash=r['request_hash'],payload=p,snapshot=deepcopy(r['snapshot'])))


class BackupShape(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True,allow_inf_nan=False)

class BackupUsage(BackupShape):
    input_tokens: int=Field(ge=0)
    output_tokens: int=Field(ge=0)
    total_tokens: int=Field(ge=0)

class BackupResult(BackupShape):
    variant: str=Field(min_length=1,max_length=200)
    case_id: str=Field(min_length=1,max_length=100)
    query: str=Field(max_length=2000)
    answer: str | None=Field(max_length=32000)
    hit_keys: list[str]=Field(max_length=20)
    metrics: dict[str,float | None]
    usage: BackupUsage | None
    latency_ms: float=Field(ge=0)
    cost_usd: float | None=Field(ge=0)
    error: str | None=Field(max_length=2000)

class BackupLink(BackupShape):
    label: str=Field(min_length=1,max_length=200)
    url: str=Field(min_length=1,max_length=4000)

class BackupPayload(BackupShape):
    id: int=Field(ge=1)
    dataset_id: str=Field(min_length=1,max_length=80)
    dataset_name: str=Field(min_length=1,max_length=200)
    corpus: Literal['demo','live']
    mode: Literal['retrieval','answers']
    models: list[str]=Field(max_length=2)
    status: Literal['queued','running','completed','failed','stopped']
    progress: int=Field(ge=0,le=20)
    total: int=Field(ge=1,le=20)
    created_at: str
    updated_at: str
    error: str | None=Field(max_length=2000)
    links: list[BackupLink]=Field(max_length=3)
    results: list[BackupResult]=Field(max_length=20)
    snapshot_hash: str=Field(pattern=r'^[0-9a-f]{64}$')
