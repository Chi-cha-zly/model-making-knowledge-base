"""Version-dispatched immutable reports and backward compatible backup validation."""

from copy import deepcopy
from datetime import datetime
import json
import math
import re
from sqlalchemy import select, delete
from .evaluation_models import EvaluationDataset, EvaluationRun
from .evaluation_schemas import DatasetInput
from .evaluation_metrics import fingerprint, case_metrics, aggregate_metrics

MAX_REQUEST = 1024 * 1024


def export_evaluation(db):
    return {
        'evaluation_datasets': [
            r.payload for r in db.scalars(select(EvaluationDataset).order_by(EvaluationDataset.id))
        ],
        'evaluation_runs': [
            r.payload for r in db.scalars(select(EvaluationRun).order_by(EvaluationRun.id))
        ],
    }


def _integer(value, minimum=0):
    if type(value) is not int or value < minimum:
        raise ValueError('Invalid evaluation integer')


def _validate_metrics(actual, expected):
    if not isinstance(actual, dict) or set(actual) != set(expected):
        raise ValueError('Invalid evaluation metrics')
    for key, value in expected.items():
        got = actual[key]
        if value is None:
            if got is not None:
                raise ValueError('Invalid metric denominator')
        elif type(got) not in (int, float) or not math.isfinite(got) or abs(got - value) > 1e-12:
            raise ValueError('Inconsistent evaluation metric')


def _validate_report_v1(report):
    required = {
        'id',
        'dataset_id',
        'dataset_name',
        'created_at',
        'k',
        'corpus',
        'corpus_size',
        'dataset_hash',
        'corpus_hash',
        'versions',
        'metrics',
        'cases',
    }
    if not isinstance(report, dict) or set(report) != required:
        raise ValueError('Invalid evaluation report fields')
    _integer(report['id'], 1)
    _integer(report['k'], 1)
    _integer(report['corpus_size'])
    if report['k'] > 20 or report['corpus'] not in ('demo', 'live'):
        raise ValueError('Invalid evaluation report configuration')
    for key, limit in [('dataset_id', 80), ('dataset_name', 200)]:
        if not isinstance(report[key], str) or not 1 <= len(report[key]) <= limit:
            raise ValueError('Invalid evaluation identity')
    if not isinstance(report['created_at'], str):
        raise ValueError('Invalid evaluation time')
    datetime.fromisoformat(report['created_at'])
    for key in ('dataset_hash', 'corpus_hash'):
        if not isinstance(report[key], str) or not re.fullmatch('[0-9a-f]{64}', report[key]):
            raise ValueError('Invalid evaluation fingerprint')
    versions = report['versions']
    if (
        not isinstance(versions, dict)
        or set(versions) != {'baseline', 'ranked'}
        or any(not isinstance(v, str) or not 1 <= len(v) <= 100 for v in versions.values())
    ):
        raise ValueError('Invalid evaluation algorithm versions')
    cases = report['cases']
    if not isinstance(cases, list) or not 1 <= len(cases) <= 200:
        raise ValueError('Invalid evaluation cases')
    case_snapshot = [{k: c[k] for k in ('id', 'query', 'category', 'judgments')} for c in cases]
    DatasetInput.model_validate({'name': report['dataset_name'], 'cases': case_snapshot})
    if fingerprint(case_snapshot) != report['dataset_hash']:
        raise ValueError('Evaluation dataset fingerprint mismatch')
    for case in cases:
        if (
            set(case) != {'id', 'query', 'category', 'judgments', 'baseline', 'ranked', 'metrics'}
            or not isinstance(case['metrics'], dict)
            or set(case['metrics']) != {'baseline', 'ranked'}
        ):
            raise ValueError('Invalid case result')
        for strategy in ('baseline', 'ranked'):
            hits = case[strategy]
            if not isinstance(hits, list) or len(hits) > min(report['k'], report['corpus_size']):
                raise ValueError('Invalid evaluation hits')
            seen = set()
            for rank, hit in enumerate(hits, 1):
                if not isinstance(hit, dict) or set(hit) != {
                    'key',
                    'type',
                    'id',
                    'title',
                    'excerpt',
                    'source_ids',
                    'sources',
                    'rank',
                    'score',
                    'reasons',
                }:
                    raise ValueError('Invalid evaluation hit fields')
                _integer(hit['id'], 1)
                _integer(hit['rank'], 1)
                if (
                    hit['type'] not in ('technique', 'product')
                    or hit['key'] != f'{hit["type"]}:{hit["id"]}'
                    or hit['key'] in seen
                    or hit['rank'] != rank
                ):
                    raise ValueError('Invalid evaluation hit identity')
                seen.add(hit['key'])
                if (
                    any(not isinstance(hit[k], str) for k in ('title', 'excerpt'))
                    or type(hit['score']) not in (int, float)
                    or not math.isfinite(hit['score'])
                ):
                    raise ValueError('Invalid evaluation hit content')
                if not isinstance(hit['reasons'], list) or any(
                    not isinstance(r, str) for r in hit['reasons']
                ):
                    raise ValueError('Invalid evaluation reasons')
                if not isinstance(hit['sources'], list) or not isinstance(hit['source_ids'], list):
                    raise ValueError('Invalid evaluation sources')
                for source in hit['sources']:
                    if (
                        not isinstance(source, dict)
                        or not isinstance(source.get('title'), str)
                        or (source.get('url') is not None and not isinstance(source['url'], str))
                    ):
                        raise ValueError('Invalid evaluation source')
                    _integer(source.get('id'), 1)
                if any(type(id) is not int for id in hit['source_ids']) or hit['source_ids'] != [
                    s['id'] for s in hit['sources']
                ]:
                    raise ValueError('Invalid evaluation source references')
            _validate_metrics(
                case['metrics'][strategy], case_metrics(hits, case['judgments'], report['k'])
            )
    if not isinstance(report['metrics'], dict) or set(report['metrics']) != {'baseline', 'ranked'}:
        raise ValueError('Invalid report metrics')
    for strategy in ('baseline', 'ranked'):
        _validate_metrics(
            report['metrics'][strategy], aggregate_metrics([c['metrics'][strategy] for c in cases])
        )


