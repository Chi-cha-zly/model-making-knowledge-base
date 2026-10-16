"""CLI consent gates and persistent workflow integration; only synthetic inputs."""

import importlib
import json
from pathlib import Path

import httpx
import pytest

from nmm_fakes import make_inputs

SETTINGS = {
    'VISION_BASE_URL': 'https://dashscope.aliyuncs.com/compatible-mode/v1',
    'VISION_MODEL': 'qwen3-vl-plus',
    'VISION_API_KEY': 'synthetic-vision',
    'IMAGE_BASE_URL': 'https://api.openai.com/v1',
    'IMAGE_MODEL': 'gpt-image-2.5-sunburst',
    'IMAGE_API_KEY': 'synthetic-image',
}


@pytest.fixture
def inputs(tmp_path):
    result = []
    for name, image in make_inputs().items():
        path = tmp_path / f'{name}.png'
        path.write_bytes(image.data)
        result.extend([f'--{name}', str(path)])
    return result


def cli():
    return importlib.import_module('scripts.nmm_live_check')


def deny_request(request):
    pytest.fail('dry-run or rejected input sent a model request')


def test_dry_run_never_connects_database_writes_or_sends(inputs, tmp_path, monkeypatch, capsys):
    def forbidden(*args, **kwargs):
        pytest.fail('dry-run connected a database')

    monkeypatch.setattr('app.db.create_db_engine', forbidden)
    monkeypatch.setattr('app.runtime_config.read_settings', forbidden)
    output = tmp_path / 'output'
    before = set(tmp_path.rglob('*'))
    assert (
        cli().main(
            inputs + ['--output', str(output)],
            environ=SETTINGS,
            transport=httpx.MockTransport(deny_request),
        )
        == 0
    )
    result = json.loads(capsys.readouterr().out)
    assert result['status'] == 'dry_run'
    assert result['expected_image_calls'] == 1 and result['expected_vision_calls'] == 1
    assert result['estimated_cost'] is None
    assert 'synthetic-' not in json.dumps(result) and str(tmp_path) not in json.dumps(result)
    assert set(tmp_path.rglob('*')) == before


@pytest.mark.parametrize(
    'flags',
    [
        [],
        ['--i-understand-cost'],
        ['--max-calls', '2'],
        ['--i-understand-cost', '--max-calls', '1'],
        ['--i-understand-cost', '--max-calls', '3'],
    ],
)
def test_apply_requires_both_consent_and_exact_call_budget(inputs, tmp_path, flags, capsys):
    result = cli().main(
        inputs
        + [
            '--apply',
            '--database-url',
            'postgresql://redacted',
            '--output',
            str(tmp_path / 'output'),
        ]
        + flags,
        environ=SETTINGS,
        transport=httpx.MockTransport(deny_request),
    )
    assert result == 2
    assert not (tmp_path / 'output').exists()
    assert 'redacted' not in capsys.readouterr().out


@pytest.mark.parametrize('kind', ['directory', 'file', 'symlink'])
def test_apply_never_overwrites_existing_output(inputs, tmp_path, kind, capsys):
    output = tmp_path / 'existing'
    if kind == 'directory':
        output.mkdir()
    elif kind == 'file':
        output.write_text('keep')
    else:
        output.symlink_to(tmp_path / 'absent')
    assert (
        cli().main(
            inputs
            + [
                '--apply',
                '--i-understand-cost',
                '--max-calls',
                '2',
                '--database-url',
                'postgresql://redacted',
                '--output',
                str(output),
            ],
            environ=SETTINGS,
            transport=httpx.MockTransport(deny_request),
        )
        == 2
    )
    assert output.is_symlink() if kind == 'symlink' else output.exists()
    if kind == 'file':
        assert output.read_text() == 'keep'
    assert 'redacted' not in capsys.readouterr().out


def test_apply_missing_image_key_fails_before_database_or_files(
    inputs, tmp_path, monkeypatch, capsys
):
    def forbidden(*args, **kwargs):
        pytest.fail('invalid provider configuration connected a database')

    monkeypatch.setattr('app.db.create_db_engine', forbidden)
    settings = {k: v for k, v in SETTINGS.items() if k != 'IMAGE_API_KEY'}
    assert (
        cli().main(
            inputs
            + [
                '--apply',
                '--i-understand-cost',
                '--max-calls',
                '2',
                '--database-url',
                'postgresql://redacted',
                '--output',
                str(tmp_path / 'out'),
            ],
            environ=settings,
            transport=httpx.MockTransport(deny_request),
        )
        == 2
    )
    assert not (tmp_path / 'out').exists()
    assert '图像编辑未配置或配置不受支持' in json.loads(capsys.readouterr().out)['error']


