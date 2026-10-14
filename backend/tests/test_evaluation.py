from pg_test_support import test_database_url
import copy
import json
import math
from io import BytesIO
from zipfile import ZipFile

import pytest
from fastapi.testclient import TestClient
from app.main import create_app

API = '/api/v1'


@pytest.fixture
def client(tmp_path):
    with TestClient(
        create_app(test_database_url(f'{tmp_path / "test.db"}'), tmp_path / 'uploads')
    ) as c:
        yield c


def dataset(client, judgments=None):
    body = {
        'name': '中文测试',
        'cases': [{'id': 'one', 'query': '打磨', 'category': '步骤', 'judgments': judgments or []}],
    }
    r = client.post(API + '/evaluations/datasets', json=body)
    assert r.status_code == 201, r.text
    return r.json()


def archive(payload):
    raw = BytesIO()
    with ZipFile(raw, 'w') as z:
        z.writestr('manifest.json', json.dumps(payload))
    return raw.getvalue()


def manifest(client):
    with ZipFile(BytesIO(client.get(API + '/backup').content)) as z:
        return json.loads(z.read('manifest.json'))


def test_demo_is_isolated_deterministic_and_history_survives_dataset_delete(client):
    listing = client.get(API + '/evaluations/datasets')
    assert listing.status_code == 200
    demo = client.get(API + '/evaluations/datasets/demo-v1').json()
    assert demo['case_count'] >= 24
    assert len({c['category'] for c in demo['cases']}) >= 6
    assert client.get(API + '/techniques').json() == []
    assert client.get(API + '/products').json() == []
    a = client.post(API + '/evaluations/runs', json={'dataset_id': 'demo-v1', 'k': 5})
    assert a.status_code == 200, a.text
    a = a.json()
    b = client.post(API + '/evaluations/runs', json={'dataset_id': 'demo-v1', 'k': 5}).json()
    assert a['cases'] == b['cases'] and a['corpus_hash'] == b['corpus_hash']
    assert a['metrics']['ranked']['positive_cases'] > 0
    assert a['metrics']['ranked']['no_answer_cases'] > 0
    custom = dataset(client)
    r = client.post(API + '/evaluations/runs', json={'dataset_id': custom['id']}).json()
    assert client.delete(API + '/evaluations/datasets/' + custom['id']).status_code == 200
    assert client.get(API + f'/evaluations/runs/{r["id"]}').json() == r
    assert client.delete(API + '/evaluations/datasets/demo-v1').status_code == 409
    assert (
        client.put(
            API + '/evaluations/datasets/demo-v1', json={'name': 'bad', 'cases': demo['cases']}
        ).status_code
        == 409
    )


def test_metric_macro_positive_only_and_graded_ndcg():
    from app.evaluation import case_metrics, aggregate_metrics

    # Relevant at ranks 2 and 3. grade 3 at rank 3, grade 1 at rank 2.
    m = case_metrics(
        [{'key': 'x'}, {'key': 'a'}, {'key': 'b'}],
        [{'key': 'a', 'grade': 1}, {'key': 'b', 'grade': 3}, {'key': 'c', 'grade': 2}],
        3,
    )
    assert m['hit_rate'] == 1 and m['recall'] == pytest.approx(2 / 3)
    assert m['mrr'] == 0.5
    assert m['ndcg'] == pytest.approx((1 / math.log2(3) + 7 / 2) / (7 + 3 / math.log2(3) + 1 / 2))
    empty = case_metrics([], [], 3)
    missed = case_metrics([], [{'key': 'a', 'grade': 3}], 3)
    total = aggregate_metrics([m, empty, missed])
    assert total['positive_cases'] == 2 and total['no_answer_cases'] == 1
    assert total['hit_rate'] == 0.5 and total['recall'] == pytest.approx(1 / 3)
    assert total['mrr'] == 0.25 and total['no_answer_accuracy'] == 1
    assert empty['recall'] is None


@pytest.mark.parametrize(
    'mutation',
    ['bool', 'float', 'zero', 'duplicate_key', 'duplicate_case', 'long_query', 'no_cases'],
)
def test_dataset_validation(client, mutation):
    body = {
        'name': 'bad',
        'cases': [
            {
                'id': 'a',
                'query': '打磨',
                'category': 'x',
                'judgments': [{'key': 'technique:1', 'grade': 3}],
            }
        ],
    }
    case = body['cases'][0]
    if mutation in ('bool', 'float', 'zero'):
        case['judgments'][0]['grade'] = {'bool': True, 'float': 1.5, 'zero': 0}[mutation]
    if mutation == 'duplicate_key':
        case['judgments'] *= 2
    if mutation == 'duplicate_case':
        body['cases'] *= 2
    if mutation == 'long_query':
        case['query'] = '中' * 2001
    if mutation == 'no_cases':
        body['cases'] = []
    assert client.post(API + '/evaluations/datasets', json=body).status_code == 422


