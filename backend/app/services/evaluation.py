"""Evaluation orchestration; provider awaits never hold a database transaction."""

import asyncio
from copy import deepcopy
from datetime import datetime, timezone
from time import perf_counter
from uuid import uuid4

from sqlalchemy.orm import Session

from ..evaluation_demo import demo_fixture
from ..evaluation_metrics import aggregate_metrics, case_metrics, fingerprint
from ..repositories.evaluation import EvaluationRepository, dataset
from ..retrieval import load_documents, rank_documents, RETRIEVAL_VERSION
from .errors import ServiceError
from .retrieval import BM25_VERSION, HYBRID_VERSION


class EvaluationService:
    def __init__(self, engine, retrieval):
        self.engine = engine
        self.retrieval = retrieval

    def datasets(self):
        with Session(self.engine) as db:
            return EvaluationRepository(db).datasets()

    def dataset(self, id):
        with Session(self.engine) as db:
            return dataset(db, id)

    def save_dataset(self, data, id=None):
        if id == 'demo-v1':
            raise ServiceError(409, 'Built-in dataset is immutable; import a copy instead')
        payload = {
            'id': id or str(uuid4()),
            **data.model_dump(),
            'corpus': 'live',
            'builtin': False,
            'case_count': len(data.cases),
        }
        with Session(self.engine) as db, db.begin():
            EvaluationRepository(db).save_dataset(payload, updating=id is not None)
        return payload

    def delete_dataset(self, id):
        if id == 'demo-v1':
            raise ServiceError(409, 'Built-in dataset is immutable')
        with Session(self.engine) as db, db.begin():
            EvaluationRepository(db).delete_dataset(id)
        return {'deleted': True}

    def reports(self):
        with Session(self.engine) as db:
            return EvaluationRepository(db).reports()

    def report(self, id):
        with Session(self.engine) as db:
            return EvaluationRepository(db).report(id)

    def preview_legacy(self, query, k):
        with Session(self.engine) as db:
            documents = load_documents(db)
        return {
            'query': query,
            'k': k,
            'corpus_size': len(documents),
            'version': RETRIEVAL_VERSION,
            **{
                strategy: rank_documents(documents, query, strategy=strategy, k=k)
                for strategy in ('baseline', 'ranked')
            },
        }

    @staticmethod
    def _check_labels(value, keys):
        missing = sorted({j['key'] for case in value['cases'] for j in case['judgments']} - keys)
        if missing:
            raise ServiceError(
                422,
                '预期资料不存在或已不可用（可能已撤回、未发布或来源不可用），请修改标注后重试：'
                + ', '.join(missing),
            )

    @staticmethod
    def _payload(value, k, keys, corpus_hash, cases, versions):
        return {
            'dataset_id': value['id'],
            'dataset_name': value['name'],
            'created_at': datetime.now(timezone.utc).isoformat(),
            'k': k,
            'corpus': value['corpus'],
            'corpus_size': len(keys),
            'dataset_hash': fingerprint(value['cases']),
            'corpus_hash': corpus_hash,
            'versions': versions,
            'metrics': {
                strategy: aggregate_metrics([case['metrics'][strategy] for case in cases])
                for strategy in versions
            },
            'cases': cases,
        }

    def _legacy_run(self, data):
        with Session(self.engine) as db, db.begin():
            value = dataset(db, data.dataset_id)
            documents = demo_fixture()[1] if value['corpus'] == 'demo' else load_documents(db)
            keys = {item['key'] for item in documents}
            self._check_labels(value, keys)
            cases = []
            for case in value['cases']:
                hits = {
                    strategy: rank_documents(documents, case['query'], strategy=strategy, k=data.k)
                    for strategy in ('baseline', 'ranked')
                }
                cases.append(
                    {
                        **deepcopy(case),
                        **hits,
                        'metrics': {
                            strategy: case_metrics(items, case['judgments'], data.k)
                            for strategy, items in hits.items()
                        },
                    }
                )
            payload = self._payload(
                value,
                data.k,
                keys,
                fingerprint(documents),
                cases,
                {'baseline': 'v2-lexical', 'ranked': RETRIEVAL_VERSION},
            )
            return EvaluationRepository(db).save_report(payload)

    def _compare_snapshot(self, cases, k, prepared):
        # Both online strategies run on exactly one materialized database snapshot.
        with (
            self.engine.connect().execution_options(isolation_level='REPEATABLE READ') as conn,
            conn.begin(),
            Session(bind=conn) as db,
        ):
            identity, keys = EvaluationRepository(db).corpus_identity()
            results = []
            for case, query_vector in zip(cases, prepared):
                pair = {}
                for strategy in ('bm25', 'hybrid'):
                    start = perf_counter()
                    result = self.retrieval.search_prepared(
                        db, case['query'], k=k, strategy=strategy, prepared=query_vector
                    )
                    result.diagnostics['latency_ms'] = round(
                        (perf_counter() - start) * 1000
                        + (query_vector.embedding_ms if strategy == 'hybrid' else 0),
                        2,
                    )
                    pair[strategy] = result
                results.append(
                    {
                        **deepcopy(case),
                        **{strategy: result.hits for strategy, result in pair.items()},
                        'metrics': {
                            strategy: case_metrics(result.hits, case['judgments'], k)
                            for strategy, result in pair.items()
                        },
                        'diagnostics': {
                            strategy: result.metadata() for strategy, result in pair.items()
                        },
                    }
                )
            return identity, keys, results

    def _publish(self, value, payload):
        with Session(self.engine) as db, db.begin():
            current, _ = EvaluationRepository(db).corpus_identity()
            if (
                current != payload['corpus_hash']
                or fingerprint(dataset(db, value['id'])['cases']) != payload['dataset_hash']
            ):
                raise ServiceError(409, '测评期间资料或标注已变化，请重新运行；未保存不可比结果。')
            return EvaluationRepository(db).save_report(payload)

    async def run(self, data):
        if data.strategies is None:
            return await asyncio.to_thread(self._legacy_run, data)
        value = await asyncio.to_thread(self.dataset, data.dataset_id)
        payload = await self.compare_dataset(value, data.k)
        return await asyncio.to_thread(self._publish, value, payload)

    async def compare_dataset(self, value, k=5):
        """Compare a file or stored dataset without writing a historical report."""
        if value['corpus'] != 'live':
            raise ServiceError(422, '混合检索比较仅支持真实资料测评集。')
        # Validate labels before any paid query is sent.
        with Session(self.engine) as db:
            _, keys = EvaluationRepository(db).corpus_identity()
        self._check_labels(value, keys)
        prepared = []
        for case in value['cases']:
            prepared.append(await self.retrieval.prepare_query(case['query']))
        identity, keys, cases = await asyncio.to_thread(
            self._compare_snapshot, value['cases'], k, prepared
        )
        self._check_labels(value, keys)
        with Session(self.engine) as db:
            current, _ = EvaluationRepository(db).corpus_identity()
        if current != identity:
            raise ServiceError(409, '测评期间资料已变化，请重新运行。')
        payload = self._payload(
            value, k, keys, identity, cases, {'bm25': BM25_VERSION, 'hybrid': HYBRID_VERSION}
        )
        payload.update(
            report_version=2,
            strategies=['bm25', 'hybrid'],
            degraded_cases=sum(
                case['diagnostics']['hybrid']['effective_strategy'] != 'hybrid' for case in cases
            ),
        )
        return payload

    async def preview(self, query, k):
        prepared = await self.retrieval.prepare_query(query)
        identity, keys, cases = await asyncio.to_thread(
            self._compare_snapshot,
            [{'id': 'preview', 'query': query, 'category': 'preview', 'judgments': []}],
            k,
            [prepared],
        )
        with Session(self.engine) as db:
            current, _ = EvaluationRepository(db).corpus_identity()
        if current != identity:
            raise ServiceError(409, '资料已变化，请重新检索。')
        case = cases[0]
        return {
            'query': query,
            'k': k,
            'corpus_size': len(keys),
            'version': HYBRID_VERSION,
            'bm25': case['bm25'],
            'hybrid': case['hybrid'],
            'diagnostics': case['diagnostics'],
        }
