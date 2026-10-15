"""Versioned evaluation compares the online strategies without rewriting history."""

from copy import deepcopy

import pytest
from fastapi.testclient import TestClient
from pg_test_support import test_database_url
from app.main import create_app
from test_hybrid_retrieval import QueryProvider, seed
from test_evaluation import manifest, archive


@pytest.fixture
def client(tmp_path):
    engine, ids = seed()
    engine.dispose()
    provider = QueryProvider()
    with TestClient(
        create_app(test_database_url(), tmp_path / 'uploads', provider=provider)
    ) as client:
        created = client.post(
            '/api/v1/evaluations/datasets',
            json={
                'name': 'synthetic semantic',
                'cases': [
                    {
                        'id': 'one',
                        'query': '表面反射光亮',
                        'category': 'paraphrase',
                        'judgments': [{'key': f'technique:{ids[0]}', 'grade': 3}],
                    }
                ],
            },
        ).json()
        yield client, created, provider


def test_explicit_hybrid_report_and_preview_use_versioned_strategies(client):
    client, dataset, provider = client
    result = client.post(
        '/api/v1/evaluations/runs',
        json={'dataset_id': dataset['id'], 'strategies': ['bm25', 'hybrid']},
    )
    assert result.status_code == 200, result.text
    report = result.json()
    assert report['report_version'] == 2
    assert set(report['versions']) == {'bm25', 'hybrid'}
    assert report['metrics']['hybrid']['hit_rate'] == 1
    assert report['cases'][0]['diagnostics']['hybrid']['effective_strategy'] == 'hybrid'
    assert len(provider.calls) == 1
    preview = client.post(
        '/api/v1/retrieval/preview', json={'query': '表面反射光亮', 'k': 5}
    ).json()
    assert preview['hybrid'][0]['key'] == report['cases'][0]['hybrid'][0]['key']
    assert preview['diagnostics']['hybrid']['effective_strategy'] == 'hybrid'


def test_legacy_default_and_demo_stay_offline(client):
    client, _, provider = client
    report = client.post('/api/v1/evaluations/runs', json={'dataset_id': 'demo-v1'}).json()
    assert 'report_version' not in report
    assert set(report['versions']) == {'baseline', 'ranked'}
    result = client.post(
        '/api/v1/evaluations/runs', json={'dataset_id': 'demo-v1', 'strategies': ['bm25', 'hybrid']}
    )
    assert result.status_code == 422
    assert provider.calls == []


def test_new_reports_round_trip_backup_and_reject_metric_or_version_tampering(client):
    client, dataset, _ = client
    report = client.post(
        '/api/v1/evaluations/runs',
        json={'dataset_id': dataset['id'], 'strategies': ['bm25', 'hybrid']},
    ).json()
    assert report.get('report_version') == 2
    backup = manifest(client)
    result = client.post(
        '/api/v1/backup', files={'file': ('backup.zip', archive(backup), 'application/zip')}
    )
    assert result.status_code == 200, result.text
    assert client.get(f'/api/v1/evaluations/runs/{report["id"]}').json() == report
    from app.evaluation import validate_evaluation_backup

    for change in ('metric', 'version'):
        broken = deepcopy(backup)
        if change == 'metric':
            broken['evaluation_runs'][0]['metrics']['hybrid']['recall'] = 9
        else:
            broken['evaluation_runs'][0]['report_version'] = 99
        with pytest.raises(ValueError):
            validate_evaluation_backup(broken)


def test_unconfigured_hybrid_report_discloses_degradation(client):
    client, dataset, provider = client
    provider.embedding_configured = False
    response = client.post(
        '/api/v1/evaluations/runs',
        json={'dataset_id': dataset['id'], 'strategies': ['bm25', 'hybrid']},
    )
    assert response.status_code == 200
    report = response.json()
    assert report['degraded_cases'] == 1
    assert report['cases'][0]['diagnostics']['hybrid']['fallback_reason'] == 'unconfigured'
    assert provider.calls == []


@pytest.mark.parametrize(
    'headers', [{'Origin': 'https://foreign.invalid'}, {'Sec-Fetch-Site': 'cross-site'}]
)
def test_paid_search_paths_reject_cross_site_before_model_call(client, headers):
    client, dataset, provider = client
    client.app.state.retrieval.mode = 'hybrid'
    for method, path, body in [
        ('get', '/api/v1/search?q=肩甲', None),
        ('post', '/api/v1/retrieval/preview', {'query': '肩甲', 'k': 5}),
        (
            'post',
            '/api/v1/evaluations/runs',
            {'dataset_id': dataset['id'], 'strategies': ['bm25', 'hybrid']},
        ),
    ]:
        result = getattr(client, method)(path, headers=headers, **({'json': body} if body else {}))
        assert result.status_code == 403, result.text
    assert provider.calls == []