def validate_evaluation_backup(data):
    if data['version'] < 3:
        return
    for key in ('evaluation_datasets', 'evaluation_runs'):
        if not isinstance(data.get(key), list):
            raise ValueError('Missing or invalid evaluation table')
    ids = set()
    for row in data['evaluation_datasets']:
        if not isinstance(row, dict) or set(row) != {
            'id',
            'name',
            'description',
            'cases',
            'case_count',
            'corpus',
            'builtin',
        }:
            raise ValueError('Invalid evaluation dataset fields')
        id = row['id']
        if not isinstance(id, str) or not 1 <= len(id) <= 80 or id == 'demo-v1' or id in ids:
            raise ValueError('Invalid or duplicate evaluation dataset id')
        ids.add(id)
        parsed = DatasetInput.model_validate(row)
        if (
            row['corpus'] != 'live'
            or row['builtin'] is not False
            or type(row['case_count']) is not int
            or row['case_count'] != len(parsed.cases)
        ):
            raise ValueError('Invalid evaluation dataset metadata')
        # Measure the minimal accepted input representation, not backup metadata or
        # pretty-print spacing. Every accepted <=1 MiB import must round-trip.
        canonical_input = json.dumps(
            parsed.model_dump(exclude_defaults=True), ensure_ascii=False, separators=(',', ':')
        ).encode('utf-8')
        if len(canonical_input) > MAX_REQUEST:
            raise ValueError('Evaluation dataset exceeds 1 MiB')
    ids = set()
    for report in data['evaluation_runs']:
        _validate_report(report)
        if report['id'] in ids:
            raise ValueError('Duplicate evaluation report id')
        ids.add(report['id'])


def restore_evaluation(db, data):
    if data['version'] < 3:
        return  # Old backups preserve datasets and historical snapshots.
    for model in (EvaluationRun, EvaluationDataset):
        db.execute(delete(model))
    for payload in data['evaluation_datasets']:
        db.add(EvaluationDataset(id=payload['id'], payload=payload))
    for payload in data['evaluation_runs']:
        db.add(EvaluationRun(id=payload['id'], payload=payload))