def test_import_byte_limit_and_missing_judgment(client):
    body = {'name': 'oversized', 'description': '中' * 400000, 'cases': []}
    assert (
        client.post(
            API + '/evaluations/datasets',
            content=json.dumps(body, ensure_ascii=False).encode(),
            headers={'content-type': 'application/json'},
        ).status_code
        == 413
    )
    d = dataset(client, [{'key': 'technique:999', 'grade': 3}])
    r = client.post(API + '/evaluations/runs', json={'dataset_id': d['id']})
    assert r.status_code == 422 and 'technique:999' in r.text
    assert '预期资料不存在或已不可用' in r.json()['detail']
    assert '请修改标注后重试' in r.json()['detail']
    assert client.get(API + '/evaluations/runs').json() == []


def test_preview_live_source_withdrawal_and_immutable_report(client):
    s = client.post(API + '/sources', json={'title': 'manual'}).json()['id']
    t = client.post(
        API + '/techniques',
        json={
            'title': '打磨',
            'content_blocks': [{'type': 'paragraph', 'text': t} for t in ['先用砂纸去除痕迹']],
            'source_ids': [s],
        },
    ).json()['id']
    d = dataset(client, [{'key': f'technique:{t}', 'grade': 3}])
    r = client.post(API + '/evaluations/runs', json={'dataset_id': d['id']}).json()
    assert r['corpus_size'] == 1
    assert (
        client.get(API + '/retrieval/preview', params={'q': '打磨'}).json()['ranked'][0]['key']
        == f'technique:{t}'
    )
    client.delete(API + f'/sources/{s}')
    assert client.get(API + '/retrieval/preview', params={'q': '打磨'}).json()['ranked'] == []
    assert client.post(API + '/evaluations/runs', json={'dataset_id': d['id']}).status_code == 422
    assert client.get(API + f'/evaluations/runs/{r["id"]}').json() == r


def test_v3_backup_and_old_backup_preservation_and_atomic_validation(client):
    d = dataset(client)
    r = client.post(API + '/evaluations/runs', json={'dataset_id': d['id']}).json()
    data = manifest(client)
    assert data['version'] == 6
    for version in [1, 2, 3, 4, 5, 6]:
        restored = copy.deepcopy(data)
        restored['version'] = version
        if version < 3:
            restored.pop('evaluation_datasets')
            restored.pop('evaluation_runs')
        result = client.post(
            API + '/backup', files={'file': ('backup.zip', archive(restored), 'application/zip')}
        )
        assert result.status_code == 200, result.text
        assert client.get(API + f'/evaluations/runs/{r["id"]}').json() == r
    for corrupt in ['dataset', 'report']:
        broken = copy.deepcopy(data)
        if corrupt == 'dataset':
            broken['evaluation_datasets'][0]['cases'][0]['judgments'] = [
                {'key': 'x', 'grade': True}
            ]
        else:
            broken['evaluation_runs'][0]['metrics']['ranked']['recall'] = 9
        result = client.post(
            API + '/backup', files={'file': ('bad.zip', archive(broken), 'application/zip')}
        )
        assert result.status_code == 400, result.text
        assert client.get(API + f'/evaluations/runs/{r["id"]}').json() == r


def test_demo_export_cannot_be_imported_as_live_ids(client):
    demo = client.get(API + '/evaluations/datasets/demo-v1').json()
    result = client.post(API + '/evaluations/datasets', json=demo)
    assert result.status_code == 422
    assert 'demo' in result.text.lower()


def test_updated_dataset_changes_hash_without_changing_saved_report(client):
    d = dataset(client)
    a = client.post(API + '/evaluations/runs', json={'dataset_id': d['id']}).json()
    changed = {
        **d,
        'cases': [{'id': 'two', 'query': 'different', 'category': 'new', 'judgments': []}],
    }
    updated = client.put(API + '/evaluations/datasets/' + d['id'], json=changed)
    assert updated.status_code == 200, updated.text
    b = client.post(API + '/evaluations/runs', json={'dataset_id': d['id']}).json()
    assert a['dataset_hash'] != b['dataset_hash']
    assert client.get(API + f'/evaluations/runs/{a["id"]}').json() == a
    assert client.get(API + '/evaluations/runs').json()[0]['id'] == b['id']


@pytest.mark.parametrize('k', [0, 21, True, 1.5])
def test_run_k_is_strictly_bounded(client, k):
    assert (
        client.post(API + '/evaluations/runs', json={'dataset_id': 'demo-v1', 'k': k}).status_code
        == 422
    )


def test_legacy_import_invalid_labels_do_not_replace_existing_dataset(client):
    d = dataset(client)
    bad = copy.deepcopy(d)
    bad['cases'][0]['judgments'] = [{'key': 'product:1', 'grade': False}]
    assert client.put(API + '/evaluations/datasets/' + d['id'], json=bad).status_code == 422
    assert client.get(API + '/evaluations/datasets/' + d['id']).json() == d


