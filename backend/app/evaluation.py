"""Reproducible retrieval comparisons and bounded dataset import."""
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import math
import re
import uuid
from typing import Annotated

from fastapi import HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field, StrictInt, ValidationError, model_validator
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from .evaluation_demo import demo_fixture
from .evaluation_models import EvaluationDataset, EvaluationRun

API = '/api/v1'
MAX_REQUEST = 1024 * 1024
METRIC_KEYS = ('hit_rate', 'recall', 'mrr', 'ndcg', 'no_answer_accuracy')


class Judgment(BaseModel):
    model_config = ConfigDict(extra='forbid')
    key: str = Field(pattern=r'^(technique|product):[1-9][0-9]*$', max_length=80)
    grade: Annotated[StrictInt, Field(ge=1, le=3)]


class Case(BaseModel):
    model_config = ConfigDict(extra='forbid')
    id: str = Field(min_length=1, max_length=100)
    query: str = Field(max_length=2000)
    category: str = Field(min_length=1, max_length=100)
    judgments: list[Judgment] = Field(max_length=200)

    @model_validator(mode='after')
    def unique_labels(self):
        if len({j.key for j in self.judgments}) != len(self.judgments):
            raise ValueError('Duplicate judgment key')
        if not self.id.strip() or not self.category.strip():
            raise ValueError('Case id and category cannot be blank')
        return self


class DatasetInput(BaseModel):
    # Exported Dataset JSON can be directly imported; identity is always assigned locally.
    name: str = Field(min_length=1, max_length=200)
    description: str = Field(default='', max_length=4000)
    cases: list[Case] = Field(min_length=1, max_length=200)

    @model_validator(mode='before')
    @classmethod
    def live_import_only(cls, value):
        if isinstance(value, dict) and (value.get('corpus', 'live') != 'live' or value.get('builtin', False) is not False):
            raise ValueError('Demo corpus cannot be imported into the live library; create live judgments explicitly')
        return value

    @model_validator(mode='after')
    def unique_cases(self):
        if not self.name.strip():
            raise ValueError('Name cannot be blank')
        if len({case.id for case in self.cases}) != len(self.cases):
            raise ValueError('Duplicate case id')
        return self


class RunInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    dataset_id: str = Field(min_length=1, max_length=80)
    k: Annotated[StrictInt, Field(ge=1, le=20)] = 5


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')).hexdigest()


def case_metrics(hits, judgments, k):
    grades = {j['key']:j['grade'] for j in judgments}
    if not grades:
        return {**{name:None for name in METRIC_KEYS}, 'no_answer_accuracy':float(not hits), 'positive_cases':0, 'no_answer_cases':1}
    retrieved = hits[:k]
    ranks = [i for i,h in enumerate(retrieved,1) if h['key'] in grades]
    dcg = sum((2**grades.get(h['key'],0)-1)/math.log2(i+1) for i,h in enumerate(retrieved,1))
    ideal = sum((2**grade-1)/math.log2(i+1) for i,grade in enumerate(sorted(grades.values(),reverse=True)[:k],1))
    return {'hit_rate':float(bool(ranks)), 'recall':len({h['key'] for h in retrieved}&grades.keys())/len(grades),
            'mrr':1/ranks[0] if ranks else 0.0, 'ndcg':dcg/ideal, 'no_answer_accuracy':None,
            'positive_cases':1, 'no_answer_cases':0}


def aggregate_metrics(rows):
    result = {name:sum(vals)/len(vals) if (vals := [r[name] for r in rows if r[name] is not None]) else None for name in METRIC_KEYS}
    return {**result, 'positive_cases':sum(r['positive_cases'] for r in rows), 'no_answer_cases':sum(r['no_answer_cases'] for r in rows)}


def _dataset(db, id):
    if id == 'demo-v1':
        return demo_fixture()[0]
    row = db.get(EvaluationDataset, id)
    if row is None:
        raise HTTPException(404, 'Dataset not found')
    return deepcopy(row.payload)


def _summary(dataset):
    return {k:v for k,v in dataset.items() if k != 'cases'}


