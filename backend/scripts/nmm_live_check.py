"""Explicit NMM workflow check. Default preview reads only the supplied local inputs.

Apply requires both consent flags, a two-call cap, an explicit migrated PostgreSQL
URL and a new output directory. It uses the same project authorization and ledger
as HTTP, stops at candidate confirmation, and never resumes or selects implicitly.
"""

import argparse
import asyncio
from io import BytesIO
import json
import os
from pathlib import Path
import stat
from uuid import uuid4

from PIL import Image

from app.nmm.providers import ImageInput
from app.nmm.schemas import NmmParams
from app.nmm.storage import MAX_IMAGE_BYTES, normalize_image, validate_mask

MAX_PIXELS = 4_000_000


class ConfigurationUnavailable(Exception):
    """Only fixed, safe messages from the trusted capability factory enter this error."""


def parser():
    value = argparse.ArgumentParser(description='Preview an NMM check; consent is required to run.')
    value.add_argument('--original', type=Path, required=True)
    value.add_argument('--mask', type=Path, required=True)
    value.add_argument('--reference', type=Path, action='append', default=[])
    value.add_argument(
        '--role',
        action='append',
        default=[],
        choices=[
            'reference_style',
            'reference_palette',
            'reference_steps',
        ],
    )
    value.add_argument('--params', default='{}', help='Strict JSON NMM parameters')
    value.add_argument('--title', default='NMM live check')
    value.add_argument('--apply', action='store_true')
    value.add_argument('--i-understand-cost', action='store_true')
    value.add_argument(
        '--max-calls', type=int, help='Apply requires exactly 2: one vision + one image'
    )
    value.add_argument(
        '--database-url', help='Explicit migrated verification database; never inferred'
    )
    value.add_argument('--output', type=Path, help='New directory; its parent must already exist')
    return value


