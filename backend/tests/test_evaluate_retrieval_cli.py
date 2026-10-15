"""File comparisons are explicit, bounded, and never mutate the source manifest."""

import json
import pytest
from test_hybrid_retrieval import seed, QueryProvider
from pg_test_support import test_database_url


def inputs(tmp_path, key):
    path = tmp_path / 'dataset.json'
    value = {
        'name': 'Frozen cases',
        'label_origin': 'assistant draft, reviewed',
        'cases': [
            {
                'id': 'one',
                'query': '表面反射光亮',
                'group': 'paraphrase',
                'judgments': [{'key': key, 'relevance': 3}],
            }
        ],
    }
    path.write_text(json.dumps(value), encoding='utf-8')
    return path


def test_cli_dry_run_does_not_construct_provider_or_connect(tmp_path, monkeypatch):
    from scripts import evaluate_retrieval as cli

    path = inputs(tmp_path, 'technique:1')
    before = path.read_bytes()
    monkeypatch.setattr(cli, 'LangChainProvider', lambda **kw: pytest.fail('unexpected provider'))
    monkeypatch.setattr(cli, 'create_db_engine', lambda url: pytest.fail('unexpected DB'))
    report = tmp_path / 'result.json'
    assert (
        cli.main(
            [
                '--database-url',
                'postgresql+psycopg://unused/test',
                '--dataset',
                str(path),
                '--report',
                str(report),
                '--storage-dir',
                str(tmp_path / 'uploads'),
            ],
            environ={},
        )
        == 0
    )
    result = json.loads(report.read_text())
    assert result['status'] == 'dry_run' and result['query_budget'] == 1
    assert path.read_bytes() == before


def test_cli_apply_maps_frozen_labels_and_preserves_input(tmp_path):
    from scripts import evaluate_retrieval as cli

    engine, ids = seed()
    engine.dispose()
    path = inputs(tmp_path, f'technique:{ids[0]}')
    before = path.read_bytes()
    report = tmp_path / 'result.json'
    provider = QueryProvider()
    assert (
        cli.main(
            [
                '--database-url',
                test_database_url(),
                '--dataset',
                str(path),
                '--report',
                str(report),
                '--storage-dir',
                str(tmp_path / 'uploads'),
                '--apply',
            ],
            environ={},
            provider=provider,
        )
        == 0
    )
    result = json.loads(report.read_text())
    assert result['comparison']['report_version'] == 2
    assert result['comparison']['cases'][0]['judgments'][0]['grade'] == 3
    assert result['comparison']['metrics']['hybrid']['hit_rate'] == 1
    assert len(provider.calls) == 1 and path.read_bytes() == before


@pytest.mark.parametrize('invalid', ['input', 'storage', 'budget'])
def test_cli_rejects_dangerous_output_or_oversized_budget_before_provider(
    tmp_path, monkeypatch, invalid
):
    from scripts import evaluate_retrieval as cli

    path = inputs(tmp_path, 'technique:1')
    storage = tmp_path / 'uploads'
    report = (
        path
        if invalid == 'input'
        else storage / 'report.json'
        if invalid == 'storage'
        else tmp_path / 'report.json'
    )
    monkeypatch.setattr(cli, 'LangChainProvider', lambda **kw: pytest.fail('unexpected provider'))
    args = [
        '--database-url',
        'postgresql+psycopg://unused/test',
        '--dataset',
        str(path),
        '--report',
        str(report),
        '--storage-dir',
        str(storage),
        '--apply',
    ]
    if invalid == 'budget':
        value = json.loads(path.read_text())
        value['cases'] = [{**value['cases'][0], 'id': str(i)} for i in range(21)]
        path.write_text(json.dumps(value))
    assert cli.main(args, environ={}) == 2
