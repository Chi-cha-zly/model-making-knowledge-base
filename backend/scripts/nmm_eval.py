"""Offline NMM evaluation, default read-only validation; --apply computes locally.

Example configuration (paths relative to the JSON file):
{"groups": [{"label": "sample", "steps": ["base.png", "middle.png", "final.png"],
             "mask": "mask.png", "original": "before.png", "step_count": 7,
             "render_params": {"seed": 42}}]}

All images must already be manually aligned/cropped to identical dimensions.
Original is optional: when absent, detail strength is forced to zero and final
is used only as the exterior canvas. That mode cannot assess original detail
preservation. The final frame is a construction check, NEVER an intermediate
quality score. Reports and comparison sheets may contain private reference
material: keep the explicit output directory local. No DB/settings/model access.
"""

import argparse
from collections import Counter
from dataclasses import asdict, replace
from hashlib import sha256
from io import BytesIO
import json
import os
from pathlib import Path
import stat
import sys
from uuid import uuid4

import numpy as np
from PIL import Image, ImageDraw

from app.nmm.evaluation import compare_frames, match_intermediates
from app.nmm.render import RENDERER_VERSION, RenderParams
from app.nmm.storage import MAX_IMAGE_BYTES, normalize_image, validate_mask
from scripts.nmm_render_demo import _input, _publish_directory, _write_json, _write_png

MAX_CONFIG_BYTES = 1024 * 1024
MAX_INPUT_BYTES = 512 * 1024 * 1024
MAX_OUTPUT_BYTES = 64 * 1024 * 1024
MAX_PIXELS = 4_000_000


def _parser():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument(
        '--apply', action='store_true', help='Run local computation and write new output.'
    )
    return parser


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f'Duplicate JSON key: {key}')
        result[key] = value
    return result


