"""Synthetic offline evaluation: honest intermediate metrics and safe publication."""

import importlib
import json
import os
from pathlib import Path
import subprocess
import sys

import numpy as np
from PIL import Image
import pytest


def evaluation():
    return importlib.import_module('app.nmm.evaluation')


def test_metrics_ignore_background_and_use_shared_final_palette():
    real = np.array([[[0, 0, 0], [255, 255, 255], [255, 0, 0]]], dtype=np.uint8)
    generated = np.array([[[0, 0, 0], [0, 0, 0], [0, 255, 0]]], dtype=np.uint8)
    mask = np.array([[True, True, False]])
    result = evaluation().compare_frames(real, generated, mask, [[0, 0, 0], [100, 0, 0]])
    assert result['mean_delta_e2000'] == pytest.approx(50, abs=0.001)
    assert result['reference_area_ratios'] == [0.5, 0.5]
    assert result['generated_area_ratios'] == [1.0, 0.0]
    assert result['area_ratio_signed_difference'] == [0.5, -0.5]
    assert result['mean_absolute_area_ratio_difference'] == 0.5
    identical = evaluation().compare_frames(real, real, mask, [[0, 0, 0], [100, 0, 0]])
    assert identical['mean_delta_e2000'] == 0
    assert identical['mean_absolute_area_ratio_difference'] == 0


def test_mapping_uses_normalized_progress_and_never_scores_final():
    pairs = evaluation().match_intermediates(5, 3)
    assert [(p['reference_step'], p['generated_step']) for p in pairs] == [
        (1, 1),
        (2, 1),
        (3, 2),
        (4, 2),
    ]
    assert [p['reference_progress'] for p in pairs] == [0, 0.25, 0.5, 0.75]
    assert [p['generated_progress'] for p in pairs] == [0, 0, 0.5, 0.5]
    assert [p['progress_distance'] for p in pairs] == [0, 0.25, 0, 0.25]


@pytest.mark.parametrize('bad', ['dimensions', 'empty_mask', 'nonfinite_palette'])
def test_metric_rejects_invalid_inputs(bad):
    real = np.zeros((2, 2, 3), dtype=np.uint8)
    other = real.copy()
    mask = np.ones((2, 2), dtype=bool)
    palette = [[0, 0, 0]]
    if bad == 'dimensions':
        other = other[:1]
    elif bad == 'empty_mask':
        mask[:] = False
    else:
        palette = [[float('nan'), 0, 0]]
    with pytest.raises(ValueError):
        evaluation().compare_frames(real, other, mask, palette)


@pytest.fixture
def case(tmp_path):
    final = np.zeros((8, 12, 3), dtype=np.uint8)
    final[:, :6] = (30, 30, 30)
    final[:, 6:] = (220, 220, 220)
    for name, pixels in [
        ('first', np.full_like(final, 130)),
        ('final', final),
        ('original', np.full_like(final, 50)),
    ]:
        Image.fromarray(pixels).save(tmp_path / f'{name}.png')
    Image.new('L', (12, 8), 255).save(tmp_path / 'mask.png')
    config = {
        'groups': [
            {
                'label': 'synthetic',
                'steps': ['first.png', 'final.png'],
                'mask': 'mask.png',
                'original': 'original.png',
                'step_count': 4,
                'render_params': {'transition_width': 0, 'detail_strength': 0},
            }
        ]
    }
    path = tmp_path / 'config.json'
    path.write_text(json.dumps(config))
    return tmp_path, path, config


def args(case, *, apply=True):
    directory, config, _ = case
    return ['--config', str(config), '--output', str(directory / 'out')] + (
        ['--apply'] if apply else []
    )


def cli():
    return importlib.import_module('scripts.nmm_eval')


def test_dry_run_validates_without_rendering_or_creating_output(case, monkeypatch, capsys):
    def forbidden(*a, **k):
        pytest.fail('dry-run must not render')

    monkeypatch.setattr('app.nmm.render.render_steps', forbidden)
    assert cli().main(args(case, apply=False)) == 0
    report = json.loads(capsys.readouterr().out)
    assert report['mode'] == 'dry_run'
    assert report['groups'][0]['dimensions'] == [12, 8]
    assert not (case[0] / 'out').exists()
    assert not list(case[0].glob('.nmm-eval-*'))


