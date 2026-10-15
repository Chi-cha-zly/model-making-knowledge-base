"""Guarded, replay-safe graph nodes; worker threads compute arrays only."""

import asyncio
from copy import deepcopy
from dataclasses import asdict
from io import BytesIO
from uuid import UUID

import numpy as np
from PIL import Image
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import now
from app.services.errors import ServiceError
from .models import NmmAsset
from .prompts import analysis_prompt, generation_prompt, plan_prompt, review_prompt
from .providers import ImageInput
from .render import RENDERER_VERSION, RenderParams, make_contact_sheet, render_steps
from .repository import NmmRepository
from .storage import NormalizedImage, normalize_image
from .workflow_schemas import AnalysisOutput, PlanOutput, ReviewOutput


def png(image):
    stream = BytesIO()
    image.save(stream, 'PNG')
    return NormalizedImage(stream.getvalue(), 'image/png', image.width, image.height)


class WorkflowOperations:
    def __init__(self, service, project_id, revision, job_id, thread):
        self.service, self.project_id, self.revision, self.job_id, self.thread = (
            service,
            project_id,
            revision,
            job_id,
            thread,
        )

    def guard(self, db):
        return self.service._guard(db, self.project_id, self.revision, self.job_id, self.thread)

    def inputs(self):
        with Session(self.service.engine) as db:
            project = self.guard(db)
            assets = list(
                db.scalars(
                    select(NmmAsset)
                    .where(NmmAsset.project_id == project.id, NmmAsset.revision == self.revision)
                    .order_by(NmmAsset.sort_order)
                )
            )
            output = {}
            for asset in assets:
                if asset.role in ('original', 'mask', 'reference_style', 'reference_palette'):
                    raw, kind = self.service.inputs.read_asset(asset.id)
                    output.setdefault(asset.role, []).append(ImageInput(raw, kind))
            return output

    def selected(self, state):
        raw, kind = self.service.inputs.read_asset(UUID(state['selected_candidate_id']))
        return ImageInput(raw, kind)

    def render_inputs(self, state):
        inputs = self.inputs()
        with (
            Image.open(BytesIO(inputs['original'][0].data)) as source,
            Image.open(BytesIO(inputs['mask'][0].data)) as region,
            Image.open(BytesIO(self.selected(state).data)) as final,
        ):
            return (
                np.array(source.convert('RGB')),
                np.array(final.convert('RGB')),
                np.array(region.convert('L')) > 0,
            )

    def write(self, key, output, *, images=(), writer=None):
        written = []
        try:
            # Check again after every provider/CPU await, before creating files.
            with Session(self.service.engine) as db, db.begin():
                project = self.guard(db)
                repository = NmmRepository(db)
                asset_ids = []
                for role, image, meta in images:
                    stored = self.service.inputs.storage.put(project.id, image)
                    written.append((project.id, stored.name))
                    asset = repository.add_asset(
                        project.id,
                        role=role,
                        storage_name=stored.name,
                        content_type=stored.content_type,
                        size=stored.size,
                        width=stored.width,
                        height=stored.height,
                        sha256=stored.sha256,
                        sort_order=project.generation_count * 100 + len(asset_ids),
                        meta=meta,
                    )
                    asset_ids.append(str(asset.id))
                if writer is not None:
                    output = writer(db, project, repository, asset_ids)
                project.execution = {
                    **project.execution,
                    'stages': {
                        **project.execution.get('stages', {}),
                        key: {'status': 'completed', 'output': deepcopy(output)},
                    },
                }
                if 'analysis' in output:
                    project.analysis = output['analysis']
                if 'warnings' in output:
                    project.warnings = output['warnings']
                project.updated_at = now()
            return output
        except BaseException:
            for identity, name in written:
                self.service.inputs.storage.delete(identity, name)
            raise

    async def stage(self, name, state):
        attempt = state.get('attempt', 0) + (name == 'generate')
        key = f'{name}:{attempt}'
        with Session(self.service.engine) as db, db.begin():
            project = self.guard(db)
            cached = project.execution.get('stages', {}).get(key)
            if cached:
                if cached['status'] == 'completed':
                    return deepcopy(cached['output'])
                # A started generation with no committed output has unknown
                # outcome. Never silently issue it again on checkpoint replay.
                raise ServiceError(409, '生成结果未知；需要用户明确新建一次尝试。')
            project.phase = name
            project.status = (
                'generating'
                if name in ('analyze', 'generate')
                else 'planning'
                if name in ('palette', 'plan')
                else 'rendering'
            )
            project.updated_at = now()
            if name == 'generate':
                if project.generation_count >= self.service.max_generations:
                    raise ServiceError(409, '项目生成次数已达上限。')
                project.generation_count += 1
                project.execution = {
                    **project.execution,
                    'stages': {**project.execution.get('stages', {}), key: {'status': 'started'}},
                }
        inputs = self.inputs()
        prior_warnings = list(state.get('warnings', []))
        if name == 'analyze':
            images = (
                inputs['original']
                + inputs['mask']
                + inputs.get('reference_style', [])
                + inputs.get('reference_palette', [])
            )
            analysis = await self.service.vision.analyze(
                images, analysis_prompt(state['params']), AnalysisOutput
            )
            analysis = AnalysisOutput.model_validate(analysis).model_dump()
            return self.write(
                key, {'analysis': analysis, 'warnings': prior_warnings + analysis['warnings']}
            )
        if name == 'generate':
            references = inputs.get('reference_style', []) + inputs.get('reference_palette', [])
            outputs = await self.service.image_edit.edit(
                inputs['original'][0],
                inputs['mask'][0],
                references,
                generation_prompt(state['params'], state['analysis']),
                1,
            )
            if not isinstance(outputs, list) or not 1 <= len(outputs) <= 4:
                raise ValueError('Invalid candidate output count')
            images = []
            with (
                Image.open(BytesIO(inputs['original'][0].data)) as source,
                Image.open(BytesIO(inputs['mask'][0].data)) as region,
            ):
                source = source.convert('RGB')
                region = region.convert('L')
                for raw in outputs:
                    normalized = normalize_image(raw, 'image/png', max_pixels=4_000_000)
                    with Image.open(BytesIO(normalized.data)) as decoded:
                        if decoded.mode == 'RGBA' and decoded.getchannel('A').getextrema() != (
                            255,
                            255,
                        ):
                            raise ValueError('Candidate must be opaque')
                        candidate = decoded.convert('RGB').resize(
                            source.size, Image.Resampling.LANCZOS
                        )
                        image = Image.composite(candidate, source, region)
                        images.append(('candidate', png(image), {'attempt': attempt}))

            def writer(db, project, repository, ids):
                return {'candidate_ids': ids, 'attempt': attempt}

            return self.write(key, {}, images=images, writer=writer)
        if name == 'palette':
            result = await asyncio.to_thread(
                render_steps, *self.render_inputs(state), state['params']['step_count']
            )
            palette = [asdict(color) for color in result.palette]
            # JSON-only, including Lab lists instead of dataclass tuples.
            for color in palette:
                color['lab'] = list(color['lab'])
            metadata = [
                {
                    'index': index + 1,
                    'color_hex': result.palette[step.palette_index].hex
                    if step.palette_index is not None
                    else None,
                    'area_ratio': step.area_ratio,
                }
                for index, step in enumerate(result.steps)
            ]
            return self.write(
                key,
                {
                    'palette': palette,
                    'metadata': metadata,
                    'warnings': prior_warnings + result.warnings,
                },
            )
        if name == 'plan':
            colors = {color['hex'] for color in state['palette']}
            images = inputs['original'] + [self.selected(state)]
            for _ in range(3):
                try:
                    plan = await self.service.vision.analyze(
                        images,
                        plan_prompt(state['params'], state['palette'], state['metadata']),
                        PlanOutput,
                    )
                    plan = PlanOutput.model_validate(plan)
                    if len(plan.steps) != len(state['metadata']):
                        raise ValueError('Wrong actual step count')
                    for actual, expected in zip(plan.steps, state['metadata']):
                        used = (
                            actual.color_hex
                            if isinstance(actual.color_hex, list)
                            else [actual.color_hex]
                            if actual.color_hex
                            else []
                        )
                        if (
                            actual.index != expected['index']
                            or any(color not in colors for color in used)
                            or len(used) > 2
                        ):
                            raise ValueError('Invalid step index or palette color')
                        if expected['color_hex'] is not None and used != [expected['color_hex']]:
                            raise ValueError('Base color does not match the rendered layer')
                    value = plan.model_dump()
                    for instruction, expected in zip(value['steps'], state['metadata']):
                        instruction['area_ratio'] = expected['area_ratio']
                    return self.write(key, {'plan': value})
                except (ValidationError, ValueError):
                    continue
            fallback = {'steps': deepcopy(state['metadata']), 'overall_tips': [], 'fallback': True}
            return self.write(
                key,
                {
                    'plan': fallback,
                    'warnings': prior_warnings
                    + ['涂装计划三次输出不合规，仅保留算法颜色与面积；没有生成教学说明。'],
                },
            )
        if name == 'render':
            result = await asyncio.to_thread(
                render_steps, *self.render_inputs(state), state['params']['step_count']
            )
            images = []
            for index, step in enumerate(result.steps):
                images.append(
                    (
                        'step',
                        png(Image.fromarray(step.image)),
                        {
                            'index': index + 1,
                            'kind': step.kind,
                            'palette_index': step.palette_index,
                            'area_ratio': step.area_ratio,
                        },
                    )
                )
                images.append(
                    (
                        'step_diff',
                        png(Image.fromarray(step.diff_mask.astype(np.uint8) * 255)),
                        {'index': index + 1},
                    )
                )
            images.append(('final', png(Image.fromarray(result.steps[-1].image)), {}))
            images.append(('contact_sheet', png(make_contact_sheet(result.steps)), {}))

            def writer(db, project, repository, ids):
                plan = repository.create_plan(
                    project.id,
                    UUID(state['selected_candidate_id']),
                    plan=state['plan'],
                    palette=state['palette'],
                    renderer_version=RENDERER_VERSION,
                    render_params=asdict(RenderParams()),
                )
                step_ids = []
                for index, description in enumerate(state['plan']['steps']):
                    step = repository.add_step(
                        plan.id,
                        index=index + 1,
                        title=description.get('title', f'色阶 {index + 1}'),
                        image_asset_id=UUID(ids[index * 2]),
                        diff_asset_id=UUID(ids[index * 2 + 1]),
                        description=description,
                        warnings=result.warnings,
                    )
                    step_ids.append(str(step.id))
                return {'step_ids': step_ids}

            return self.write(key, {}, images=images, writer=writer)
        if name == 'refine':
            return self.write(key, {})
        if name == 'review':
            with Session(self.service.engine) as db:
                project = self.guard(db)
                assets = list(
                    db.scalars(
                        select(NmmAsset)
                        .where(
                            NmmAsset.project_id == project.id,
                            NmmAsset.revision == self.revision,
                            NmmAsset.role == 'step',
                        )
                        .order_by(NmmAsset.created_at)
                    )
                )
                images = [ImageInput(*self.service.inputs.read_asset(asset.id)) for asset in assets]
            try:
                review = await self.service.vision.analyze(
                    images + [self.selected(state)],
                    review_prompt(state['params'], state['plan']),
                    ReviewOutput,
                )
                review = ReviewOutput.model_validate(review)
                if any(issue.step > len(state['metadata']) for issue in review.issues):
                    raise ValueError('Review step outside actual sequence')
                warnings = [
                    f'步骤 {issue.step} [{issue.severity}/{issue.type}] {issue.detail}'
                    for issue in review.issues
                ]
            except (ValidationError, ValueError):
                warnings = ['质量自检输出不合规；请人工检查步骤。']
            return self.write(key, {'warnings': prior_warnings + warnings})
        raise ValueError('Unknown workflow stage')
