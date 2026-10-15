"""Explicit bounded query-only comparison; no .env loading or document re-encoding."""

import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path

from app.db import create_db_engine, init_db
from app.evaluation_metrics import aggregate_metrics
from app.evaluation_schemas import DatasetInput
from app.llm import LangChainProvider
from app.rerank import DashScopeReranker
from app.services.evaluation import EvaluationService
from app.services.retrieval import RetrievalService
from scripts.import_nmm import validate_report_path, write_report


def load_dataset(path):
    raw = path.read_bytes()
    if len(raw) > 1024 * 1024:
        raise ValueError('Dataset exceeds 1 MiB')
    value = json.loads(raw)
    cases = []
    for case in value['cases']:
        cases.append(
            {
                'id': case['id'],
                'query': case['query'],
                'category': case.get('category', case.get('group')),
                'judgments': [
                    {'key': item['key'], 'grade': item.get('grade', item.get('relevance'))}
                    for item in case['judgments']
                ],
            }
        )
    parsed = DatasetInput.model_validate({**value, 'cases': cases})
    sha = hashlib.sha256(raw).hexdigest()
    return (
        {'id': 'file-' + sha[:32], 'corpus': 'live', **parsed.model_dump()},
        sha,
        value.get('label_origin', 'Unspecified; not established human gold'),
    )


class BudgetExhausted(RuntimeError):
    """Aborts the run; never treated as a provider error that degrades silently."""


class CallLedger:
    """Paid-call budget plus a response cache, so re-runs cost nothing.

    Only hashes of request inputs are used as keys; vectors and scores are local
    evaluation data and never contain credentials.
    """

    def __init__(self, path, max_calls):
        self.path, self.max_calls = path, max_calls
        self.data = json.loads(path.read_text()) if path and path.exists() else {}
        self.data.setdefault('calls', 0)
        self.data.setdefault('entries', {})

    def key(self, *parts):
        return hashlib.sha256(json.dumps(parts, ensure_ascii=False).encode()).hexdigest()

    async def fetch(self, key, call):
        if key in self.data['entries']:
            return self.data['entries'][key]
        if self.data['calls'] >= self.max_calls:
            raise BudgetExhausted('Paid call budget exhausted')
        self.data['calls'] += 1
        value = await call()
        self.data['entries'][key] = value
        if self.path:
            write_report(self.path, self.data)
        return value


class CachedEmbedder:
    def __init__(self, provider, ledger):
        self.provider, self.ledger = provider, ledger
        self.embedding_model = provider.embedding_model
        self.embedding_configured = provider.embedding_configured

    async def embed_query(self, text):
        key = self.ledger.key('embed', self.embedding_model, text)
        return await self.ledger.fetch(key, lambda: self.provider.embed_query(text))


class CachedReranker:
    def __init__(self, reranker, ledger):
        self.reranker, self.ledger = reranker, ledger
        self.model, self.configured = reranker.model, reranker.configured

    async def rerank(self, query, documents):
        key = self.ledger.key('rerank', self.model, query, documents)
        return await self.ledger.fetch(key, lambda: self.reranker.rerank(query, documents))


def threshold_sweep(cases, k, thresholds):
    """Offline re-scoring of stored rerank scores; no provider calls."""
    from app.evaluation_metrics import case_metrics

    rows = []
    for threshold in thresholds:
        metrics = []
        for case in cases:
            kept = [
                {'key': hit['key'], 'rank': rank}
                for rank, hit in enumerate(
                    (
                        hit
                        for hit in case['rerank_scored']
                        if hit['score'] >= threshold or hit['protected']
                    ),
                    1,
                )
            ][:k]
            if not case['rerank_scored']:
                kept = [{'key': hit['key'], 'rank': hit['rank']} for hit in case['rerank']]
            metrics.append(case_metrics(kept, case['judgments'], k))
        rows.append({'threshold': threshold, **aggregate_metrics(metrics)})
    return rows