def test_dry_run_rejects_bad_mask_without_persistence(inputs, tmp_path, capsys):
    Path(inputs[3]).write_bytes(b'bad')
    assert cli().main(inputs, environ=SETTINGS, transport=httpx.MockTransport(deny_request)) == 2
    result = json.loads(capsys.readouterr().out)
    assert result['status'] == 'failed'
    assert str(tmp_path) not in json.dumps(result)


@pytest.fixture
def live_stubs(monkeypatch):
    """Explicit live-mode stubs exercise production reservations without any HTTP."""
    from app.nmm.provider_types import CallUsage, ProviderResult
    from nmm_fakes import OfflineVision, RecordingImageEdit

    class Vision(OfflineVision):
        mode = 'live'
        public_config = {'provider': 'test', 'model': 'synthetic-vision'}
        configuration_id = 'v' * 64

        async def analyze(self, images, prompt, schema):
            result = await super().analyze(images, prompt, schema)
            return ProviderResult(
                result,
                CallUsage(
                    'test',
                    'synthetic-vision',
                    input_tokens=10,
                    output_tokens=20,
                    total_tokens=30,
                    input_images=len(images),
                ),
            )

    class Editor(RecordingImageEdit):
        mode = 'live'
        public_config = {'provider': 'test', 'model': 'synthetic-image'}
        configuration_id = 'i' * 64

        async def edit(self, image, mask, references, prompt, n):
            result = await super().edit(image, mask, references, prompt, n)
            return ProviderResult(
                result,
                CallUsage(
                    'test', 'synthetic-image', input_images=1 + len(references), output_images=n
                ),
            )

    vision, editor = Vision(), Editor()
    monkeypatch.setattr(
        'app.nmm.provider_config.build_nmm_providers', lambda settings, **kwargs: (vision, editor)
    )
    return vision, editor


def apply_args(inputs, url, output):
    return inputs + [
        '--apply',
        '--i-understand-cost',
        '--max-calls',
        '2',
        '--database-url',
        url,
        '--output',
        str(output),
    ]


def test_apply_runs_same_persistent_ledger_and_stops_before_selection(
    inputs, tmp_path, live_stubs, capsys
):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    from app.nmm.models import NmmProject
    from pg_test_support import test_database_url
    from uuid import UUID

    url = test_database_url()
    output = tmp_path / 'check'
    assert cli().main(apply_args(inputs, url, output), environ={}) == 0
    report = json.loads(capsys.readouterr().out)
    assert report['status'] == 'awaiting_confirmation'
    assert [task for task, _ in live_stubs[0].calls] == ['analysis']
    assert live_stubs[1].calls == [0]
    assert len(report['meta']['usage']['calls']) == 2
    assert report['meta']['usage']['reserved_image_calls'] == 1
    assert report['meta']['usage']['reserved_vision_calls'] == 1
    assert report['meta']['authorization']['remaining_image_calls'] == 0
    assert report['meta']['authorization']['remaining_vision_calls'] == 0
    assert report['estimated_cost'] is None
    assert report['candidates'] and (output / report['candidates'][0]['path']).is_file()
    assert not Path(report['candidates'][0]['path']).is_absolute()
    assert json.loads((output / 'report.json').read_text()) == report
    assert str(tmp_path) not in json.dumps(report) and url not in json.dumps(report)
    engine = create_engine(url)
    try:
        with Session(engine) as db:
            row = db.get(NmmProject, UUID(report['project_id']))
            assert row.selected_candidate_id is None
            assert len(row.meta['accounting']['calls']) == 2
            assert row.generation_count == 1
    finally:
        engine.dispose()
    assert cli().main(apply_args(inputs, url, output), environ={}) == 2
    assert live_stubs[1].calls == [0]


def test_apply_failure_retains_usage_without_retry(inputs, tmp_path, live_stubs, capsys):
    from pg_test_support import test_database_url

    live_stubs[1].fail = True
    output = tmp_path / 'failed-check'
    assert cli().main(apply_args(inputs, test_database_url(), output), environ={}) == 2
    report = json.loads(capsys.readouterr().out)
    assert report['status'] == 'failed' and report['project_id']
    assert len(report['meta']['usage']['calls']) == 2
    assert live_stubs[1].calls == [0]
    assert 'secret-image-error' not in json.dumps(report)
    assert (output / 'report.json').exists()


def test_unmigrated_database_rejected_before_output_or_calls(inputs, tmp_path, live_stubs):
    from sqlalchemy import create_engine, text
    from pg_test_support import test_database_url

    url = test_database_url()
    engine = create_engine(url)
    try:
        with engine.begin() as connection:
            connection.execute(text('DELETE FROM alembic_version'))
    finally:
        engine.dispose()
    output = tmp_path / 'unmigrated'
    assert cli().main(apply_args(inputs, url, output), environ={}) == 2
    assert not output.exists()
    assert live_stubs[0].calls == [] and live_stubs[1].calls == []
