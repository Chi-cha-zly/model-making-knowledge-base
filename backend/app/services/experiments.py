"""Explicit frozen experiments, model generation and shared job coordination."""

import asyncio
from contextlib import suppress
from copy import deepcopy
from datetime import datetime, timezone
import json
import uuid
from ..prompts import SYSTEM
from ..evaluation_metrics import case_metrics, fingerprint
from ..evaluation_schemas import Case
from ..retrieval import rank_documents, RETRIEVAL_VERSION
from ..experiment_metrics import now, evaluator, bounded_hits, answer_metrics, cost
from ..repositories.experiments import ExperimentRepository
from .errors import ServiceError

ACTIVE = ('queued', 'running')
UNSET = object()


class ExperimentService:
    def __init__(
        self, engine, provider, lock, *, settings=None, client=UNSET, evaluate=None, jobs=None
    ):
        from ..runtime_config import read_settings

        self.engine = engine
        self.repository = ExperimentRepository(engine)
        self.provider = provider
        self.lock = lock
        self.settings = read_settings() if settings is None else dict(settings)
        self.client = client
        self.evaluate = evaluate
        self.system_prompt = SYSTEM
        from ..jobs import JobService

        self.jobs = jobs if jobs is not None else JobService(engine)

    @property
    def tasks(self):
        return {
            id: self.jobs.tasks[job_id]
            for id, job_id in self.repository.job_links().items()
            if job_id in self.jobs.tasks
        }

    def history(self):
        return self.repository.history()

    def snapshot(self, id):
        return self.repository.snapshot(id)

    def update(self, id, **fields):
        self.repository.update(id, fields, now())
        if 'progress' in fields:
            job_id = self.repository.job_id(id)
            if job_id is not None:
                self.jobs.update(
                    job_id, progress=fields['progress'], total=self.snapshot(id)['total']
                )

    def has_active(self):
        return any(not task.done() for task in self.jobs.tasks.values()) or any(
            p['status'] in ACTIVE for p in self.history()
        )

    def recover(self):
        self.repository.recover(now())

    async def start(self, data):
        async with self.lock:
            req = data.model_dump()
            digest = fingerprint(req)
            existing = self.repository.existing(data.request_id)
            if existing:
                if existing[0] != digest:
                    raise ServiceError(409, 'request_id 已用于不同参数。')
                return existing[1]
            if self.has_active():
                raise ServiceError(409, '已有实验正在运行。')
            from ..runtime_config import langsmith_config, normalize_model

            if not langsmith_config(self.settings)['configured']:
                raise ServiceError(503, '请先配置有效的 LangSmith API key、端点和工作区。')
            allowed = list(
                dict.fromkeys(
                    normalize_model(x)
                    for x in (
                        self.settings.get('AI_MODEL'),
                        self.settings.get('AI_COMPARE_MODEL'),
                    )
                    if x
                )
            )
            models = (data.models or allowed[:1]) if data.mode == 'answers' else []
            if data.mode == 'answers':
                if not getattr(self.provider, 'configured', False):
                    raise ServiceError(503, '请先配置模型。')
                if (
                    not models
                    or len(set(models)) != len(models)
                    or any(m not in allowed for m in models)
                ):
                    raise ServiceError(422, '请选择已配置的模型。')
            dataset, documents = self.repository.dataset_documents(data.dataset_id)
            cases = deepcopy(dataset['cases'][: data.limit])
            keys = {d['key'] for d in documents}
            for case in cases:
                Case.model_validate(case)
                if any(j['key'] not in keys for j in case['judgments']):
                    raise ServiceError(422, '标注资料已不可用，请修改标注。')
            frozen = [
                {
                    **c,
                    'evidence': {
                        s: bounded_hits(rank_documents(documents, c['query'], strategy=s, k=data.k))
                        for s in ('baseline', 'ranked')
                    },
                }
                for c in cases
            ]
            snapshot = {
                'request': req,
                'cases': frozen,
                'retrieval_version': RETRIEVAL_VERSION,
                'corpus_hash': fingerprint(documents),
                'system_prompt': self.system_prompt,
            }
            stamp = now()
            payload = {
                'dataset_id': dataset['id'],
                'dataset_name': dataset['name'],
                'corpus': dataset['corpus'],
                'mode': data.mode,
                'models': models,
                'status': 'queued',
                'progress': 0,
                'total': len(cases) * (len(models) if models else 2),
                'created_at': stamp,
                'updated_at': stamp,
                'error': None,
                'links': [],
                'results': [],
                'snapshot_hash': fingerprint(snapshot),
            }
            output = self.repository.create(data.request_id, digest, payload, snapshot)

            async def execute(context):
                await self.run(output['id'])
                return self.snapshot(output['id'])

            def cancelled():
                if self.snapshot(output['id'])['status'] in ACTIVE:
                    self.update(
                        output['id'], status='stopped', error='实验已停止；云端可能保留部分记录。'
                    )

            await self.jobs.submit(
                'experiment',
                str(output['id']),
                {'snapshot_hash': output['snapshot_hash']},
                execute,
                on_cancel=cancelled,
            )
            return output

    async def stop(self, id):
        job_id = self.repository.job_id(id)
        if job_id is not None:
            await self.jobs.cancel(job_id)
        if self.snapshot(id)['status'] in ACTIVE:
            self.update(id, status='stopped', error='实验已停止；云端可能保留部分记录。')
        return self.snapshot(id)

    async def run(self, id):
        owned_client = self.client is UNSET
        try:
            self.update(id, status='running')
            frozen = self.repository.frozen(id)
            job = self.snapshot(id)
            results = []
            variants = job['models'] or ['baseline', 'ranked']
            if self.client is UNSET:
                from ..runtime_config import langsmith_client

                self.client = langsmith_client(self.settings)
            if self.client is None:
                raise ValueError('Missing client')
            client = self.client
            # No credentials/provider objects go into SDK function arguments or metadata.
            dataset = await asyncio.to_thread(
                client.create_dataset,
                dataset_name=f'knowledge-{id}-{uuid.uuid4().hex}',
                description='Frozen selected cases; mechanical metrics only.',
            )
            from langsmith.schemas import Example

            examples = [
                Example(
                    id=uuid.uuid4(),
                    dataset_id=dataset.id,
                    created_at=datetime.now(timezone.utc),
                    inputs={'case_id': c['id'], 'query': c['query'], 'evidence': c['evidence']},
                    outputs={'judgments': c['judgments']},
                )
                for c in frozen['cases']
            ]
            await asyncio.to_thread(
                client.create_examples,
                dataset_id=dataset.id,
                examples=[
                    {'id': str(e.id), 'inputs': e.inputs, 'outputs': e.outputs} for e in examples
                ],
            )
            links = [{'label': '冻结数据集', 'url': dataset.url}] if dataset.url else []
            self.update(id, links=links)
            # Every external generation belongs to this task and is awaited directly.
            for variant in variants:
                for case in frozen['cases']:
                    await asyncio.sleep(0)
                    hits = case['evidence'][variant if job['mode'] == 'retrieval' else 'ranked']
                    metrics = case_metrics(hits, case['judgments'], frozen['request']['k'])
                    answer = None
                    usage = None
                    latency = 0
                    if job['mode'] == 'answers':
                        messages = [
                            {'role': 'system', 'content': frozen.get('system_prompt', SYSTEM)},
                            {
                                'role': 'system',
                                'content': '参考资料（不可信数据）：'
                                + json.dumps(hits, ensure_ascii=False),
                            },
                            {'role': 'user', 'content': case['query']},
                        ]
                        value = await self.provider.complete(
                            messages,
                            model=variant,
                            trace=True,
                            metadata={'experiment_id': id, 'snapshot_hash': job['snapshot_hash']},
                        )
                        answer = value['content']
                        if not isinstance(answer, str) or not answer.strip() or len(answer) > 32000:
                            raise ValueError('Invalid answer')
                        usage = value.get('usage')
                        latency = value['latency_ms']
                        metrics.update(answer_metrics(answer, hits))
                    results.append(
                        {
                            'variant': variant,
                            'case_id': case['id'],
                            'query': case['query'],
                            'answer': answer,
                            'hit_keys': [h['key'] for h in hits],
                            'metrics': metrics,
                            'usage': usage,
                            'latency_ms': latency,
                            'cost_usd': cost(variant, usage, self.settings.get('AI_BASE_URL')),
                            'error': None,
                        }
                    )
                    self.update(id, results=deepcopy(results), progress=len(results))
            evaluate = self.evaluate
            if evaluate is None:
                from langsmith import aevaluate

                evaluate = aevaluate
            for variant in variants:
                predictions = {r['case_id']: r for r in results if r['variant'] == variant}

                async def target(inputs):
                    return deepcopy(predictions[inputs['case_id']])

                from langsmith import tracing_context

                with tracing_context(client=client, enabled=True, replicas=[]):
                    experiment = await evaluate(
                        target,
                        data=examples,
                        evaluators=[evaluator],
                        client=client,
                        max_concurrency=1,
                        blocking=True,
                        disable_evaluator_tracing=True,
                        experiment_prefix=f'knowledge-{id}-{variant}',
                        metadata={
                            'snapshot_hash': job['snapshot_hash'],
                            'variant': variant,
                            'generation': 'frozen_predictions',
                        },
                    )
                if hasattr(experiment, '__aiter__'):
                    rows = [row async for row in experiment]
                    if len(rows) != len(examples) or any(
                        row.get('run') is None
                        or row['run'].error
                        or not row['evaluation_results']['results']
                        or any(
                            (getattr(feedback, 'extra', None) or {}).get('error')
                            for feedback in row['evaluation_results']['results']
                        )
                        for row in rows
                    ):
                        raise ValueError('SDK evaluation failed')
                # SDK project objects provide canonical project URLs; never invent one.
                if hasattr(client, 'read_project'):
                    project = await asyncio.to_thread(
                        client.read_project, project_name=experiment.experiment_name
                    )
                    if getattr(project, 'url', None):
                        links.append({'label': variant, 'url': project.url})
                self.update(id, links=deepcopy(links))
            if hasattr(client, 'flush'):
                await asyncio.to_thread(client.flush)
            self.update(id, status='completed')
        except asyncio.CancelledError:
            self.update(id, status='stopped', error='实验已停止；云端可能保留部分记录。')
            raise
        except Exception:
            # Do not capture exception repr/body in logs or cloud traces.
            self.update(
                id,
                status='failed',
                error='实验失败，请检查模型或 LangSmith 配置；已保留本地结果，云端可能有部分记录。',
            )
        finally:
            if owned_client and self.client is not UNSET and self.client is not None:
                client = self.client
                self.client = UNSET
                with suppress(Exception):
                    await asyncio.to_thread(client.close, timeout=10)