def test_dataset_hash_is_reproducible_from_reported_case_snapshot(client):
    from hashlib import sha256

    d = dataset(client)
    r = client.post(API + '/evaluations/runs', json={'dataset_id': d['id']}).json()
    snapshot = [
        {k: case[k] for k in ('id', 'query', 'category', 'judgments')} for case in r['cases']
    ]
    expected = sha256(
        json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()
    ).hexdigest()
    assert r['dataset_hash'] == expected


def test_backup_into_fresh_database_restores_demo_and_custom_reports(client, tmp_path):
    custom = dataset(client)
    a = client.post(API + '/evaluations/runs', json={'dataset_id': custom['id']}).json()
    b = client.post(API + '/evaluations/runs', json={'dataset_id': 'demo-v1'}).json()
    raw = client.get(API + '/backup').content
    url = test_database_url(f'{tmp_path / "fresh.db"}')
    directory = tmp_path / 'fresh-uploads'
    with TestClient(create_app(url, directory)) as other:
        restored = other.post(
            API + '/backup', files={'file': ('backup.zip', raw, 'application/zip')}
        )
        assert restored.status_code == 200, restored.text
        assert other.get(API + f'/evaluations/runs/{a["id"]}').json() == a
        assert other.get(API + f'/evaluations/runs/{b["id"]}').json() == b
        assert other.get(API + '/evaluations/datasets/' + custom['id']).json() == custom
    with TestClient(create_app(url, directory)) as reopened:
        assert reopened.get(API + f'/evaluations/runs/{b["id"]}').json() == b
        next_run = reopened.post(
            API + '/evaluations/runs', json={'dataset_id': custom['id']}
        ).json()
        assert next_run['id'] > b['id']


def test_missing_judgments_are_not_silently_treated_as_negative(client):
    payload = {'name': 'invalid', 'cases': [{'id': 'a', 'query': 'q', 'category': 'x'}]}
    assert client.post(API + '/evaluations/datasets', json=payload).status_code == 422


def test_case_count_limit_and_duplicate_backup_report_are_atomic(client):
    payload = {
        'name': 'invalid',
        'cases': [
            {'id': str(i), 'query': 'q', 'category': 'x', 'judgments': []} for i in range(201)
        ],
    }
    assert client.post(API + '/evaluations/datasets', json=payload).status_code == 422
    custom = dataset(client)
    run = client.post(API + '/evaluations/runs', json={'dataset_id': custom['id']}).json()
    data = manifest(client)
    data['evaluation_runs'] *= 2
    response = client.post(
        API + '/backup', files={'file': ('backup.zip', archive(data), 'application/zip')}
    )
    assert response.status_code == 400
    assert client.get(API + '/evaluations/runs').json()[0]['id'] == run['id']
    assert len(client.get(API + '/evaluations/runs').json()) == 1


def test_restore_rejects_changed_case_snapshot_hash(client):
    d = dataset(client)
    run = client.post(API + '/evaluations/runs', json={'dataset_id': d['id']}).json()
    data = manifest(client)
    data['evaluation_runs'][0]['cases'][0]['query'] = 'tampered question'
    response = client.post(
        API + '/backup', files={'file': ('backup.zip', archive(data), 'application/zip')}
    )
    assert response.status_code == 400
    assert client.get(API + f'/evaluations/runs/{run["id"]}').json() == run


def test_near_byte_limit_dataset_can_restore_its_own_backup(client):
    # Compact UTF-8 input stays below 1 MiB; backup metadata/formatting makes it larger.
    body = {
        'name': 'boundary',
        'cases': [
            {'id': str(i), 'query': '中' * 1700, 'category': 'x', 'judgments': []}
            for i in range(200)
        ],
    }

    def encoded():
        return json.dumps(body, ensure_ascii=False, separators=(',', ':')).encode('utf-8')

    remaining = (1024 * 1024 - len(encoded()) - 100) // 3
    for case in body['cases']:
        extra = min(300, remaining)
        case['query'] += '中' * extra
        remaining -= extra
    raw = encoded()
    assert 1024 * 1024 - 105 <= len(raw) <= 1024 * 1024
    created = client.post(
        API + '/evaluations/datasets', content=raw, headers={'content-type': 'application/json'}
    )
    assert created.status_code == 201, created.text
    backup = client.get(API + '/backup')
    assert backup.status_code == 200
    restored = client.post(
        API + '/backup', files={'file': ('backup.zip', backup.content, 'application/zip')}
    )
    assert restored.status_code == 200, restored.text
    assert (
        client.get(API + '/evaluations/datasets/' + created.json()['id']).json() == created.json()
    )