def test_apply_scores_intermediate_only_and_writes_comparison(case, capsys):
    before = {p.name: p.read_bytes() for p in case[0].glob('*.png')}
    assert cli().main(args(case)) == 0
    capsys.readouterr()
    report = json.loads((case[0] / 'out/result.json').read_text())
    group = report['groups'][0]
    assert group['original_policy'] == 'provided'
    assert group['actual_step_count'] == 4
    assert group['final_check']['mean_delta_e2000'] == 0
    assert group['intermediate_summary']['pair_count'] == 1
    assert group['intermediate_summary']['mean_delta_e2000'] > 20
    assert group['pairs'][0]['generated_step'] == 1
    assert group['pairs'][0]['reference_step'] == 1
    assert (
        group['pairs'][0]['mean_delta_e2000'] == group['intermediate_summary']['mean_delta_e2000']
    )
    assert len(group['input_sha256']['steps'][0]) == 64
    assert str(case[0]) not in (case[0] / 'out/result.json').read_text()
    with Image.open(case[0] / 'out' / group['comparison']) as image:
        assert image.width == 512 and image.height == 512
    assert {p.name: p.read_bytes() for p in case[0].glob('*.png')} == before
    assert set(p.name for p in (case[0] / 'out').iterdir()) == {'result.json', 'group-01.png'}


def test_missing_original_disables_detail_and_reduced_steps_are_mapped(case, capsys):
    group = case[2]['groups'][0]
    del group['original']
    group['step_count'] = 9
    group['render_params']['detail_strength'] = 1.5
    group['steps'] = ['first.png'] * 5 + ['final.png']
    case[1].write_text(json.dumps(case[2]))
    assert cli().main(args(case)) == 0
    capsys.readouterr()
    result = json.loads((case[0] / 'out/result.json').read_text())['groups'][0]
    assert result['original_policy'] == 'missing_disable_detail'
    assert result['params']['detail_strength'] == 0
    assert result['requested_params']['detail_strength'] == 1.5
    assert result['input_sha256']['original'] is None
    assert result['actual_step_count'] == 4
    assert len(result['pairs']) == 5
    assert max(p['generated_step'] for p in result['pairs']) < 4
    assert result['repeated_generated_steps']
    assert any('original' in warning.lower() for warning in result['warnings'])


@pytest.mark.parametrize(
    'problem',
    [
        'dimensions',
        'empty_mask',
        'grey_mask',
        'transparent',
        'unknown_field',
        'bad_params',
        'too_many_steps',
        'one_step',
    ],
)
def test_invalid_inputs_fail_without_output(case, problem, capsys):
    if problem == 'dimensions':
        Image.new('RGB', (6, 8)).save(case[0] / 'first.png')
    elif problem == 'empty_mask':
        Image.new('L', (12, 8), 0).save(case[0] / 'mask.png')
    elif problem == 'grey_mask':
        Image.new('L', (12, 8), 120).save(case[0] / 'mask.png')
    elif problem == 'transparent':
        Image.new('RGBA', (12, 8), (0, 0, 0, 254)).save(case[0] / 'first.png')
    else:
        group = case[2]['groups'][0]
        if problem == 'unknown_field':
            group['typo'] = 1
        elif problem == 'bad_params':
            group['render_params']['seed'] = -1
        else:
            group['steps'] = ['first.png'] * (31 if problem == 'too_many_steps' else 1)
        case[1].write_text(json.dumps(case[2]))
    assert cli().main(args(case)) == 1
    assert capsys.readouterr().err
    assert not (case[0] / 'out').exists()
    assert not list(case[0].glob('.nmm-eval-*'))


@pytest.mark.parametrize('kind', ['directory', 'symlink', 'input'])
def test_output_never_replaces_existing_entry(case, kind, capsys):
    output = case[0] / 'out'
    if kind == 'directory':
        output.mkdir()
    elif kind == 'symlink':
        output.symlink_to('missing')
    else:
        output.write_bytes((case[0] / 'first.png').read_bytes())
    before = output.lstat().st_ino
    assert cli().main(args(case)) == 1
    assert output.lstat().st_ino == before
    assert not list(case[0].glob('.nmm-eval-*'))


def test_disk_failure_cleans_staging_and_does_not_publish(case, monkeypatch, capsys):
    module = cli()

    def fail(*a, **k):
        raise OSError('synthetic disk failure')

    monkeypatch.setattr(module, '_write_json', fail)
    assert module.main(args(case)) == 1
    assert 'synthetic disk failure' in capsys.readouterr().err
    assert not (case[0] / 'out').exists()
    assert not list(case[0].glob('.nmm-eval-*'))


def test_subprocess_dry_run_requires_no_settings_or_database(case):
    env = {k: os.environ[k] for k in ('PATH', 'LANG', 'TMPDIR') if k in os.environ}
    completed = subprocess.run(
        [sys.executable, '-m', 'scripts.nmm_eval', *args(case, apply=False)],
        cwd=Path(__file__).resolve().parents[1],
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout)['mode'] == 'dry_run'
    assert not (case[0] / 'out').exists()


def test_metrics_include_every_chunk_and_partial_tail():
    real = np.zeros((1, 16385, 3), dtype=np.uint8)
    generated = real.copy()
    generated[0, -1] = 255
    result = evaluation().compare_frames(
        real, generated, np.ones((1, 16385), dtype=bool), [[0, 0, 0], [100, 0, 0]]
    )
    assert result['mean_delta_e2000'] == pytest.approx(100 / 16385, abs=1e-7)
    assert result['generated_area_ratios'][1] == 1 / 16385