def comparison_summary(report):
    groups = {}
    for category in sorted({case['category'] for case in report['cases']}):
        cases = [case for case in report['cases'] if case['category'] == category]
        groups[category] = {
            strategy: aggregate_metrics([case['metrics'][strategy] for case in cases])
            for strategy in report['strategies']
        }
    outside = [case for case in report['cases'] if not case['judgments']]
    return {
        'groups': groups,
        'unanswerable_cases': len(outside),
        'unanswerable_return_rate': {
            strategy: sum(bool(case[strategy]) for case in outside) / len(outside)
            if outside
            else None
            for strategy in report['strategies']
        },
        'limitations': 'Small local labelled sample; retrieval metrics and vector distance do not establish answer confidence or overall quality.',
    }


def main(argv=None, *, environ=None, provider=None, reranker_override=None):
    parser = argparse.ArgumentParser(
        description='Dry-run by default. --apply sends only the fixed case queries to the embedding provider.'
    )
    parser.add_argument('--database-url', required=True)
    parser.add_argument('--dataset', required=True, type=Path)
    parser.add_argument('--report', required=True, type=Path)
    parser.add_argument('--storage-dir', required=True, type=Path)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--max-queries', type=int, choices=range(1, 101), default=20)
    parser.add_argument('--k', type=int, choices=range(1, 21), default=5)
    parser.add_argument('--rerank', action='store_true', help='Also rerank hybrid candidates')
    parser.add_argument('--ledger', type=Path, help='Paid-call budget and response cache file')
    parser.add_argument(
        '--max-calls', type=int, default=0, help='Paid calls allowed (with --ledger)'
    )
    parser.add_argument('--sweep', default='', help='Comma-separated rerank thresholds to report')
    args = parser.parse_args(argv)
    engine = None
    try:
        output = validate_report_path(args.report, args.storage_dir)
        if output == args.dataset.resolve():
            raise ValueError('Report cannot replace dataset')
        value, sha, origin = load_dataset(args.dataset)
        if len(value['cases']) > args.max_queries:
            raise ValueError('Dataset exceeds explicit query budget')
        result = {
            'status': 'dry_run',
            'query_budget': len(value['cases']),
            'source_sha256': sha,
            'label_origin': origin,
        }
        if args.apply:
            env = os.environ if environ is None else environ
            configured = provider or LangChainProvider(
                settings={
                    key: env.get(key, '')
                    for key in ('EMBEDDING_BASE_URL', 'EMBEDDING_API_KEY', 'EMBEDDING_MODEL')
                }
            )
            reranker = reranker_override or DashScopeReranker(
                {
                    key: env.get(key, '')
                    for key in ('RERANK_BASE_URL', 'RERANK_API_KEY', 'RERANK_MODEL')
                }
            )
            if args.ledger:
                ledger = CallLedger(
                    validate_report_path(args.ledger, args.storage_dir), args.max_calls
                )
                configured = CachedEmbedder(configured, ledger)
                reranker = CachedReranker(reranker, ledger)
            engine = create_db_engine(args.database_url)
            init_db(engine)
            retrieval = RetrievalService(
                engine,
                lambda: configured,
                reranker_getter=lambda: reranker,
                rerank_min_score=float(env.get('RERANK_MIN_SCORE') or 0),
                rerank_candidates=int(env.get('RERANK_CANDIDATES') or 20),
            )
            service = EvaluationService(engine, retrieval)
            report = asyncio.run(service.compare_dataset(value, args.k, rerank=args.rerank))
            result.update(status='completed', comparison=report, summary=comparison_summary(report))
            if args.rerank and args.sweep:
                thresholds = [float(item) for item in args.sweep.split(',')]
                result['threshold_sweep'] = threshold_sweep(report['cases'], args.k, thresholds)
            if args.ledger:
                result['paid_calls_total'] = ledger.data['calls']
        write_report(output, result)
        print(
            json.dumps(
                {'status': result['status'], 'query_budget': result['query_budget']},
                ensure_ascii=False,
            )
        )
        return 0
    except Exception:
        # Neither provider errors nor database URLs/credentials are emitted.
        print(
            json.dumps(
                {
                    'status': 'failed',
                    'error': 'Comparison not completed; check explicit paths, dataset, budget, database migration and provider configuration.',
                }
            )
        )
        return 2
    finally:
        if engine is not None:
            engine.dispose()


if __name__ == '__main__':
    raise SystemExit(main())