def _validate_report(report):
    if not isinstance(report, dict):
        raise ValueError('Invalid evaluation report')
    if 'report_version' not in report:
        return _validate_report_v1(report)
    if type(report['report_version']) is not int or report['report_version'] != 2:
        raise ValueError('Unknown evaluation report version')
    extras = {'report_version', 'strategies', 'degraded_cases'}
    if report.get('strategies') != ['bm25', 'hybrid'] or report.get('corpus') != 'live':
        raise ValueError('Invalid hybrid report strategies')
    _integer(report.get('degraded_cases'))
    if set(report.get('versions', {})) != {'bm25', 'hybrid'} or set(report.get('metrics', {})) != {
        'bm25',
        'hybrid',
    }:
        raise ValueError('Invalid hybrid algorithm versions or metrics')
    converted = {key: deepcopy(value) for key, value in report.items() if key not in extras}
    converted['versions'] = {
        'baseline': report['versions']['bm25'],
        'ranked': report['versions']['hybrid'],
    }
    converted['metrics'] = {
        'baseline': report['metrics']['bm25'],
        'ranked': report['metrics']['hybrid'],
    }
    converted['cases'] = []
    degraded = 0
    reasons = {
        None,
        'empty_query',
        'unconfigured',
        'unsupported_model',
        'no_ready_vectors',
        'timeout',
        'provider_error',
        'invalid_vector',
    }
    for case in report.get('cases', []):
        if set(case) != {
            'id',
            'query',
            'category',
            'judgments',
            'bm25',
            'hybrid',
            'metrics',
            'diagnostics',
        }:
            raise ValueError('Invalid hybrid case fields')
        if set(case['metrics']) != {'bm25', 'hybrid'} or set(case['diagnostics']) != {
            'bm25',
            'hybrid',
        }:
            raise ValueError('Invalid hybrid diagnostics')
        for strategy, diagnostic in case['diagnostics'].items():
            required = {
                'requested_strategy',
                'effective_strategy',
                'version',
                'fallback_reason',
                'query_truncated',
                'lexical_candidates',
                'vector_candidates',
                'model',
                'dimensions',
                'latency_ms',
                'embedding_ms',
            }
            if not isinstance(diagnostic, dict) or set(diagnostic) != required:
                raise ValueError('Invalid retrieval metadata fields')
            if diagnostic['requested_strategy'] != strategy or diagnostic[
                'effective_strategy'
            ] not in ('bm25', 'hybrid'):
                raise ValueError('Invalid effective strategy')
            if strategy == 'bm25' and diagnostic['effective_strategy'] != 'bm25':
                raise ValueError('BM25 cannot invoke vector search')
            if (
                diagnostic['fallback_reason'] not in reasons
                or type(diagnostic['query_truncated']) is not bool
            ):
                raise ValueError('Invalid fallback metadata')
            for field in ('lexical_candidates', 'vector_candidates'):
                _integer(diagnostic[field])
                if diagnostic[field] > 200:
                    raise ValueError('Invalid candidate count')
            if (
                not isinstance(diagnostic['version'], str)
                or not 1 <= len(diagnostic['version']) <= 100
            ):
                raise ValueError('Invalid retrieval version')
            if diagnostic['model'] is not None and (
                not isinstance(diagnostic['model'], str) or len(diagnostic['model']) > 200
            ):
                raise ValueError('Invalid embedding model')
            if diagnostic['dimensions'] is not None and (
                type(diagnostic['dimensions']) is not int or diagnostic['dimensions'] != 1024
            ):
                raise ValueError('Invalid embedding dimensions')
            if (
                type(diagnostic['embedding_ms']) not in (int, float)
                or not math.isfinite(diagnostic['embedding_ms'])
                or diagnostic['embedding_ms'] < 0
            ):
                raise ValueError('Invalid embedding latency')
            if (
                type(diagnostic['latency_ms']) not in (int, float)
                or not math.isfinite(diagnostic['latency_ms'])
                or diagnostic['latency_ms'] < 0
            ):
                raise ValueError('Invalid retrieval latency')
            effective = diagnostic['effective_strategy']
            if diagnostic['version'] != report['versions'][effective]:
                raise ValueError('Inconsistent retrieval version')
            if diagnostic['latency_ms'] < diagnostic['embedding_ms']:
                raise ValueError('Embedding latency exceeds total latency')
            if strategy == 'bm25':
                if (
                    diagnostic['fallback_reason'] is not None
                    or diagnostic['model'] is not None
                    or diagnostic['dimensions'] is not None
                    or diagnostic['vector_candidates'] != 0
                    or diagnostic['embedding_ms'] != 0
                    or diagnostic['query_truncated']
                ):
                    raise ValueError('BM25 cannot include embedding diagnostics')
            elif effective == 'hybrid':
                if (
                    diagnostic['fallback_reason'] is not None
                    or not diagnostic['model']
                    or diagnostic['dimensions'] != 1024
                ):
                    raise ValueError('Hybrid requires a successful query embedding')
            elif (
                diagnostic['fallback_reason'] is None
                or diagnostic['dimensions'] is not None
                or diagnostic['vector_candidates'] != 0
            ):
                raise ValueError('Degraded hybrid requires explicit fallback diagnostics')
            for hit in case[strategy]:
                if (
                    'attachment_ids' not in hit
                    or not isinstance(hit['attachment_ids'], list)
                    or any(type(id) is not int or id < 1 for id in hit['attachment_ids'])
                    or len(hit['attachment_ids']) != len(set(hit['attachment_ids']))
                ):
                    raise ValueError('Invalid evaluation attachment references')
        degraded += case['diagnostics']['hybrid']['effective_strategy'] != 'hybrid'
        converted['cases'].append(
            {
                **{key: deepcopy(case[key]) for key in ('id', 'query', 'category', 'judgments')},
                'baseline': [
                    {key: value for key, value in hit.items() if key != 'attachment_ids'}
                    for hit in case['bm25']
                ],
                'ranked': [
                    {key: value for key, value in hit.items() if key != 'attachment_ids'}
                    for hit in case['hybrid']
                ],
                'metrics': {
                    'baseline': case['metrics']['bm25'],
                    'ranked': case['metrics']['hybrid'],
                },
            }
        )
    if degraded != report['degraded_cases']:
        raise ValueError('Invalid degraded case count')
    _validate_report_v1(converted)
