import asyncio

import pytest
from sqlalchemy import create_engine

from app.jobs import JobService
from app.nmm.workflow_service import NmmWorkflowService
from app.services.errors import ServiceError
from nmm_fakes import OfflineVision, RecordingImageEdit, make_inputs, wait_job
from pg_test_support import test_database_url


def service(tmp_path, **kwargs):
    tmp_path.mkdir(parents=True, exist_ok=True)
    engine = create_engine(test_database_url())
    jobs = JobService(engine)
    return NmmWorkflowService(
        engine,
        tmp_path / 'attachments',
        jobs,
        vision=kwargs.pop('vision', OfflineVision()),
        image_edit=kwargs.pop('image_edit', RecordingImageEdit()),
        **kwargs,
    )


def test_full_flow_real_postgres_pause_and_recreated_service_resume(tmp_path):
    async def exercise():
        first = service(tmp_path)
        project = first.create_project(title='shield', **make_inputs())
        job = await first.run(
            project['id'], request_id='start', revision=1, confirm_generation=True
        )
        assert (await wait_job(first.jobs, job))['status'] == 'completed'
        paused = first.detail(project['id'])
        assert paused['status'] == 'awaiting_confirmation' and paused['active_job_id'] is None
        candidate = next(a for a in paused['assets'] if a['role'] == 'candidate')
        second = NmmWorkflowService(
            first.engine,
            tmp_path / 'attachments',
            JobService(first.engine),
            vision=OfflineVision(),
            image_edit=RecordingImageEdit(),
        )
        second.jobs.recover()
        second.recover()
        assert second.detail(project['id'])['status'] == 'awaiting_confirmation'
        job = await second.confirm(
            project['id'],
            request_id='select',
            revision=1,
            action='select',
            candidate_id=candidate['id'],
        )
        assert (await wait_job(second.jobs, job))['status'] == 'completed'
        completed = second.detail(project['id'])
        assert completed['status'] == 'done' and completed['steps'] and completed['plan']
        assert completed['generation_count'] == 1 and completed['warnings']
        assert second.image_edit.calls == []
        assert [name for name, _ in second.vision.calls] == ['plan', 'review']
        await second.delete_project(project['id'])
        assert second.list_projects() == []

    asyncio.run(exercise())


def test_defaults_fail_closed_and_generation_confirmation(tmp_path):
    async def exercise():
        engine = create_engine(test_database_url())
        default = NmmWorkflowService(engine, tmp_path / 'attachments', JobService(engine))
        project = default.create_project(title='closed', **make_inputs())
        assert default.estimate(project['id'])['provider_mode'] == 'unconfigured'
        with pytest.raises(ServiceError) as error:
            await default.run(project['id'], request_id='x', revision=1, confirm_generation=True)
        assert error.value.status_code == 503
        ready = service(tmp_path / 'other')
        project = ready.create_project(title='confirm', **make_inputs())
        with pytest.raises(ServiceError) as error:
            await ready.run(project['id'], request_id='x', revision=1, confirm_generation=False)
        assert error.value.status_code == 422

    asyncio.run(exercise())


