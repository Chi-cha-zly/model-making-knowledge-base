"""Project/revision-bound jobs around a persistent, explicitly resumed graph."""

import asyncio
from collections import OrderedDict
from io import BytesIO

import numpy as np
from PIL import Image
from copy import deepcopy
from hashlib import sha256
from uuid import UUID, uuid4

from langgraph.types import Command
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.ingestion import digest
from app.job_models import Job
from app.models import now
from app.services.errors import ServiceError
from .billing import NmmBilling
from .candidate_quality import QUALITY_VERSION, check_candidate, unavailable_check
from .calls import error_record
from .provider_types import NmmProviderError
from .prompts import PROMPT_VERSION
from .workflow_schemas import AnalysisOutput, PlanOutput, ReviewOutput
from .checkpoints import PostgresCheckpointStore, delete_project_checkpoints
from .models import NmmAsset, NmmPlan, NmmProject, NmmStep
from .providers import UnconfiguredImageEditProvider, UnconfiguredVisionProvider
from .repository import NmmRepository
from .schemas import NmmParams
from .service import NmmService
from .workflow import build_workflow

ACTIVE = ('generating', 'planning', 'rendering')
INPUT_ROLES = ('original', 'mask', 'reference_style', 'reference_palette', 'reference_steps')


def encode(row, *, exclude=()):
    result = {}
    for column in row.__table__.columns:
        if column.name in exclude:
            continue
        value = getattr(row, column.name)
        result[column.name] = (
            str(value)
            if isinstance(value, UUID)
            else value.isoformat()
            if hasattr(value, 'isoformat')
            else deepcopy(value)
        )
    return result