async def _body(request, model):
    # Count actual streamed UTF-8 bytes before parsing (including no Content-Length).
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > MAX_REQUEST:
            raise HTTPException(413, 'Evaluation request exceeds 1 MiB')
        body.extend(chunk)
    try:
        return model.model_validate_json(bytes(body))
    except ValidationError as exc:
        raise HTTPException(422, str(exc)) from exc


def install_evaluation(app, engine):
    from .retrieval import load_documents, rank_documents, RETRIEVAL_VERSION

    @app.get(API + '/retrieval/preview')
    def preview(q: str = Query(default='', max_length=2000), k: int = Query(default=5, ge=1, le=20)):
        with Session(engine) as db:
            documents = load_documents(db)
        return {'query':q, 'k':k, 'corpus_size':len(documents), 'version':RETRIEVAL_VERSION,
                'baseline':rank_documents(documents,q,strategy='baseline',k=k), 'ranked':rank_documents(documents,q,strategy='ranked',k=k)}

    @app.get(API + '/evaluations/datasets')
    def datasets():
        with Session(engine) as db:
            return [_summary(demo_fixture()[0]), *[_summary(row.payload) for row in db.scalars(select(EvaluationDataset).order_by(EvaluationDataset.id))]]

    @app.get(API + '/evaluations/datasets/{id}')
    def get_dataset(id: str):
        with Session(engine) as db:
            return _dataset(db,id)

    def save_dataset(data, id, updating=False):
        with Session(engine) as db:
            if id == 'demo-v1':
                raise HTTPException(409, 'Built-in dataset is immutable; import a copy instead')
            if updating:
                _dataset(db,id)
            payload = {'id':id, **data.model_dump(), 'corpus':'live', 'builtin':False, 'case_count':len(data.cases)}
            if updating:
                db.get(EvaluationDataset,id).payload = payload
            else:
                db.add(EvaluationDataset(id=id,payload=payload))
            db.commit()
            return payload

    @app.post(API + '/evaluations/datasets', status_code=201)
    async def create_dataset(request: Request):
        return save_dataset(await _body(request,DatasetInput),str(uuid.uuid4()))

    @app.put(API + '/evaluations/datasets/{id}')
    async def update_dataset(id: str, request: Request):
        return save_dataset(await _body(request,DatasetInput),id,True)

    @app.delete(API + '/evaluations/datasets/{id}')
    def delete_dataset(id: str):
        if id == 'demo-v1':
            raise HTTPException(409, 'Built-in dataset is immutable')
        with Session(engine) as db:
            _dataset(db,id)
            db.execute(delete(EvaluationDataset).where(EvaluationDataset.id==id))
            db.commit()
        return {'deleted':True}

    @app.post(API + '/evaluations/runs')
    async def run_evaluation(request: Request):
        data = await _body(request,RunInput)
        with Session(engine) as db:
            dataset = _dataset(db,data.dataset_id)
            # One materialized snapshot, shared by both strategies for every case.
            documents = demo_fixture()[1] if dataset['corpus']=='demo' else load_documents(db)
            keys = {d['key'] for d in documents}
            missing = sorted({j['key'] for c in dataset['cases'] for j in c['judgments']} - keys)
            if missing:
                raise HTTPException(422, '预期资料不存在或已不可用（可能已撤回、未发布或来源不可用），请修改标注后重试：' + ', '.join(missing))
            corpus_hash = fingerprint(documents)
            cases = []
            for case in dataset['cases']:
                hits = {strategy:rank_documents(documents,case['query'],strategy=strategy,k=data.k) for strategy in ('baseline','ranked')}
                cases.append({**deepcopy(case), **hits, 'metrics':{s:case_metrics(h,case['judgments'],data.k) for s,h in hits.items()}})
            payload = {'dataset_id':dataset['id'], 'dataset_name':dataset['name'], 'created_at':datetime.now(timezone.utc).isoformat(),
                       'k':data.k, 'corpus':dataset['corpus'], 'corpus_size':len(documents), 'dataset_hash':fingerprint(dataset['cases']),
                       'corpus_hash':corpus_hash, 'versions':{'baseline':'v2-lexical','ranked':RETRIEVAL_VERSION},
                       'metrics':{s:aggregate_metrics([c['metrics'][s] for c in cases]) for s in ('baseline','ranked')}, 'cases':cases}
            row = EvaluationRun(payload=payload)
            db.add(row)
            db.flush()
            payload = {'id':row.id, **payload}
            row.payload = payload
            db.commit()
            return payload

    @app.get(API + '/evaluations/runs')
    def runs():
        with Session(engine) as db:
            return [{k:v for k,v in r.payload.items() if k in ('id','dataset_id','dataset_name','created_at','k','corpus','metrics')}
                    for r in db.scalars(select(EvaluationRun).order_by(EvaluationRun.id.desc()).limit(50))]

    @app.get(API + '/evaluations/runs/{id}')
    def get_run(id: int):
        with Session(engine) as db:
            row = db.get(EvaluationRun,id)
            if row is None:
                raise HTTPException(404, 'Evaluation run not found')
            return row.payload


