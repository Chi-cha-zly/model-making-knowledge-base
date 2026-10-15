"""Offline deterministic NMM preview; run from backend with python -m scripts.nmm_render_demo.

Only macOS/Linux filesystems supporting atomic no-replace rename are supported.
No database, private settings, model provider, or workflow is loaded by this CLI.
"""

import argparse
import ctypes
from dataclasses import asdict
from io import BytesIO
import json
import os
from pathlib import Path
import stat
import sys
from uuid import uuid4

import numpy as np
from PIL import Image

from app.nmm.storage import MAX_IMAGE_BYTES, normalize_image, validate_mask

MAX_PIXELS = 4_000_000


def _parser():
    parser = argparse.ArgumentParser(
        description='Render an offline NMM preview into a new directory.'
    )
    for name in ('original', 'final', 'mask', 'output'):
        parser.add_argument(f'--{name}', type=Path, required=True)
    parser.add_argument('--steps', type=int, choices=range(4, 10), default=7)
    parser.add_argument('--detail-strength', type=float, default=0.6)
    parser.add_argument('--detail-sigma', type=float, default=1.2)
    parser.add_argument('--erosion-radius', type=int, default=1)
    parser.add_argument('--transition-width', type=int, default=2)
    parser.add_argument('--diff-threshold', type=float, default=2.0)
    parser.add_argument('--seed', type=int, default=42)
    return parser


def _input(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    with os.fdopen(descriptor, 'rb') as source:
        if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
            raise ValueError('Input must be a regular image file.')
        raw = source.read(MAX_IMAGE_BYTES + 1)
    if raw.startswith(b'\x89PNG\r\n\x1a\n'):
        content_type = 'image/png'
    elif raw.startswith(b'\xff\xd8\xff'):
        content_type = 'image/jpeg'
    elif raw[:4] == b'RIFF' and raw[8:12] == b'WEBP':
        content_type = 'image/webp'
    else:
        raise ValueError('Input must be a PNG, JPEG, or WebP image.')
    return raw, content_type


def _rgb(path, label):
    normalized = normalize_image(*_input(path), max_pixels=MAX_PIXELS)
    with Image.open(BytesIO(normalized.data)) as image:
        with image.getchannel('A') as alpha:
            if alpha.getextrema() != (255, 255):
                raise ValueError(f'{label} contains transparent pixels; use an opaque image.')
        with image.convert('RGB') as rgb:
            return np.array(rgb, dtype=np.uint8)


def _mask(path, width, height):
    normalized = validate_mask(*_input(path), width=width, height=height, max_pixels=MAX_PIXELS)
    with Image.open(BytesIO(normalized.data)) as image:
        with image.convert('L') as grey:
            return np.array(grey) == 255


def _publish_directory(parent_fd, staging_name, output_name):
    """Do not replace even a racing empty directory or dangling symbolic link.

    Python's os.rename may replace an empty directory; existence checks cannot
    close that race. Use each supported platform's atomic no-replace operation.
    """
    libc = ctypes.CDLL(None, use_errno=True)
    if sys.platform == 'darwin':
        symbol, flag = 'renameatx_np', 0x00000004  # RENAME_EXCL, Apple sys/stdio.h
    elif sys.platform.startswith('linux'):
        symbol, flag = 'renameat2', 1  # RENAME_NOREPLACE
    else:
        raise RuntimeError('Atomic no-replace publication requires macOS or Linux.')
    rename = getattr(libc, symbol, None)
    if rename is None:
        raise RuntimeError('Atomic no-replace rename is unavailable; output was not published.')
    rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    rename.restype = ctypes.c_int
    if rename(parent_fd, os.fsencode(staging_name), parent_fd, os.fsencode(output_name), flag):
        code = ctypes.get_errno()
        raise OSError(code, f'Cannot publish output without replacing it: {os.strerror(code)}')


def _write_png(directory_fd, name, image):
    descriptor = os.open(
        name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode=0o600, dir_fd=directory_fd
    )
    with os.fdopen(descriptor, 'wb') as output:
        image.save(output, 'PNG')
        output.flush()
        os.fsync(output.fileno())


def _write_json(directory_fd, report):
    descriptor = os.open(
        'result.json',
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
        mode=0o600,
        dir_fd=directory_fd,
    )
    with os.fdopen(descriptor, 'w', encoding='utf-8') as output:
        json.dump(report, output, ensure_ascii=False, indent=2, allow_nan=False)
        output.write('\n')
        output.flush()
        os.fsync(output.fileno())


def run(args):
    output = args.output.absolute()
    if output.name in ('', '.', '..'):
        raise ValueError('Output must name a new directory.')
    # The configured parent may be a normal macOS /tmp alias. The output itself
    # is never resolved, so an existing symbolic link is rejected as an entry.
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
        original = _rgb(args.original, 'Original')
        final = _rgb(args.final, 'Final')
        mask = _mask(args.mask, original.shape[1], original.shape[0])
        from app.nmm.render import RENDERER_VERSION, RenderParams, make_contact_sheet, render_steps

        params = RenderParams(
            detail_strength=args.detail_strength,
            detail_sigma=args.detail_sigma,
            erosion_radius=args.erosion_radius,
            transition_width=args.transition_width,
            diff_threshold=args.diff_threshold,
            seed=args.seed,
        )
        result = render_steps(original, final, mask, step_count=args.steps, params=params)
        candidate = f'.nmm-render-{uuid4().hex}'
        os.mkdir(candidate, mode=0o700, dir_fd=parent_fd)
        staging_name = candidate  # Cleanup ownership starts only after exclusive creation.
        staging_fd = os.open(
            staging_name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd
        )
        report = {
            'renderer_version': RENDERER_VERSION,
            'requested_step_count': args.steps,
            'actual_step_count': len(result.steps),
            'params': asdict(params),
            'palette': [asdict(color) for color in result.palette],
            'warnings': result.warnings,
            'steps': [],
        }
        for index, step in enumerate(result.steps, 1):
            image_name, diff_name = f'step-{index:02d}.png', f'diff-{index:02d}.png'
            with Image.fromarray(step.image) as image:
                _write_png(staging_fd, image_name, image)
            with Image.fromarray(step.diff_mask.astype(np.uint8) * 255) as diff:
                _write_png(staging_fd, diff_name, diff)
            report['steps'].append(
                {
                    'kind': step.kind,
                    'palette_index': step.palette_index,
                    'area_ratio': step.area_ratio,
                    'image': image_name,
                    'diff_mask': diff_name,
                }
            )
        with make_contact_sheet(result.steps) as contact:
            _write_png(staging_fd, 'contact-sheet.png', contact)
        _write_json(staging_fd, report)
        os.fsync(staging_fd)
        _publish_directory(parent_fd, staging_name, output.name)
        staging_name = None  # Published directory is complete and is no longer cleanup-owned.
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
    except (OSError, ValueError, RuntimeError, ImportError) as exc:
        print(f'NMM preview error: {exc}', file=sys.stderr)
        return 1
    print(
        f'Rendered {report["actual_step_count"]} steps ({report["requested_step_count"]} requested).'
    )
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