def test_idempotency_regeneration_version_and_total_generation_limit(tmp_path):
    async def exercise():
        item = service(tmp_path, max_generations=3)
        project = item.create_project(title='versions', **make_inputs())
        first = await item.run(project['id'], request_id='one', revision=1, confirm_generation=True)
        assert (
            await item.run(project['id'], request_id='one', revision=1, confirm_generation=True)
            == first
        )
        await wait_job(item.jobs, first)
        old = item.detail(project['id'])
        job = await item.confirm(
            project['id'],
            request_id='two',
            revision=1,
            action='regenerate',
            confirm_generation=True,
        )
        await wait_job(item.jobs, job)
        assert item.detail(project['id'])['workflow_thread_id'] == old['workflow_thread_id']
        job = await item.confirm(
            project['id'],
            request_id='three',
            revision=1,
            action='regenerate',
            params={'metal': 'gold'},
            confirm_generation=True,
        )
        # The same successful request remains idempotent even after its own revision change.
        again = await item.confirm(
            project['id'],
            request_id='three',
            revision=1,
            action='regenerate',
            params={'metal': 'gold'},
            confirm_generation=True,
        )
        assert again['id'] == job['id']
        await wait_job(item.jobs, job)
        current = item.detail(project['id'])
        assert current['revision'] == 2 and current['generation_count'] == 3
        assert current['workflow_thread_id'] != old['workflow_thread_id']
        assert len(current['input_snapshots']) == 2
        with pytest.raises(ServiceError):
            await item.confirm(
                project['id'],
                request_id='old',
                revision=2,
                action='select',
                candidate_id=old['candidate_ids'][0],
            )
        with pytest.raises(ServiceError):
            await item.confirm(
                project['id'],
                request_id='four',
                revision=2,
                action='regenerate',
                confirm_generation=True,
            )
        with pytest.raises(ServiceError):
            await item.run(project['id'], request_id='one', revision=2, confirm_generation=True)

    asyncio.run(exercise())


def test_provider_failure_consumes_attempt_without_leaking_errors(tmp_path):
    async def exercise():
        image = RecordingImageEdit(fail=True)
        item = service(tmp_path, image_edit=image, max_generations=2)
        project = item.create_project(title='fail', **make_inputs())
        for index in range(2):
            job = await item.run(
                project['id'], request_id=str(index), revision=1, confirm_generation=True
            )
            assert (await wait_job(item.jobs, job))['status'] == 'failed'
            detail = item.detail(project['id'])
            assert detail['status'] == 'failed' and detail['generation_count'] == index + 1
            assert 'secret' not in detail['error']
        with pytest.raises(ServiceError):
            await item.run(project['id'], request_id='limit', revision=1, confirm_generation=True)
        assert len(image.calls) == 2

    asyncio.run(exercise())


def test_stop_concurrent_run_and_explicit_generation_retry(tmp_path):
    async def exercise():
        image = RecordingImageEdit(block=True)
        item = service(tmp_path, image_edit=image)
        project = item.create_project(title='stop', **make_inputs())
        job = await item.run(project['id'], request_id='one', revision=1, confirm_generation=True)
        await asyncio.wait_for(image.started.wait(), 10)
        same = await item.run(project['id'], request_id='one', revision=1, confirm_generation=True)
        assert same['id'] == job['id']
        with pytest.raises(ServiceError):
            await item.run(project['id'], request_id='two', revision=1, confirm_generation=True)
        stopped = await item.stop(project['id'])
        assert stopped['status'] == 'stopped' and stopped['generation_count'] == 1
        old_thread = stopped['workflow_thread_id']
        assert (await item.stop(project['id']))['status'] == 'stopped'
        image.release.set()
        retry = await item.run(
            project['id'], request_id='retry', revision=1, confirm_generation=True
        )
        await wait_job(item.jobs, retry)
        detail = item.detail(project['id'])
        assert detail['status'] == 'awaiting_confirmation' and detail['generation_count'] == 2
        assert detail['workflow_thread_id'] != old_thread

    asyncio.run(exercise())


