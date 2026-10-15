"""Synthetic, offline CLI integration and publication failure boundaries."""

import importlib
import json
import os
from pathlib import Path
import subprocess
import sys

import numpy as np
from PIL import Image
import pytest

BACKEND = Path(__file__).resolve().parents[1]


@pytest.fixture
def inputs(tmp_path):
    original = np.full((12, 16, 4), (40, 60, 80, 255), dtype=np.uint8)
    final = np.zeros((12, 16, 3), dtype=np.uint8)
    final[:, :4] = (30, 35, 40)
    final[:, 4:8] = (90, 100, 110)
    final[:, 8:12] = (160, 170, 180)
    final[:, 12:] = (240, 245, 250)
    mask = np.zeros((12, 16), dtype=np.uint8)
    mask[1:-1, 1:-1] = 255
    for name, array in [('original', original), ('final', final), ('mask', mask)]:
        Image.fromarray(array).save(tmp_path / f'{name}.png')
    return tmp_path, original[:, :, :3], final, mask == 255


def arguments(directory, output=None):
    return [
        '--original',
        str(directory / 'original.png'),
        '--final',
        str(directory / 'final.png'),
        '--mask',
        str(directory / 'mask.png'),
        '--output',
        str(output or directory / 'preview'),
    ]


def invoke(args):
    # Supply no ambient database/model configuration; the module must be standalone.
    env = {
        name: os.environ[name]
        for name in ('PATH', 'LANG', 'LC_ALL', 'TMPDIR')
        if name in os.environ
    }
    return subprocess.run(
        [sys.executable, '-m', 'scripts.nmm_render_demo', *args],
        cwd=BACKEND,
        env=env,
        capture_output=True,
        text=True,
        timeout=45,
    )


def test_cli_writes_real_steps_diffs_contact_sheet_and_path_free_metadata(inputs):
    directory, original, final, mask = inputs
    before = {path.name: path.read_bytes() for path in directory.glob('*.png')}
    completed = invoke(
        arguments(directory)
        + [
            '--steps',
            '6',
            '--detail-strength',
            '0.4',
            '--detail-sigma',
            '1.5',
            '--erosion-radius',
            '0',
            '--transition-width',
            '1',
            '--diff-threshold',
            '3',
            '--seed',
            '17',
        ]
    )
    assert completed.returncode == 0, completed.stderr
    output = directory / 'preview'
    report_text = (output / 'result.json').read_text()
    report = json.loads(report_text)
    assert str(directory) not in report_text
    assert report['requested_step_count'] == 6
    assert report['actual_step_count'] == len(report['steps'])
    assert 3 <= len(report['steps']) <= 6
    assert report['renderer_version'] and report['palette']
    assert report['params'] == {
        'detail_strength': 0.4,
        'detail_sigma': 1.5,
        'erosion_radius': 0,
        'transition_width': 1,
        'diff_threshold': 3.0,
        'seed': 17,
        'max_samples': 32768,
    }
    for index, step in enumerate(report['steps'], 1):
        assert step['image'] == f'step-{index:02d}.png'
        assert step['diff_mask'] == f'diff-{index:02d}.png'
        assert step['kind'] in ('base', 'transition', 'final')
        assert 'palette_index' in step
        with Image.open(output / step['image']) as image:
            rendered = np.array(image)
        assert np.array_equal(rendered[~mask], original[~mask])
        with Image.open(output / step['diff_mask']) as diff:
            changed = np.array(diff)
        assert set(np.unique(changed)) <= {0, 255}
        assert not changed[~mask].any()
        assert step['area_ratio'] == pytest.approx(np.count_nonzero(changed) / mask.sum())
    expected = original.copy()
    expected[mask] = final[mask]
    assert np.array_equal(rendered, expected)
    with Image.open(output / 'contact-sheet.png') as contact:
        assert contact.width > 0 and contact.height > 0
    assert {path.name: path.read_bytes() for path in directory.glob('*.png')} == before
    assert not list(directory.glob('.nmm-render-*'))


@pytest.mark.parametrize('missing', ['--original', '--final', '--mask', '--output'])
def test_cli_requires_explicit_input_and_output_paths(inputs, missing):
    args = arguments(inputs[0])
    offset = args.index(missing)
    del args[offset : offset + 2]
    completed = invoke(args)
    assert completed.returncode != 0
    assert missing in completed.stderr
    assert not (inputs[0] / 'preview').exists()


