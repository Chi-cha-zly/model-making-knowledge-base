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


def comparison_summary(report):
    groups = {}
    for category in sorted({case['category'] for case in report['cases']}):
        cases = [case for case in report['cases'] if case['category'] == category]
        groups[category] = {
            strategy: aggregate_metrics([case['metrics'][strategy] for case in cases])
            for strategy in ('bm25', 'hybrid')
        }
    outside = [case for case in report['cases'] if not case['judgments']]
    return {
        'groups': groups,
        'unanswerable_cases': len(outside),
        'unanswerable_return_rate': {
            strategy: sum(bool(case[strategy]) for case in outside) / len(outside)
            if outside
            else None
            for strategy in ('bm25', 'hybrid')
        },
        'limitations': 'Small local labelled sample; retrieval metrics and vector distance do not establish answer confidence or overall quality.',
    }


def main(argv=None, *, environ=None, provider=None):
    parser = argparse.ArgumentParser(
        description='Dry-run by default. --apply sends only the fixed case queries to the embedding provider.'
    )
    parser.add_argument('--database-url', required=True)
    parser.add_argument('--dataset', required=True, type=Path)
    parser.add_argument('--report', required=True, type=Path)
    parser.add_argument('--storage-dir', required=True, type=Path)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--max-queries', type=int, choices=range(1, 21), default=20)
    parser.add_argument('--k', type=int, choices=range(1, 21), default=5)
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
            engine = create_db_engine(args.database_url)
            init_db(engine)
            service = EvaluationService(engine, RetrievalService(engine, lambda: configured))
            report = asyncio.run(service.compare_dataset(value, args.k))
            result.update(status='completed', comparison=report, summary=comparison_summary(report))
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