def test_selected_review_stop_resumes_same_real_checkpoint_without_new_generation(tmp_path):
    import json

    class BlockingReview(OfflineVision):
        def __init__(self):
            super().__init__()
            self.started = asyncio.Event()

        async def analyze(self, images, prompt, schema):
            if json.loads(prompt)['task'] == 'review':
                self.started.set()
                await asyncio.Event().wait()
            return await super().analyze(images, prompt, schema)

    async def exercise():
        vision = BlockingReview()
        item = service(tmp_path, vision=vision)
        project = item.create_project(title='resume review', **make_inputs())
        first = await item.run(project['id'], request_id='one', revision=1, confirm_generation=True)
        await wait_job(item.jobs, first)
        paused = item.detail(project['id'])
        await item.confirm(
            project['id'],
            request_id='select',
            revision=1,
            action='select',
            candidate_id=paused['candidate_ids'][0],
        )
        await asyncio.wait_for(vision.started.wait(), 10)
        stopped = await item.stop(project['id'])
        assert stopped['status'] == 'stopped' and stopped['steps']
        second = NmmWorkflowService(
            item.engine,
            tmp_path / 'attachments',
            JobService(item.engine),
            vision=OfflineVision(),
            image_edit=RecordingImageEdit(),
        )
        job = await second.run(
            project['id'], request_id='resume', revision=1, confirm_generation=False
        )
        await wait_job(second.jobs, job)
        completed = second.detail(project['id'])
        assert completed['status'] == 'done'
        assert completed['workflow_thread_id'] == stopped['workflow_thread_id']
        assert completed['generation_count'] == 1 and len(completed['assets']) == len(
            stopped['assets']
        )
        assert second.image_edit.calls == [] and [task for task, _ in second.vision.calls] == [
            'review'
        ]

    asyncio.run(exercise())


def test_bad_plan_retries_three_times_then_color_only_fallback_and_reference_isolation(tmp_path):
    from app.nmm.providers import ImageInput
    from nmm_fakes import make_png

    async def exercise():
        vision, image = OfflineVision(bad_plans=3), RecordingImageEdit()
        item = service(tmp_path, vision=vision, image_edit=image)
        refs = [
            (role, ImageInput(make_png(), 'image/png'))
            for role in ('reference_style', 'reference_palette', 'reference_steps')
        ]
        project = item.create_project(title='fallback', references=refs, **make_inputs())
        first = await item.run(project['id'], request_id='one', revision=1, confirm_generation=True)
        await wait_job(item.jobs, first)
        paused = item.detail(project['id'])
        selected = await item.confirm(
            project['id'],
            request_id='select',
            revision=1,
            action='select',
            candidate_id=paused['candidate_ids'][0],
        )
        await wait_job(item.jobs, selected)
        done = item.detail(project['id'])
        assert done['status'] == 'done' and done['plan']['plan']['fallback'] is True
        assert all(
            set(step['description']) == {'index', 'color_hex', 'area_ratio'}
            for step in done['steps']
        )
        assert [task for task, _ in vision.calls].count('plan') == 3
        assert vision.calls[0] == ('analysis', 2) and image.calls == [2]
        assert any('三次' in warning for warning in done['warnings'])

    asyncio.run(exercise())


def test_successful_plan_keeps_full_teaching_contract(tmp_path):
    async def exercise():
        item = service(tmp_path)
        project = item.create_project(title='teaching', **make_inputs())
        first = await item.run(project['id'], request_id='one', revision=1, confirm_generation=True)
        await wait_job(item.jobs, first)
        candidate = item.detail(project['id'])['candidate_ids'][0]
        selected = await item.confirm(
            project['id'], request_id='select', revision=1, action='select', candidate_id=candidate
        )
        await wait_job(item.jobs, selected)
        done = item.detail(project['id'])
        assert done['plan']['plan']['overall_tips']
        required = {
            'color_description',
            'areas',
            'technique',
            'expected_result',
            'change_from_previous',
            'common_mistakes',
            'needs_confirmation',
        }
        assert all(required <= set(step['description']) for step in done['steps'])

    asyncio.run(exercise())