def test_catalog_search_retains_collections_and_exposes_actual_strategy(client):
    client, _, provider = client
    result = client.get('/api/v1/search?q=肩甲').json()
    assert set(result) == {'techniques', 'products', 'ranking', 'retrieval'}
    assert result['retrieval']['effective_strategy'] == 'bm25'
    assert provider.calls == []


@pytest.mark.parametrize('change', ['fallback', 'version', 'bm25_vector', 'timing'])
def test_v2_codec_rejects_contradictory_strategy_diagnostics(client, change):
    from app.evaluation_codec import _validate_report

    client, dataset, _ = client
    report = client.post(
        '/api/v1/evaluations/runs',
        json={'dataset_id': dataset['id'], 'strategies': ['bm25', 'hybrid']},
    ).json()
    diagnostic = report['cases'][0]['diagnostics']
    if change == 'fallback':
        diagnostic['hybrid'].update(fallback_reason='timeout', model=None, dimensions=None)
    elif change == 'version':
        diagnostic['hybrid']['version'] = 'invented'
    elif change == 'bm25_vector':
        diagnostic['bm25'].update(
            model='text-embedding-v4', dimensions=1024, vector_candidates=1, embedding_ms=10
        )
    else:
        diagnostic['hybrid'].update(embedding_ms=100, latency_ms=1)
    with pytest.raises(ValueError):
        _validate_report(report)


@pytest.mark.parametrize('change', ['source', 'dataset'])
def test_evaluation_does_not_publish_if_snapshot_changes(client, monkeypatch, change):
    from sqlalchemy import select
    from sqlalchemy.orm import Session
    from app.models import Source
    from app.evaluation_models import EvaluationDataset

    client, dataset, _ = client
    service = client.app.state.evaluation
    original = service._compare_snapshot

    def mutate_after_snapshot(*args):
        snapshot = original(*args)
        with Session(service.engine) as db:
            if change == 'source':
                db.scalar(select(Source)).status = 'withdrawn'
            else:
                row = db.get(EvaluationDataset, dataset['id'])
                payload = deepcopy(row.payload)
                payload['cases'][0]['query'] = 'changed'
                row.payload = payload
            db.commit()
        return snapshot

    monkeypatch.setattr(service, '_compare_snapshot', mutate_after_snapshot)
    response = client.post(
        '/api/v1/evaluations/runs',
        json={'dataset_id': dataset['id'], 'strategies': ['bm25', 'hybrid']},
    )
    assert response.status_code == 409, response.text
    assert client.get('/api/v1/evaluations/runs').json() == []


def test_valid_historical_v2_versions_remain_importable(client):
    from app.evaluation_codec import _validate_report

    client, dataset, _ = client
    report = client.post(
        '/api/v1/evaluations/runs',
        json={'dataset_id': dataset['id'], 'strategies': ['bm25', 'hybrid']},
    ).json()
    report['versions'] = {'bm25': 'past-bm25', 'hybrid': 'past-hybrid'}
    for metadata in report['cases'][0]['diagnostics'].values():
        metadata['version'] = report['versions'][metadata['effective_strategy']]
    _validate_report(report)


def test_runtime_retrieval_mode_uses_explicit_settings_then_runtime_configuration(
    tmp_path, monkeypatch
):
    from app.services import runtime

    monkeypatch.setattr(runtime, 'read_settings', lambda: {'RETRIEVAL_MODE': 'hybrid'})
    provider = QueryProvider()
    provider.settings = {'EMBEDDING_MODEL': 'text-embedding-v4'}
    app = create_app(test_database_url(), tmp_path / 'uploads', provider=provider)
    assert app.state.retrieval.mode == 'hybrid'
    explicit = create_app(
        test_database_url(),
        tmp_path / 'uploads',
        provider=provider,
        runtime_settings={'RETRIEVAL_MODE': 'lexical'},
    )
    assert explicit.state.retrieval.mode == 'lexical'
    app.state.engine.dispose()
    explicit.state.engine.dispose()


@pytest.mark.parametrize('headers', [{'Sec-Fetch-Site': 'cross-site'}])
def test_rerank_mode_search_also_rejects_cross_site(client, headers):
    client, _, provider = client
    client.app.state.retrieval.mode = 'rerank'
    result = client.get('/api/v1/search?q=肩甲', headers=headers)
    assert result.status_code == 403, result.text
    assert provider.calls == []