def export_evaluation(db):
    return {'evaluation_datasets':[r.payload for r in db.scalars(select(EvaluationDataset).order_by(EvaluationDataset.id))],
            'evaluation_runs':[r.payload for r in db.scalars(select(EvaluationRun).order_by(EvaluationRun.id))]}


def _integer(value, minimum=0):
    if type(value) is not int or value < minimum:
        raise ValueError('Invalid evaluation integer')


def _validate_metrics(actual, expected):
    if not isinstance(actual,dict) or set(actual) != set(expected):
        raise ValueError('Invalid evaluation metrics')
    for key,value in expected.items():
        got = actual[key]
        if value is None:
            if got is not None:
                raise ValueError('Invalid metric denominator')
        elif type(got) not in (int,float) or not math.isfinite(got) or abs(got-value)>1e-12:
            raise ValueError('Inconsistent evaluation metric')


def _validate_report(report):
    required = {'id','dataset_id','dataset_name','created_at','k','corpus','corpus_size','dataset_hash','corpus_hash','versions','metrics','cases'}
    if not isinstance(report,dict) or set(report)!=required:
        raise ValueError('Invalid evaluation report fields')
    _integer(report['id'],1)
    _integer(report['k'],1)
    _integer(report['corpus_size'])
    if report['k']>20 or report['corpus'] not in ('demo','live'):
        raise ValueError('Invalid evaluation report configuration')
    for key,limit in [('dataset_id',80),('dataset_name',200)]:
        if not isinstance(report[key],str) or not 1<=len(report[key])<=limit:
            raise ValueError('Invalid evaluation identity')
    if not isinstance(report['created_at'],str):
        raise ValueError('Invalid evaluation time')
    datetime.fromisoformat(report['created_at'])
    for key in ('dataset_hash','corpus_hash'):
        if not isinstance(report[key],str) or not re.fullmatch('[0-9a-f]{64}',report[key]):
            raise ValueError('Invalid evaluation fingerprint')
    versions = report['versions']
    if not isinstance(versions,dict) or set(versions)!={'baseline','ranked'} or any(not isinstance(v,str) or not 1<=len(v)<=100 for v in versions.values()):
        raise ValueError('Invalid evaluation algorithm versions')
    cases = report['cases']
    if not isinstance(cases,list) or not 1<=len(cases)<=200:
        raise ValueError('Invalid evaluation cases')
    case_snapshot = [{k:c[k] for k in ('id','query','category','judgments')} for c in cases]
    DatasetInput.model_validate({'name':report['dataset_name'], 'cases':case_snapshot})
    if fingerprint(case_snapshot) != report['dataset_hash']:
        raise ValueError('Evaluation dataset fingerprint mismatch')
    for case in cases:
        if set(case)!={'id','query','category','judgments','baseline','ranked','metrics'} or not isinstance(case['metrics'],dict) or set(case['metrics'])!={'baseline','ranked'}:
            raise ValueError('Invalid case result')
        for strategy in ('baseline','ranked'):
            hits = case[strategy]
            if not isinstance(hits,list) or len(hits)>min(report['k'],report['corpus_size']):
                raise ValueError('Invalid evaluation hits')
            seen = set()
            for rank,hit in enumerate(hits,1):
                if not isinstance(hit,dict) or set(hit)!={'key','type','id','title','excerpt','source_ids','sources','rank','score','reasons'}:
                    raise ValueError('Invalid evaluation hit fields')
                _integer(hit['id'],1)
                _integer(hit['rank'],1)
                if hit['type'] not in ('technique','product') or hit['key']!=f"{hit['type']}:{hit['id']}" or hit['key'] in seen or hit['rank']!=rank:
                    raise ValueError('Invalid evaluation hit identity')
                seen.add(hit['key'])
                if any(not isinstance(hit[k],str) for k in ('title','excerpt')) or type(hit['score']) not in (int,float) or not math.isfinite(hit['score']):
                    raise ValueError('Invalid evaluation hit content')
                if not isinstance(hit['reasons'],list) or any(not isinstance(r,str) for r in hit['reasons']):
                    raise ValueError('Invalid evaluation reasons')
                if not isinstance(hit['sources'],list) or not isinstance(hit['source_ids'],list):
                    raise ValueError('Invalid evaluation sources')
                for source in hit['sources']:
                    if not isinstance(source,dict) or not isinstance(source.get('title'),str) or (source.get('url') is not None and not isinstance(source['url'],str)):
                        raise ValueError('Invalid evaluation source')
                    _integer(source.get('id'),1)
                if any(type(id) is not int for id in hit['source_ids']) or hit['source_ids'] != [s['id'] for s in hit['sources']]:
                    raise ValueError('Invalid evaluation source references')
            _validate_metrics(case['metrics'][strategy],case_metrics(hits,case['judgments'],report['k']))
    if not isinstance(report['metrics'],dict) or set(report['metrics'])!={'baseline','ranked'}:
        raise ValueError('Invalid report metrics')
    for strategy in ('baseline','ranked'):
        _validate_metrics(report['metrics'][strategy],aggregate_metrics([c['metrics'][strategy] for c in cases]))