def read_image(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    with os.fdopen(descriptor, 'rb') as source:
        if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
            raise ValueError('图片输入必须为普通文件。')
        raw = source.read(MAX_IMAGE_BYTES + 1)
    if raw.startswith(b'\x89PNG\r\n\x1a\n'):
        mime = 'image/png'
    elif raw.startswith(b'\xff\xd8\xff'):
        mime = 'image/jpeg'
    elif raw[:4] == b'RIFF' and raw[8:12] == b'WEBP':
        mime = 'image/webp'
    else:
        raise ValueError('图片输入必须为 PNG、JPEG 或 WebP。')
    return raw, mime


def load_inputs(args):
    if not 1 <= len(args.title.strip()) <= 300 or len(args.params) > 32 * 1024:
        raise ValueError('标题或参数超出限制。')
    params = NmmParams.model_validate_json(args.params, strict=True)
    if len(args.reference) != len(args.role) or len(args.reference) > 12:
        raise ValueError('参考图和用途须一一对应，最多 12 张。')
    if sum(role != 'reference_steps' for role in args.role) > 3:
        raise ValueError('风格与配色参考合计最多 3 张。')
    original = normalize_image(*read_image(args.original), max_pixels=MAX_PIXELS)
    with Image.open(BytesIO(original.data)) as image:
        if image.getchannel('A').getextrema() != (255, 255):
            raise ValueError('原图须不透明。')
    mask = validate_mask(
        *read_image(args.mask), width=original.width, height=original.height, max_pixels=MAX_PIXELS
    )
    references = []
    for role, path in zip(args.role, args.reference):
        image = normalize_image(*read_image(path))
        references.append((role, ImageInput(image.data, image.content_type)))
    return {
        'title': args.title.strip(),
        'params': params,
        'original': ImageInput(original.data, original.content_type),
        'mask': ImageInput(mask.data, mask.content_type),
        'references': references,
    }, {'width': original.width, 'height': original.height}


def output_path(path):
    if path is None:
        raise ValueError('执行模式需要新的输出目录。')
    absolute = path.absolute()
    parent = absolute.parent.resolve(strict=True)
    if not parent.is_dir() or absolute.name in ('', '.', '..'):
        raise ValueError('输出父目录必须已存在。')
    output = parent / absolute.name
    try:
        output.lstat()  # Existing dangling symlinks are also occupied output names.
    except FileNotFoundError:
        return output
    raise ValueError('输出目录必须不存在。')


def write_new(output, name, raw):
    parent = os.open(output, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        descriptor = os.open(
            name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent
        )
        with os.fdopen(descriptor, 'wb') as target:
            target.write(raw)
            target.flush()
            os.fsync(target.fileno())
    finally:
        os.close(parent)


async def execute(engine, output, inputs, vision, image):
    from app.jobs import JobService
    from app.nmm.workflow_service import NmmWorkflowService

    jobs = JobService(engine)
    service = NmmWorkflowService(
        engine, output / 'attachments', jobs, vision=vision, image_edit=image
    )
    project_id = None
    try:
        item = service.create_project(**inputs)
        project_id = item['id']
        quote = service.estimate(project_id)
        authorization = {
            'estimate_id': quote['estimate_id'],
            'max_image_calls': 1,
            'max_vision_calls': 1,
            'accept_unknown_cost': True,
        }
        job = await service.run(
            project_id,
            request_id=str(uuid4()),
            revision=item['revision'],
            confirm_generation=True,
            cost_authorization=authorization,
        )
        task = jobs.tasks.get(job['id'])
        if task is not None:
            await task  # Providers own bounded timeouts; cancellation keeps the ledger.
        detail = service.detail(project_id)
        status = detail['status']
        report = {
            'status': status,
            'project_id': project_id,
            'revision': detail['revision'],
            'provider_mode': detail['provider_mode'],
            'meta': detail.get('meta', {}),
            'estimated_cost': None,
            'candidates': [],
        }
        if status == 'awaiting_confirmation':
            for asset in detail['assets']:
                if asset['role'] == 'candidate':
                    name = f'candidate-{asset["id"]}.png'
                    raw, _ = service.read_asset(asset['id'])
                    write_new(output, name, raw)
                    report['candidates'].append({'asset_id': asset['id'], 'path': name})
        return report
    except Exception:
        # Unknown failures retain any project and ledger for inspection, without
        # printing provider response bodies, credential-bearing URLs or local paths.
        report = {
            'status': 'failed',
            'project_id': project_id,
            'error': 'NMM 验证未完成；已有项目与调用记录保留，不会自动重试。',
        }
        if project_id is not None:
            try:
                report['meta'] = service.detail(project_id).get('meta', {})
            except Exception:
                pass
        return report
    finally:
        await jobs.shutdown()


def main(argv=None, *, environ=None, transport=None):
    args = parser().parse_args(argv)
    engine = None
    try:
        if args.apply and (
            not args.i_understand_cost
            or args.max_calls != 2
            or not args.database_url
            or args.output is None
        ):
            raise ValueError('执行需要明确费用确认、两次调用预算、数据库及新输出目录。')
        inputs, dimensions = load_inputs(args)
        from app.nmm.provider_config import build_nmm_providers

        env = os.environ if environ is None else environ
        settings = {
            key: env.get(key, '')
            for key in (
                'VISION_BASE_URL',
                'VISION_MODEL',
                'VISION_API_KEY',
                'IMAGE_BASE_URL',
                'IMAGE_MODEL',
                'IMAGE_API_KEY',
            )
        }
        vision, image = build_nmm_providers(settings, transport=transport)
        configs = {
            'vision': getattr(vision, 'public_config', {}),
            'image': getattr(image, 'public_config', {}),
        }
        if not args.apply:
            report = {
                'status': 'dry_run',
                'expected_vision_calls': 1,
                'expected_image_calls': 1,
                'estimated_cost': None,
                'input': dimensions,
                'configuration': configs,
            }
        else:
            for provider in (vision, image):
                if getattr(provider, 'mode', 'unconfigured') != 'live':
                    raise ConfigurationUnavailable(provider.safe_message)
            output = output_path(args.output)
            from app.db import create_db_engine, init_db

            engine = create_db_engine(args.database_url)
            init_db(engine)  # Verify only: never migrate or recover unrelated jobs.
            output.mkdir(mode=0o700)  # Atomic exclusive reservation also rejects racing entries.
            report = asyncio.run(execute(engine, output, inputs, vision, image))
            report['configuration'] = configs
            write_new(
                output,
                'report.json',
                (json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + '\n').encode(),
            )
    except ConfigurationUnavailable as error:
        report = {'status': 'failed', 'error': str(error)}
    except Exception:
        report = {
            'status': 'failed',
            'error': 'NMM 验证未完成；请检查输入、费用确认、独立数据库迁移和新输出目录。',
        }
    finally:
        if engine is not None:
            engine.dispose()
    print(json.dumps(report, ensure_ascii=False, allow_nan=False))
    return 0 if report['status'] in ('dry_run', 'awaiting_confirmation') else 2


if __name__ == '__main__':
    raise SystemExit(main())