@pytest.mark.parametrize(
    'problem',
    [
        'pixels',
        'image_bytes',
        'config_bytes',
        'total_bytes',
        'groups',
        'duplicate_key',
        'nan',
        'original_size',
    ],
)
def test_input_limits_fail_before_publication(case, monkeypatch, problem, capsys):
    module = cli()
    if problem == 'pixels':
        Image.new('RGB', (2001, 2000)).save(case[0] / 'first.png')
    elif problem == 'image_bytes':
        with (case[0] / 'first.png').open('ab') as file:
            file.truncate(20 * 1024 * 1024 + 1)
    elif problem == 'config_bytes':
        case[1].write_bytes(b' ' * (1024 * 1024 + 1))
    elif problem == 'total_bytes':
        monkeypatch.setattr(module, 'MAX_INPUT_BYTES', 1)
    elif problem == 'groups':
        case[1].write_text(json.dumps({'groups': case[2]['groups'] * 13}))
    elif problem == 'duplicate_key':
        case[1].write_text('{"groups": [], "groups": []}')
    elif problem == 'nan':
        case[2]['groups'][0]['render_params']['detail_strength'] = float('nan')
        case[1].write_text(json.dumps(case[2]))
    else:
        Image.new('RGB', (11, 8)).save(case[0] / 'original.png')
    assert module.main(args(case)) == 1
    assert capsys.readouterr().err
    assert not (case[0] / 'out').exists()
    assert not list(case[0].glob('.nmm-eval-*'))


def test_input_change_after_validation_does_not_publish(case, monkeypatch, capsys):
    module = cli()
    evaluate = module._evaluate

    def mutate(group):
        Image.new('RGB', (12, 8), 'red').save(case[0] / 'first.png')
        return evaluate(group)

    monkeypatch.setattr(module, '_evaluate', mutate)
    assert module.main(args(case)) == 1
    assert 'changed after validation' in capsys.readouterr().err
    assert not (case[0] / 'out').exists()
    assert not list(case[0].glob('.nmm-eval-*'))


@pytest.mark.parametrize('competitor', ['directory', 'symlink'])
def test_atomic_publish_preserves_racing_destination(case, monkeypatch, competitor, capsys):
    module = cli()
    publish = module._publish_directory
    inode = None

    def race(parent_fd, staging_name, output_name):
        nonlocal inode
        if competitor == 'directory':
            os.mkdir(output_name, dir_fd=parent_fd)
        else:
            os.symlink('original.png', output_name, dir_fd=parent_fd)
        inode = os.stat(output_name, dir_fd=parent_fd, follow_symlinks=False).st_ino
        return publish(parent_fd, staging_name, output_name)

    monkeypatch.setattr(module, '_publish_directory', race)
    original = (case[0] / 'original.png').read_bytes()
    assert module.main(args(case)) == 1
    assert (case[0] / 'out').lstat().st_ino == inode
    assert (case[0] / 'original.png').read_bytes() == original
    assert not list(case[0].glob('.nmm-eval-*'))


def test_staging_collision_is_not_cleanup_owned(case, monkeypatch):
    from uuid import UUID

    module = cli()
    value = UUID('33333333-3333-4333-8333-333333333333')
    existing = case[0] / f'.nmm-eval-{value.hex}'
    existing.mkdir()
    sentinel = existing / 'keep'
    sentinel.write_text('existing')
    monkeypatch.setattr(module, 'uuid4', lambda: value)
    assert module.main(args(case)) == 1
    assert sentinel.read_text() == 'existing'
    assert not (case[0] / 'out').exists()


def test_output_budget_failure_cleans_complete_staged_images(case, monkeypatch):
    monkeypatch.setattr(cli(), 'MAX_OUTPUT_BYTES', 1)
    assert cli().main(args(case)) == 1
    assert not (case[0] / 'out').exists()
    assert not list(case[0].glob('.nmm-eval-*'))


def test_group_macro_average_does_not_weight_more_reference_steps(case, capsys):
    import copy

    second = copy.deepcopy(case[2]['groups'][0])
    Image.new('RGB', (12, 8), (30, 30, 30)).save(case[0] / 'exact.png')
    second['steps'] = ['exact.png'] * 3 + ['final.png']
    case[2]['groups'].append(second)
    case[1].write_text(json.dumps(case[2]))
    assert cli().main(args(case)) == 0
    capsys.readouterr()
    report = json.loads((case[0] / 'out/result.json').read_text())
    first, second = [g['intermediate_summary'] for g in report['groups']]
    assert first['pair_count'] == 1 and second['pair_count'] == 3
    assert report['intermediate_macro_average']['mean_delta_e2000'] == pytest.approx(
        (first['mean_delta_e2000'] + second['mean_delta_e2000']) / 2
    )