def _configuration(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    with os.fdopen(descriptor, 'rb') as source:
        if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
            raise ValueError('Configuration must be a regular JSON file.')
        raw = source.read(MAX_CONFIG_BYTES + 1)
    if len(raw) > MAX_CONFIG_BYTES:
        raise ValueError('Configuration exceeds 1MiB.')
    config = json.loads(raw, object_pairs_hook=_object)
    if not isinstance(config, dict) or set(config) != {'groups'}:
        raise ValueError('Configuration must contain only groups.')
    groups = config['groups']
    if not isinstance(groups, list) or not 1 <= len(groups) <= 12:
        raise ValueError('Configuration requires 1–12 groups.')
    return groups, sha256(raw).hexdigest()


def _path(base, value):
    if not isinstance(value, str) or not value.strip() or '\0' in value:
        raise ValueError('Each image path must be a nonempty local path.')
    path = Path(value)
    return path if path.is_absolute() else base / path


def _image(path, *, expected=None, mask_size=None):
    raw, content_type = _input(path)
    if len(raw) > MAX_IMAGE_BYTES:
        raise ValueError('Image exceeds 20MiB.')
    fingerprint = sha256(raw).hexdigest()
    if expected is not None and fingerprint != expected:
        raise ValueError('Input changed after validation; restart with a stable configuration.')
    if mask_size:
        normalized = validate_mask(
            raw, content_type, width=mask_size[0], height=mask_size[1], max_pixels=MAX_PIXELS
        )
    else:
        normalized = normalize_image(raw, content_type, max_pixels=MAX_PIXELS)
    with Image.open(BytesIO(normalized.data)) as image:
        with image.getchannel('A') as alpha:
            if alpha.getextrema() != (255, 255):
                raise ValueError('Evaluation images must be opaque.')
        with image.convert('L' if mask_size else 'RGB') as converted:
            pixels = np.array(converted)
    if mask_size:
        pixels = pixels == 255
        if not pixels.any():
            raise ValueError('Mask must contain editable pixels.')
    return pixels, fingerprint, len(raw)


def _prepare(config_path):
    configurations, fingerprint = _configuration(config_path)
    prepared = []
    total = 0
    for index, group in enumerate(configurations, 1):
        if not isinstance(group, dict) or set(group) - {
            'label',
            'steps',
            'mask',
            'original',
            'step_count',
            'render_params',
        }:
            raise ValueError('Unknown group configuration field.')
        paths = group.get('steps')
        if not isinstance(paths, list) or not 2 <= len(paths) <= 30:
            raise ValueError('Each group requires 2–30 real steps, final last.')
        label = group.get('label', f'group-{index:02d}')
        if not isinstance(label, str) or not 1 <= len(label) <= 120:
            raise ValueError('Group label must contain 1–120 characters.')
        count = group.get('step_count', 7)
        if type(count) is not int or not 4 <= count <= 9:
            raise ValueError('Requested step_count must be an integer from 4 to 9.')
        values = group.get('render_params', {})
        if not isinstance(values, dict):
            raise ValueError('render_params must be an object.')
        params = RenderParams(**values)
        base = config_path.absolute().parent
        sources = {
            'steps': [_path(base, path) for path in paths],
            'mask': _path(base, group.get('mask')),
            'original': _path(base, group['original']) if 'original' in group else None,
        }
        hashes = {'steps': [], 'mask': None, 'original': None}
        dimensions = None
        # Decode one image at a time, even during preflight: 12 x 30 frames do
        # not become a resident image collection. Apply rechecks raw hashes.
        for role, paths in [
            ('steps', sources['steps']),
            ('original', [sources['original']] if sources['original'] else []),
        ]:
            for path in paths:
                pixels, digest, size = _image(path)
                shape = [pixels.shape[1], pixels.shape[0]]
                del pixels
                if dimensions is not None and shape != dimensions:
                    raise ValueError(
                        'All cropped frames and original must have identical dimensions.'
                    )
                dimensions = shape
                if role == 'steps':
                    hashes[role].append(digest)
                else:
                    hashes[role] = digest
                total += size
                if total > MAX_INPUT_BYTES:
                    raise ValueError('Total input images exceed 512MiB.')
        mask, hashes['mask'], size = _image(sources['mask'], mask_size=dimensions)
        del mask
        total += size
        if total > MAX_INPUT_BYTES:
            raise ValueError('Total input images exceed 512MiB.')
        prepared.append(
            {
                'sources': sources,
                'metadata': {
                    'index': index,
                    'label': label,
                    'dimensions': dimensions,
                    'reference_step_count': len(sources['steps']),
                    'requested_step_count': count,
                    'input_sha256': hashes,
                    'requested_params': asdict(params),
                    'params': asdict(
                        params if sources['original'] else replace(params, detail_strength=0)
                    ),
                    'original_policy': 'provided'
                    if sources['original']
                    else 'missing_disable_detail',
                    'warnings': []
                    if sources['original']
                    else [
                        'Original absent: detail disabled; final used only as exterior canvas. '
                        'Original-detail preservation and pre-paint changes are not evaluated.'
                    ],
                },
            }
        )
    return prepared, fingerprint


def _cell(sheet, array, row, column, label):
    with Image.fromarray(array) as thumb:
        thumb.thumbnail((244, 228), Image.Resampling.LANCZOS)
        x, y = column * 256, row * 256
        sheet.paste(thumb, (x + (256 - thumb.width) // 2, y + 24 + (228 - thumb.height) // 2))
        ImageDraw.Draw(sheet).text((x + 6, y + 5), label, fill='black')


def _evaluate(group):
    from app.nmm.render import render_steps

    sources, report = group['sources'], dict(group['metadata'])
    hashes = report['input_sha256']
    final, _, _ = _image(sources['steps'][-1], expected=hashes['steps'][-1])
    original = (
        _image(sources['original'], expected=hashes['original'])[0]
        if sources['original']
        else final
    )
    mask, _, _ = _image(sources['mask'], expected=hashes['mask'], mask_size=report['dimensions'])
    rendered = render_steps(
        original,
        final,
        mask,
        step_count=report['requested_step_count'],
        params=RenderParams(**report['params']),
    )
    palette = [color.lab for color in rendered.palette]
    pairs = match_intermediates(len(sources['steps']), len(rendered.steps))
    sheet = Image.new('RGB', (512, 256 * (len(pairs) + 1)), 'white')
    try:
        for row, pair in enumerate(pairs):
            real_index, generated_index = pair['reference_step'] - 1, pair['generated_step'] - 1
            real, _, _ = _image(sources['steps'][real_index], expected=hashes['steps'][real_index])
            generated = rendered.steps[generated_index]
            pair.update(compare_frames(real, generated.image, mask, palette))
            pair['generated_kind'] = generated.kind
            _cell(sheet, real, row, 0, f'Real {real_index + 1} / {len(sources["steps"])}')
            _cell(
                sheet,
                generated.image,
                row,
                1,
                f'Generated {generated_index + 1} / {len(rendered.steps)} ({generated.kind})',
            )
            del real
        final_metrics = compare_frames(final, rendered.steps[-1].image, mask, palette)
        final_metrics['excluded_from_intermediate_summary'] = True
        _cell(sheet, final, len(pairs), 0, 'Real final (not scored)')
        _cell(
            sheet, rendered.steps[-1].image, len(pairs), 1, 'Generated final (construction check)'
        )
        counts = Counter(pair['generated_step'] for pair in pairs)
        report.update(
            {
                'renderer_version': RENDERER_VERSION,
                'actual_step_count': len(rendered.steps),
                'palette': [asdict(color) for color in rendered.palette],
                'warnings': report['warnings'] + rendered.warnings,
                'pairs': pairs,
                'final_check': final_metrics,
                'repeated_generated_steps': [index for index, count in counts.items() if count > 1],
                'intermediate_summary': {
                    'pair_count': len(pairs),
                    'mean_delta_e2000': sum(p['mean_delta_e2000'] for p in pairs) / len(pairs),
                    'mean_absolute_area_ratio_difference': sum(
                        p['mean_absolute_area_ratio_difference'] for p in pairs
                    )
                    / len(pairs),
                },
            }
        )
        return report, sheet
    except BaseException:
        sheet.close()
        raise


def run(args):
    output = args.output.absolute()
    if output.name in ('', '.', '..'):
        raise ValueError('Output must name a new directory.')
    parent = output.parent.resolve(strict=True)
    parent_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    staging_name, staging_fd = None, None
    try:
        try:
            os.stat(output.name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise FileExistsError('Output already exists; choose a new directory.')
        groups, fingerprint = _prepare(args.config)
        report = {
            'schema_version': 1,
            'mode': 'apply' if args.apply else 'dry_run',
            'config_sha256': fingerprint,
            'method': {
                'matching': 'nearest normalized progress, first=0/final=1; finals excluded; ties earlier',
                'palette': 'shared final Lab centers, nearest Euclidean Lab assignment',
                'area': 'palette population fractions inside mask; not paint/diff masks',
                'aggregation': 'mean over reference intermediates, then equal-weight groups; final excluded',
                'limitations': 'Descriptive aligned-pixel metrics, not semantic or teaching-quality ratings.',
            },
            'groups': [],
        }
        if not args.apply:
            report['groups'] = [group['metadata'] for group in groups]
            return report
        candidate = f'.nmm-eval-{uuid4().hex}'
        os.mkdir(candidate, mode=0o700, dir_fd=parent_fd)
        staging_name = candidate
        staging_fd = os.open(
            candidate, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd
        )
        total = 0
        for index, group in enumerate(groups, 1):
            result, comparison = _evaluate(group)
            name = f'group-{index:02d}.png'
            with comparison:
                _write_png(staging_fd, name, comparison)
            total += os.stat(name, dir_fd=staging_fd, follow_symlinks=False).st_size
            if total > MAX_OUTPUT_BYTES:
                raise ValueError('Evaluation output exceeds 64MiB.')
            result['comparison'] = name
            report['groups'].append(result)
        report['intermediate_macro_average'] = {
            metric: sum(g['intermediate_summary'][metric] for g in report['groups']) / len(groups)
            for metric in ('mean_delta_e2000', 'mean_absolute_area_ratio_difference')
        }
        encoded = json.dumps(report, ensure_ascii=False, allow_nan=False, indent=2).encode('utf-8')
        if len(encoded) > MAX_CONFIG_BYTES or len(encoded) + 1 + total > MAX_OUTPUT_BYTES:
            raise ValueError('Evaluation report/output exceeds its size limit.')
        _write_json(staging_fd, report)
        os.fsync(staging_fd)
        _publish_directory(parent_fd, staging_name, output.name)
        staging_name = None
        return report
    finally:
        try:
            if staging_name is not None:
                if staging_fd is not None:
                    for name in os.listdir(staging_fd):
                        os.unlink(name, dir_fd=staging_fd)
                os.rmdir(staging_name, dir_fd=parent_fd)
        finally:
            if staging_fd is not None:
                os.close(staging_fd)
            os.close(parent_fd)


def main(argv=None):
    args = _parser().parse_args(argv)
    try:
        report = run(args)
    except (OSError, ValueError, TypeError, RuntimeError, ImportError) as exc:
        print(f'NMM evaluation error: {exc}', file=sys.stderr)
        return 1
    print(json.dumps(report, ensure_ascii=False, allow_nan=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