def test_revision_submit_failure_cannot_leave_old_checkpoint_resumable(tmp_path, monkeypatch):
    async def exercise():
        item = service(tmp_path)
        project = item.create_project(title='revision gap', **make_inputs())
        first = await item.run(
            project['id'], request_id='start', revision=1, confirm_generation=True
        )
        await wait_job(item.jobs, first)
        original_submit = item.jobs.submit

        async def broken(*args, **kwargs):
            raise RuntimeError('simulated job persistence failure')

        monkeypatch.setattr(item.jobs, 'submit', broken)
        with pytest.raises(RuntimeError):
            await item.confirm(
                project['id'],
                request_id='revision',
                revision=1,
                action='regenerate',
                params={'metal': 'gold'},
                confirm_generation=True,
            )
        detail = item.detail(project['id'])
        assert detail['revision'] == 2 and detail['status'] == 'draft'
        assert detail['workflow_thread_id'] is None and detail['execution'] == {}
        assert detail['generation_count'] == 1
        with pytest.raises(ServiceError):
            await item.confirm(
                project['id'],
                request_id='select',
                revision=2,
                action='select',
                candidate_id=detail['input_snapshots']['1']['assets'][0]['id'],
            )
        monkeypatch.setattr(item.jobs, 'submit', original_submit)
        retry = await item.run(
            project['id'], request_id='retry', revision=2, confirm_generation=True
        )
        await wait_job(item.jobs, retry)
        assert item.detail(project['id'])['status'] == 'awaiting_confirmation'

    asyncio.run(exercise())


def test_algorithm_area_overrides_model_area_and_review_receives_plan(tmp_path):
    import json

    class MisleadingAreaVision(OfflineVision):
        async def analyze(self, images, prompt, schema):
            request = json.loads(prompt)
            if request['task'] == 'review':
                self.review_plan = request['plan']
            result = await super().analyze(images, prompt, schema)
            if request['task'] == 'plan':
                for step in result.steps:
                    step.area_ratio = 0.123456
            return result

    async def exercise():
        vision = MisleadingAreaVision()
        item = service(tmp_path, vision=vision)
        project = item.create_project(title='real area', **make_inputs())
        first = await item.run(
            project['id'], request_id='start', revision=1, confirm_generation=True
        )
        await wait_job(item.jobs, first)
        selected = await item.confirm(
            project['id'],
            request_id='select',
            revision=1,
            action='select',
            candidate_id=item.detail(project['id'])['candidate_ids'][0],
        )
        await wait_job(item.jobs, selected)
        detail = item.detail(project['id'])
        assert all(step['description']['area_ratio'] != 0.123456 for step in detail['steps'])
        assert vision.review_plan == detail['plan']['plan']

    asyncio.run(exercise())


def test_completed_generation_business_checkpoint_gap_reuses_candidates(tmp_path, monkeypatch):
    from app.nmm.operations import WorkflowOperations

    async def exercise():
        item = service(tmp_path)
        project = item.create_project(title='generation gap', **make_inputs())
        original_write = WorkflowOperations.write
        injected = False

        def crash_after_committed_generate(self, key, output, **kwargs):
            nonlocal injected
            result = original_write(self, key, output, **kwargs)
            if key.startswith('generate:') and not injected:
                injected = True
                raise RuntimeError('simulated crash before graph checkpoint persisted')
            return result

        monkeypatch.setattr(WorkflowOperations, 'write', crash_after_committed_generate)
        first = await item.run(
            project['id'], request_id='start', revision=1, confirm_generation=True
        )
        assert (await wait_job(item.jobs, first))['status'] == 'failed'
        persisted = item.detail(project['id'])
        assert persisted['candidate_ids'] and persisted['generation_count'] == 1
        retry = await item.run(
            project['id'], request_id='recover', revision=1, confirm_generation=False
        )
        assert (await wait_job(item.jobs, retry))['status'] == 'completed'
        restored = item.detail(project['id'])
        assert restored['status'] == 'awaiting_confirmation'
        assert restored['workflow_thread_id'] == persisted['workflow_thread_id']
        assert restored['candidate_ids'] == persisted['candidate_ids']
        assert restored['generation_count'] == 1 and len(item.image_edit.calls) == 1

    asyncio.run(exercise())