def validate_evaluation_backup(data):
    if data['version']<3:
        return
    for key in ('evaluation_datasets','evaluation_runs'):
        if not isinstance(data.get(key),list):
            raise ValueError('Missing or invalid evaluation table')
    ids = set()
    for row in data['evaluation_datasets']:
        if not isinstance(row,dict) or set(row)!={'id','name','description','cases','case_count','corpus','builtin'}:
            raise ValueError('Invalid evaluation dataset fields')
        id = row['id']
        if not isinstance(id,str) or not 1<=len(id)<=80 or id=='demo-v1' or id in ids:
            raise ValueError('Invalid or duplicate evaluation dataset id')
        ids.add(id)
        parsed = DatasetInput.model_validate(row)
        if row['corpus']!='live' or row['builtin'] is not False or type(row['case_count']) is not int or row['case_count']!=len(parsed.cases):
            raise ValueError('Invalid evaluation dataset metadata')
        # Measure the minimal accepted input representation, not backup metadata or
        # pretty-print spacing. Every accepted <=1 MiB import must round-trip.
        canonical_input = json.dumps(parsed.model_dump(exclude_defaults=True), ensure_ascii=False, separators=(',', ':')).encode('utf-8')
        if len(canonical_input)>MAX_REQUEST:
            raise ValueError('Evaluation dataset exceeds 1 MiB')
    ids = set()
    for report in data['evaluation_runs']:
        _validate_report(report)
        if report['id'] in ids:
            raise ValueError('Duplicate evaluation report id')
        ids.add(report['id'])


def restore_evaluation(db,data):
    if data['version']<3:
        return  # Old backups preserve datasets and historical snapshots.
    for model in (EvaluationRun,EvaluationDataset):
        db.execute(delete(model))
    for payload in data['evaluation_datasets']:
        db.add(EvaluationDataset(id=payload['id'],payload=payload))
    for payload in data['evaluation_runs']:
        db.add(EvaluationRun(id=payload['id'],payload=payload))