@pytest.mark.parametrize(
    'problem',
    [
        'bad_image',
        'grey_mask',
        'wrong_mask_size',
        'transparent_original',
        'transparent_final',
        'too_large_final',
    ],
)
def test_cli_rejects_unsafe_images_without_output(inputs, problem):
    directory = inputs[0]
    if problem == 'bad_image':
        (directory / 'original.png').write_bytes(b'not an image')
    elif problem == 'grey_mask':
        Image.new('L', (16, 12), 128).save(directory / 'mask.png')
    elif problem == 'wrong_mask_size':
        Image.new('L', (3, 3), 255).save(directory / 'mask.png')
    elif problem.startswith('transparent_'):
        name = problem.removeprefix('transparent_')
        Image.new('RGBA', (16, 12), (40, 60, 80, 254)).save(directory / f'{name}.png')
    else:
        Image.new('RGB', (2001, 2000), 'grey').save(directory / 'final.png')
    completed = invoke(arguments(directory))
    assert completed.returncode != 0
    assert 'error' in completed.stderr.lower()
    assert not (directory / 'preview').exists()
    assert not list(directory.glob('.nmm-render-*'))


@pytest.mark.parametrize('existing', ['directory', 'file', 'symlink', 'input'])
def test_cli_refuses_existing_output_even_empty_directory_or_dangling_symlink(inputs, existing):
    directory = inputs[0]
    output = directory / 'preview'
    if existing == 'directory':
        output.mkdir()
    elif existing == 'file':
        output.write_text('keep')
    elif existing == 'symlink':
        output.symlink_to(directory / 'missing')
    else:
        output = directory / 'original.png'
    before = output.lstat()
    completed = invoke(arguments(directory, output))
    assert completed.returncode != 0
    assert 'exist' in completed.stderr.lower()
    assert output.lstat().st_ino == before.st_ino
    assert not list(directory.glob('.nmm-render-*'))


def test_write_failure_removes_staging_without_publishing(inputs, monkeypatch, capsys):
    module = importlib.import_module('scripts.nmm_render_demo')
    original = module._write_png
    calls = 0

    def fail(directory_fd, name, image):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError('synthetic disk failure')
        return original(directory_fd, name, image)

    monkeypatch.setattr(module, '_write_png', fail)
    assert module.main(arguments(inputs[0])) != 0
    assert 'synthetic disk failure' in capsys.readouterr().err
    assert not (inputs[0] / 'preview').exists()
    assert not list(inputs[0].glob('.nmm-render-*'))


@pytest.mark.parametrize('competitor', ['directory', 'symlink'])
def test_atomic_publish_does_not_replace_racing_destination(inputs, monkeypatch, competitor):
    module = importlib.import_module('scripts.nmm_render_demo')
    publish = module._publish_directory
    sentinel = inputs[0] / 'sentinel'
    sentinel.write_bytes(b'keep')
    inode = None

    def race(parent_fd, staging_name, output_name):
        nonlocal inode
        if competitor == 'directory':
            os.mkdir(output_name, dir_fd=parent_fd)
        else:
            os.symlink('sentinel', output_name, dir_fd=parent_fd)
        inode = os.stat(output_name, dir_fd=parent_fd, follow_symlinks=False).st_ino
        return publish(parent_fd, staging_name, output_name)

    monkeypatch.setattr(module, '_publish_directory', race)
    assert module.main(arguments(inputs[0])) != 0
    output = inputs[0] / 'preview'
    assert output.lstat().st_ino == inode
    assert sentinel.read_bytes() == b'keep'
    assert not list(inputs[0].glob('.nmm-render-*'))
    if competitor == 'directory':
        assert list(output.iterdir()) == []


def test_staging_name_collision_does_not_remove_preexisting_directory(inputs, monkeypatch):
    from uuid import UUID

    module = importlib.import_module('scripts.nmm_render_demo')
    identifier = UUID('22222222-2222-4222-8222-222222222222')
    existing = inputs[0] / f'.nmm-render-{identifier.hex}'
    existing.mkdir()
    inode = existing.stat().st_ino
    monkeypatch.setattr(module, 'uuid4', lambda: identifier)
    assert module.main(arguments(inputs[0])) != 0
    assert existing.stat().st_ino == inode
    assert not (inputs[0] / 'preview').exists()


def test_cli_reports_reduced_steps_and_resizes_final_without_resizing_mask(inputs):
    directory, original, _, mask = inputs
    Image.new('RGB', (2, 2), (110, 120, 130)).save(directory / 'final.png')
    completed = invoke(arguments(directory) + ['--steps', '9'])
    assert completed.returncode == 0, completed.stderr
    output = directory / 'preview'
    report = json.loads((output / 'result.json').read_text())
    assert report['requested_step_count'] == 9
    assert report['actual_step_count'] == 3
    assert report['warnings']
    with Image.open(output / report['steps'][-1]['image']) as image:
        result = np.array(image)
    assert result.shape == original.shape
    assert np.array_equal(result[~mask], original[~mask])
    assert (result[mask] == (110, 120, 130)).all()


@pytest.mark.parametrize(
    'flag,value', [('--detail-strength', 'nan'), ('--seed', '-1'), ('--steps', '10')]
)
def test_cli_rejects_invalid_render_parameters(inputs, flag, value):
    completed = invoke(arguments(inputs[0]) + [flag, value])
    assert completed.returncode != 0
    assert 'error' in completed.stderr.lower()
    assert not (inputs[0] / 'preview').exists()
    assert not list(inputs[0].glob('.nmm-render-*'))
