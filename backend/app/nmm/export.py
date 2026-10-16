"""Bounded current-version teaching archives, deliberately not project backups."""

from dataclasses import dataclass
from io import BytesIO
import json
from tempfile import SpooledTemporaryFile
from types import SimpleNamespace
from uuid import UUID
from zipfile import ZIP_DEFLATED, ZIP_STORED, ZipFile

import numpy as np
from PIL import Image
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.services.errors import ServiceError
from .models import NmmAsset, NmmPlan, NmmStep
from .render import make_contact_sheet

MAX_TOTAL_BYTES = 64 * 1024 * 1024
MAX_ARCHIVE_BYTES = MAX_TOTAL_BYTES + 128 * 1024
MAX_JSON_BYTES = 1024 * 1024
MAX_STEPS = 9
_DESCRIPTION_FIELDS = frozenset(
    (
        'index',
        'title',
        'color_hex',
        'area_ratio',
        'color_description',
        'areas',
        'technique',
        'expected_result',
        'change_from_previous',
        'common_mistakes',
        'needs_confirmation',
    )
)


@dataclass
class ResultArchive:
    file: object
    size: int

    def close(self):
        self.file.close()


class _BoundedFile:
    def __init__(self, target):
        self.target = target

    def __getattr__(self, name):
        return getattr(self.target, name)

    def write(self, data):
        if self.target.tell() + len(data) > MAX_ARCHIVE_BYTES:
            raise ServiceError(413, 'NMM 成果包超过大小上限。')
        return self.target.write(data)


def build_results_archive(service, project_id: UUID) -> ResultArchive:
    """Materialize and unlock before transmission; close the returned file afterwards.

    The project row lock prevents deletion/revision publication while reading the
    finite snapshot. Only explicit latest-plan step/diff references are trusted.
    Final is the last step; contact sheet is rebuilt from those exact steps, so
    unrelated same-revision assets can never sneak into the archive.
    """
    target = SpooledTemporaryFile(max_size=8 * 1024 * 1024, mode='w+b')
    total = 0
    try:
        with Session(service.engine) as db, db.begin():
            project = service._project(db, project_id, lock=True)
            if (
                project.status in ('generating', 'planning', 'rendering')
                or project.active_job_id is not None
            ):
                raise ServiceError(409, 'NMM 任务执行中，请停止或等待完成后下载成果。')
            plan = db.scalar(
                select(NmmPlan)
                .where(NmmPlan.project_id == project.id, NmmPlan.revision == project.revision)
                .order_by(NmmPlan.created_at.desc(), NmmPlan.id.desc())
            )
            if plan is None or plan.candidate_id != project.selected_candidate_id:
                raise ServiceError(409, '当前版本尚无可下载的步骤成果。')
            steps = list(
                db.scalars(
                    select(NmmStep).where(NmmStep.plan_id == plan.id).order_by(NmmStep.index)
                )
            )
            if not 1 <= len(steps) <= MAX_STEPS or [step.index for step in steps] != list(
                range(1, len(steps) + 1)
            ):
                raise ServiceError(409, '当前版本的步骤成果不完整。')
            images = []
            metadata = []
            final = None
            with ZipFile(
                _BoundedFile(target), 'w', compression=ZIP_STORED, allowZip64=False
            ) as archive:

                def add(name, raw, *, text=False):
                    nonlocal total
                    if total + len(raw) > MAX_TOTAL_BYTES:
                        raise ServiceError(413, 'NMM 成果包超过 64 MiB 上限。')
                    total += len(raw)
                    archive.writestr(name, raw, compress_type=ZIP_DEFLATED if text else ZIP_STORED)

                def read(identity, role):
                    asset = db.get(NmmAsset, identity)
                    if asset is None or (asset.project_id, asset.revision, asset.role) != (
                        project.id,
                        project.revision,
                        role,
                    ):
                        raise ServiceError(409, '步骤图片不属于当前项目版本。')
                    raw, kind = service.read_asset(asset.id)
                    if kind != 'image/png':
                        raise ServiceError(409, '步骤成果图片格式不受支持。')
                    return raw

                for step in steps:
                    raw = read(step.image_asset_id, 'step')
                    name = f'step-{step.index:02d}.png'
                    add(name, raw)
                    with Image.open(BytesIO(raw)) as decoded:
                        if decoded.width * decoded.height > 4_000_000:
                            raise ServiceError(413, '步骤图片超过 4 MP 上限。')
                        thumb = decoded.convert('RGB')
                        thumb.thumbnail((256, 256), Image.Resampling.LANCZOS)
                        images.append(SimpleNamespace(image=np.array(thumb)))
                    diff_name = None
                    if step.diff_asset_id is not None:
                        diff_name = f'diff-{step.index:02d}.png'
                        add(diff_name, read(step.diff_asset_id, 'step_diff'))
                    metadata.append(
                        {
                            'index': step.index,
                            'title': step.title,
                            'image': name,
                            'diff': diff_name,
                            'description': {
                                key: value
                                for key, value in step.description.items()
                                if key in _DESCRIPTION_FIELDS
                            },
                            'warnings': step.warnings,
                        }
                    )
                    final = raw
                add('final.png', final)
                stream = BytesIO()
                make_contact_sheet(images).save(stream, 'PNG')
                add('contact-sheet.png', stream.getvalue())
                result = {
                    'format_version': 1,
                    'kind': 'nmm_results',
                    'revision': project.revision,
                    'renderer_version': plan.renderer_version,
                    'params': {
                        key: project.params[key]
                        for key in ('metal', 'light_direction', 'contrast', 'step_count')
                    },
                    'palette': [
                        {key: color[key] for key in ('hex', 'lab', 'area_ratio')}
                        for color in plan.palette
                    ],
                    'render_params': plan.render_params,
                    'steps': metadata,
                    'overall_tips': plan.plan.get('overall_tips', []),
                    'fallback': bool(plan.plan.get('fallback', False)),
                    'warnings': project.warnings,
                }
                raw = json.dumps(
                    result, ensure_ascii=False, allow_nan=False, separators=(',', ':')
                ).encode('utf-8')
                if len(raw) > MAX_JSON_BYTES:
                    raise ServiceError(413, '教学说明超过导出大小上限。')
                add('result.json', raw, text=True)
        size = target.tell()
        target.seek(0)
        return ResultArchive(target, size)
    except BaseException:
        target.close()
        raise