class NmmWorkflowService:
    def __init__(
        self,
        engine,
        attachment_root,
        jobs,
        *,
        vision=None,
        image_edit=None,
        checkpoint_store=None,
        max_generations=6,
    ):
        if type(max_generations) is not int or not 1 <= max_generations <= 100:
            raise ValueError('max_generations must be between 1 and 100')
        self.engine, self.jobs = engine, jobs
        self.inputs = NmmService(engine, attachment_root)
        self.vision = vision if vision is not None else UnconfiguredVisionProvider()
        self.image_edit = image_edit if image_edit is not None else UnconfiguredImageEditProvider()
        self.checkpoint_store = (
            checkpoint_store if checkpoint_store is not None else PostgresCheckpointStore(engine)
        )
        self.max_generations = max_generations
        self.lock = asyncio.Lock()
        self._quality_cache = OrderedDict()
        providers = [self.vision, self.image_edit]
        configuration = {
            'configuration_id': digest(
                [
                    PROMPT_VERSION,
                    [
                        schema.model_json_schema()
                        for schema in (AnalysisOutput, PlanOutput, ReviewOutput)
                    ],
                    [
                        getattr(provider, 'configuration_id', type(provider).__name__)
                        for provider in providers
                    ],
                ]
            ),
            'providers': [
                getattr(provider, 'public_config', {'provider': 'offline_test'})
                for provider in providers
            ],
        }
        self.billing = NmmBilling(engine, configuration)

    @property
    def provider_mode(self):
        if isinstance(self.vision, UnconfiguredVisionProvider) or isinstance(
            self.image_edit, UnconfiguredImageEditProvider
        ):
            return 'unconfigured'
        if any(
            getattr(provider, 'mode', None) == 'live' for provider in (self.vision, self.image_edit)
        ):
            return 'live'
        return 'offline_test'

    def _project(self, db, project_id, *, lock=False):
        try:
            identity = UUID(str(project_id))
        except (ValueError, TypeError):
            raise ServiceError(422, 'NMM 项目编号无效。') from None
        project = (
            db.scalar(select(NmmProject).where(NmmProject.id == identity).with_for_update())
            if lock
            else db.get(NmmProject, identity)
        )
        if project is None:
            raise ServiceError(404, 'NMM 项目不存在。')
        return project

    def create_project(self, **kwargs):
        return self.detail(self.inputs.create_project(**kwargs))

    @staticmethod
    def _thumbnail(assets):
        priority = {'final': 0, 'candidate': 1, 'original': 2}
        eligible = [asset for asset in assets if asset.role in priority]
        if not eligible:
            return None
        best = min(
            eligible, key=lambda asset: (priority[asset.role], -asset.created_at.timestamp())
        )
        return str(best.id)

    def list_projects(self):
        with Session(self.engine) as db:
            projects = list(db.scalars(select(NmmProject).order_by(NmmProject.created_at.desc())))
            assets = list(
                db.scalars(
                    select(NmmAsset)
                    .join(
                        NmmProject,
                        (NmmAsset.project_id == NmmProject.id)
                        & (NmmAsset.revision == NmmProject.revision),
                    )
                    .where(NmmAsset.role.in_(('final', 'candidate', 'original')))
                )
            )
            by_project = {}
            for asset in assets:
                by_project.setdefault(asset.project_id, []).append(asset)
            return [
                dict(
                    encode(row, exclude=('meta',)),
                    meta=self.billing.public_meta(row),
                    provider_mode=self.provider_mode,
                    thumbnail_asset_id=self._thumbnail(by_project.get(row.id, [])),
                )
                for row in projects
            ]

    def detail(self, project_id):
        with Session(self.engine) as db:
            project = self._project(db, project_id)
            result = encode(project, exclude=('meta',))
            result['meta'] = self.billing.public_meta(project)
            result['provider_mode'] = self.provider_mode
            assets = list(
                db.scalars(
                    select(NmmAsset)
                    .where(NmmAsset.project_id == project.id, NmmAsset.revision == project.revision)
                    .order_by(NmmAsset.sort_order, NmmAsset.created_at)
                )
            )
            result['thumbnail_asset_id'] = self._thumbnail(assets)
            result['assets'] = [encode(row, exclude=('storage_name',)) for row in assets]
            result['candidate_ids'] = [str(row.id) for row in assets if row.role == 'candidate']
            # Default one output per attempt, at most six attempts. Bound legacy
            # multi-output data too; overflow candidates remain unselectable.
            result['candidate_checks'] = {
                str(row.id): self._candidate_check(db, project, row.id)
                if index < 24
                else unavailable_check()
                for index, row in enumerate(a for a in assets if a.role == 'candidate')
            }
            plan = db.scalar(
                select(NmmPlan)
                .where(NmmPlan.project_id == project.id, NmmPlan.revision == project.revision)
                .order_by(NmmPlan.created_at.desc())
            )
            result['plan'] = encode(plan) if plan else None
            result['steps'] = (
                [
                    encode(row)
                    for row in db.scalars(
                        select(NmmStep).where(NmmStep.plan_id == plan.id).order_by(NmmStep.index)
                    )
                ]
                if plan
                else []
            )
            return result

    def _candidate_check(self, db, project, candidate_id):
        """Recompute legacy/stale metadata read-only; cache only verified content IDs."""
        try:
            candidate = NmmRepository(db)._asset(project, UUID(str(candidate_id)), 'candidate')
            inputs = list(
                db.scalars(
                    select(NmmAsset).where(
                        NmmAsset.project_id == project.id,
                        NmmAsset.revision == project.revision,
                        NmmAsset.role.in_(('original', 'mask')),
                    )
                )
            )
            if len(inputs) != 2 or {a.role for a in inputs} != {'original', 'mask'}:
                return unavailable_check()
            by_role = {a.role: a for a in inputs}
            rows = [by_role['original'], candidate, by_role['mask']]
            # read_asset checks bytes against sha256 even on a cache hit.
            values = [self.inputs.read_asset(a.id)[0] for a in rows]
            key = (QUALITY_VERSION, *(a.sha256 for a in rows))
            if key in self._quality_cache:
                self._quality_cache.move_to_end(key)
                return deepcopy(self._quality_cache[key])
            arrays = []
            for raw in values:
                with Image.open(BytesIO(raw)) as image:
                    if (
                        not 0 < image.width * image.height <= 4_000_000
                        or getattr(image, 'n_frames', 1) != 1
                    ):
                        return unavailable_check()
                    rgba = image.convert('RGBA')
                    if rgba.getchannel('A').getextrema() != (255, 255):
                        return unavailable_check()
                    arrays.append(np.array(rgba.convert('RGB')))
            mask = arrays[2]
            if not np.all((mask == 0).all(axis=2) | (mask == 255).all(axis=2)):
                return unavailable_check()
            result = check_candidate(arrays[0], arrays[1], (mask == 255).all(axis=2))
            self._quality_cache[key] = deepcopy(result)
            while len(self._quality_cache) > 64:
                self._quality_cache.popitem(last=False)
            return result
        except (OSError, ValueError, TypeError, ServiceError):
            return unavailable_check()

    def _require_candidate_quality(self, db, project, candidate_id, acknowledged=True):
        check = self._candidate_check(db, project, candidate_id)
        if check['status'] != 'needs_review':
            recovery = (
                '请检查对比结果，重新估算并明确重新生成。'
                if project.status == 'awaiting_confirmation'
                else '当前已选候选不能继续；请新建项目后重新估算生成。'
            )
            raise ServiceError(409, '候选结构检查未通过，不能采用或继续教学；' + recovery)
        if acknowledged is not True:
            raise ServiceError(422, '请先对照原图，明确确认零件、轮廓和浮雕未被改动。')

    def _recovery_state(self, project):
        resume_selected = (
            str(project.selected_candidate_id)
            if project.selected_candidate_id and project.workflow_thread_id
            else None
        )
        generations = [
            (int(key.split(':')[1]), value)
            for key, value in project.execution.get('stages', {}).items()
            if key.startswith('generate:')
        ]
        generated = (
            bool(generations)
            and max(generations, key=lambda item: item[0])[1]['status'] == 'completed'
        )
        # Analysis may be billed and cached before the business write
        # or graph checkpoint succeeds. Keep that thread to replay its
        # cached result, while never resuming an unknown generation.
        analyzed = not generations and self.billing.has_completed_vision(project, 'analyze:0')
        return resume_selected, generated, analyzed

    def estimate(self, project_id, *, params=None):
        project = self.detail(project_id)
        with Session(self.engine) as db:
            row = self._project(db, project_id)
            selected, generated, _ = self._recovery_state(row)
            generation_required = not bool(selected or (generated and row.workflow_thread_id))
            if params is not None and params != row.params:
                generation_required = True
        result = {
            'generation_required': generation_required,
            'revision': project['revision'],
            'generation_count': project['generation_count'],
            'generation_limit': self.max_generations,
            'remaining_generations': max(0, self.max_generations - project['generation_count']),
            'expected_image_calls': int(generation_required),
            'expected_vision_calls': 5,
            'provider_mode': self.provider_mode,
            'estimated_cost': 0 if self.provider_mode == 'offline_test' else None,
        }
        if self.provider_mode == 'live':
            result.update(self.billing.estimate(project_id, params=params))
        result['expected_image_calls'] = int(generation_required)
        return result

    def read_asset(self, asset_id):
        return self.inputs.read_asset(UUID(str(asset_id)))

    def recover(self):
        self.billing.recover()
        with Session(self.engine) as db, db.begin():
            for project in db.scalars(select(NmmProject).where(NmmProject.status.in_(ACTIVE))):
                project.status, project.active_job_id = 'stopped', None
                project.error = '服务重启，任务已中断；请显式重试。'
                project.updated_at = now()

    def _require_providers(self):
        if self.provider_mode == 'unconfigured':
            messages = [
                getattr(provider, 'safe_message', '') for provider in (self.vision, self.image_edit)
            ]
            raise ServiceError(
                503,
                next(
                    (message for message in messages if message),
                    'NMM 模型能力未配置，请设置独立的视觉和图像编辑配置。',
                ),
            )

    def _request(self, project_id, request_id, parameters):
        if not isinstance(request_id, str) or not request_id.strip() or len(request_id) > 128:
            raise ServiceError(422, 'request_id 必须为 1–128 字符。')
        key = f'nmm:{project_id}:{sha256(request_id.encode()).hexdigest()[:32]}'
        with Session(self.engine) as db:
            existing = db.scalar(select(Job).where(Job.kind == 'nmm', Job.request_id == key))
            if existing:
                if existing.request_hash != digest(parameters):
                    raise ServiceError(409, 'request_id 已用于不同操作参数。')
                return key, self.jobs.snapshot(existing.id)
        return key, None

    def _revision(self, project, revision):
        if type(revision) is not int or revision != project.revision:
            raise ServiceError(409, '项目版本已改变，请刷新后重试。')

    def _generation_allowed(self, project, confirmation):
        self._require_providers()
        if confirmation is not True:
            raise ServiceError(422, '生成前需要明确确认预计调用次数。')
        if project.generation_count >= self.max_generations:
            raise ServiceError(409, '项目生成次数已达上限。')

    async def run(
        self,
        project_id,
        *,
        request_id,
        revision,
        confirm_generation,
        cost_authorization=None,
        quality_acknowledged=False,
    ):
        async with self.lock:
            parameters = {
                'operation': 'run',
                'cost_authorization': cost_authorization,
                'project_id': str(project_id),
                'revision': revision,
                'confirm_generation': confirm_generation,
                'quality_acknowledged': quality_acknowledged,
            }
            key, existing = self._request(project_id, request_id, parameters)
            with Session(self.engine) as db:
                project = self._project(db, project_id)
                if existing:
                    return existing
                self._revision(project, revision)
                if (
                    project.status not in ('draft', 'failed', 'stopped')
                    or project.active_job_id is not None
                ):
                    raise ServiceError(409, '项目不能重复启动；等待确认的项目请提交确认。')
                resume_selected, generated, analyzed = self._recovery_state(project)
                if resume_selected:
                    self._require_candidate_quality(
                        db, project, resume_selected, quality_acknowledged
                    )
                if analyzed and not resume_selected:
                    self._generation_allowed(project, confirm_generation)
                if resume_selected or ((generated or analyzed) and project.workflow_thread_id):
                    self._require_providers()
                    thread = project.workflow_thread_id
                    initial = None
                else:
                    self._generation_allowed(project, confirm_generation)
                    thread = f'nmm:{project_id}:{revision}:{uuid4()}'
                    initial = {
                        'project_id': str(project.id),
                        'revision': revision,
                        'params': deepcopy(project.params),
                        'attempt': 0,
                        'warnings': [],
                    }
            if self.provider_mode == 'live':
                if cost_authorization is not None:
                    self.billing.authorize(project_id, cost_authorization)
                self.billing.check_access(project_id, thread)
            return await self._submit(
                project_id, revision, key, parameters, thread, initial, selected=resume_selected
            )

    async def confirm(
        self,
        project_id,
        *,
        request_id,
        revision,
        action,
        candidate_id=None,
        params=None,
        confirm_generation=False,
        cost_authorization=None,
        quality_acknowledged=False,
    ):
        async with self.lock:
            parsed = NmmParams.model_validate(params).model_dump() if params is not None else None
            parameters = {
                'operation': 'confirm',
                'cost_authorization': cost_authorization,
                'project_id': str(project_id),
                'revision': revision,
                'action': action,
                'candidate_id': str(candidate_id) if candidate_id is not None else None,
                'params': parsed,
                'confirm_generation': confirm_generation,
                'quality_acknowledged': quality_acknowledged,
            }
            key, existing = self._request(project_id, request_id, parameters)
            with Session(self.engine) as db:
                project = self._project(db, project_id)
                if existing:
                    return existing
                self._revision(project, revision)
                if (
                    project.status != 'awaiting_confirmation'
                    or not project.workflow_thread_id
                    or project.active_job_id
                ):
                    raise ServiceError(409, '项目当前不在等待确认状态。')
                self._require_providers()
                thread = project.workflow_thread_id
                if action == 'select':
                    if parsed is not None or candidate_id is None:
                        raise ServiceError(422, '选择候选时必须提供候选编号且不能修改参数。')
                    try:
                        candidate_id = UUID(str(candidate_id))
                        NmmRepository(db)._asset(project, candidate_id, 'candidate')
                    except (ValueError, TypeError):
                        raise ServiceError(409, '候选不属于当前项目版本。') from None
                    self._require_candidate_quality(db, project, candidate_id, quality_acknowledged)
                    selected = str(candidate_id)
                elif action == 'regenerate':
                    self._generation_allowed(project, confirm_generation)
                    if candidate_id is not None:
                        raise ServiceError(422, '重新生成不能同时选择候选。')
                    selected = None
                else:
                    raise ServiceError(422, '未知确认操作。')
                changed = parsed is not None and parsed != project.params
            if self.provider_mode == 'live':
                if changed and cost_authorization is None:
                    raise ServiceError(409, '修改参数前请先估算新参数并批准调用额度。')
                if cost_authorization is not None:
                    self.billing.authorize(
                        project_id, cost_authorization, params=parsed if changed else None
                    )
            if changed:
                revision = self._new_revision(project_id, parsed)
                thread = f'nmm:{project_id}:{revision}:{uuid4()}'
                initial = {
                    'project_id': str(project_id),
                    'revision': revision,
                    'params': parsed,
                    'attempt': 0,
                    'warnings': [],
                }
            else:
                initial = Command(resume={'action': action, 'candidate_id': selected})
            if self.provider_mode == 'live':
                self.billing.check_access(project_id, thread)
            return await self._submit(
                project_id, revision, key, parameters, thread, initial, selected=selected
            )

    def _new_revision(self, project_id, params):
        written = []
        try:
            with Session(self.engine) as db, db.begin():
                project = self._project(db, project_id)
                assets = list(
                    db.scalars(
                        select(NmmAsset).where(
                            NmmAsset.project_id == project.id,
                            NmmAsset.revision == project.revision,
                            NmmAsset.role.in_(INPUT_ROLES),
                        )
                    )
                )
                project.revision += 1
                project.status, project.phase = 'draft', 'input'
                project.workflow_thread_id, project.active_job_id = None, None
                project.execution, project.warnings, project.error = {}, [], None
                project.params, project.analysis, project.selected_candidate_id = params, None, None
                repository = NmmRepository(db)
                snapshots = []
                for asset in assets:
                    from .storage import normalize_image

                    raw, kind = self.inputs.read_asset(asset.id)
                    stored = self.inputs.storage.put(project.id, normalize_image(raw, kind))
                    written.append((project.id, stored.name))
                    row = repository.add_asset(
                        project.id,
                        role=asset.role,
                        storage_name=stored.name,
                        content_type=stored.content_type,
                        size=stored.size,
                        width=stored.width,
                        height=stored.height,
                        sha256=stored.sha256,
                        sort_order=asset.sort_order,
                    )
                    snapshots.append({'id': str(row.id), 'role': row.role, 'sha256': row.sha256})
                project.input_snapshots = {
                    **project.input_snapshots,
                    str(project.revision): {'params': params, 'assets': snapshots},
                }
                return project.revision
        except BaseException:
            for identity, name in written:
                self.inputs.storage.delete(identity, name)
            raise

    async def _submit(
        self, project_id, revision, key, parameters, thread, initial, *, selected=None
    ):
        async def runner(context):
            return await self._execute(project_id, revision, thread, initial, context)

        holder = {}

        def cancelled():
            if 'id' in holder:
                self._terminal(
                    project_id, revision, holder['id'], 'stopped', '任务已停止，已完成的数据保留。'
                )

        job = await self.jobs.submit('nmm', key, parameters, runner, on_cancel=cancelled)
        holder['id'] = job['id']
        try:
            with Session(self.engine) as db, db.begin():
                project = self._project(db, project_id, lock=True)
                project.workflow_thread_id, project.active_job_id = thread, job['id']
                project.status, project.error = ('planning' if selected else 'generating'), None
                project.meta = {**project.meta, 'error_info': None}
                project.selected_candidate_id = UUID(selected) if selected else None
                project.execution = (
                    project.execution
                    if project.execution.get('thread') == thread
                    else {'thread': thread, 'stages': {}}
                )
                project.updated_at = now()
        except BaseException:
            await self.jobs.cancel(job['id'])
            raise
        return job

    def _guard(self, db, project_id, revision, job_id, thread):
        project = self._project(db, project_id)
        if (
            project.revision != revision
            or project.active_job_id != job_id
            or project.workflow_thread_id != thread
            or project.status not in ACTIVE
        ):
            raise ServiceError(409, '任务已停止或项目版本已改变。')
        return project

    def _terminal(self, project_id, revision, job_id, status, error=None):
        with Session(self.engine) as db, db.begin():
            project = db.get(NmmProject, UUID(str(project_id)))
            if project and project.revision == revision and project.active_job_id == job_id:
                project.status, project.active_job_id, project.error = status, None, error
                project.updated_at = now()

    async def _execute(self, project_id, revision, thread, initial, context):
        from .operations import WorkflowOperations

        operations = WorkflowOperations(self, project_id, revision, context.id, thread)
        try:
            # A resumed graph can finish directly from a checkpoint; check the
            # persisted selection even when no node is going to run.
            with Session(self.engine) as db:
                project = self._guard(db, project_id, revision, context.id, thread)
                if project.selected_candidate_id is not None:
                    self._require_candidate_quality(db, project, project.selected_candidate_id)
            async with self.checkpoint_store.open() as saver:
                graph = build_workflow(operations.stage, saver)
                if initial is None:
                    snapshot = await graph.aget_state({'configurable': {'thread_id': thread}})
                    if any(task.interrupts for task in snapshot.tasks):
                        with Session(self.engine) as db:
                            project = self._guard(db, project_id, revision, context.id, thread)
                            if project.selected_candidate_id is not None:
                                initial = Command(
                                    resume={
                                        'action': 'select',
                                        'candidate_id': str(project.selected_candidate_id),
                                    }
                                )
                result = await graph.ainvoke(
                    initial, {'configurable': {'thread_id': thread}, 'recursion_limit': 100}
                )
            status = 'awaiting_confirmation' if result.get('__interrupt__') else 'done'
            self._terminal(project_id, revision, context.id, status)
            return {'status': 'completed', 'project_id': str(project_id), 'project_status': status}
        except asyncio.CancelledError:
            self._terminal(
                project_id, revision, context.id, 'stopped', '任务已停止，已完成的数据保留。'
            )
            raise
        except (NmmProviderError, ServiceError) as error:
            message = str(error) if isinstance(error, NmmProviderError) else error.detail
            with Session(self.engine) as db, db.begin():
                project = db.scalar(
                    select(NmmProject)
                    .where(NmmProject.id == UUID(str(project_id)))
                    .with_for_update()
                )
                if project and project.active_job_id == context.id:
                    project.meta = {
                        **project.meta,
                        'error_info': error_record(error)
                        if isinstance(error, NmmProviderError)
                        else {'code': 'authorization', 'message': message, 'retryable': True},
                    }
            self._terminal(project_id, revision, context.id, 'failed', message)
            return {'status': 'failed', 'project_id': str(project_id)}
        except Exception:
            self._terminal(
                project_id,
                revision,
                context.id,
                'failed',
                'NMM 任务失败，请检查输入和模型配置；已有结果保留。',
            )
            return {'status': 'failed', 'project_id': str(project_id)}

    async def stop(self, project_id):
        async with self.lock:
            with Session(self.engine) as db:
                project = self._project(db, project_id)
                job_id, revision = project.active_job_id, project.revision
            if job_id is not None:
                await self.jobs.cancel(job_id)
                self._terminal(
                    project_id, revision, job_id, 'stopped', '任务已停止，已完成的数据保留。'
                )
            return self.detail(project_id)

    async def delete_project(self, project_id):
        async with self.lock:
            with Session(self.engine) as db:
                project = self._project(db, project_id)
                job_id = project.active_job_id
            if job_id is not None:
                await self.jobs.cancel(job_id)
            with Session(self.engine) as db, db.begin():
                project = self._project(db, project_id)
                assets = [
                    (row.project_id, row.storage_name)
                    for row in db.scalars(select(NmmAsset).where(NmmAsset.project_id == project.id))
                ]
                delete_project_checkpoints(db, project.id)
                NmmRepository(db).delete_project(project.id)
            for identity, name in assets:
                self.inputs.storage.delete(identity, name)
