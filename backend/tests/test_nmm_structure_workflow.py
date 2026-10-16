"""Workflow guard regressions using isolated PostgreSQL and offline models only."""

import asyncio
from io import BytesIO
from uuid import UUID

import numpy as np
import pytest
from PIL import Image
from sqlalchemy import select, func
from sqlalchemy.orm import Session

from app.job_models import Job
from app.nmm.models import NmmAsset, NmmProject
from app.nmm.providers import ImageInput
from app.services.errors import ServiceError
from nmm_fakes import MemoryCheckpointStore, wait_job
from test_nmm_structure import structural_fixture
from test_nmm_workflow import service


def image_input(array):
    out = BytesIO()
    Image.fromarray(array).save(out, 'PNG')
    return ImageInput(out.getvalue(), 'image/png')


class RemovedParts:
    async def edit(self, image, mask, references, prompt, n):
        with Image.open(BytesIO(image.data)) as decoded:
            return [
                image_input(np.full((decoded.height, decoded.width, 3), 30, dtype=np.uint8)).data
            ]


async def paused_project(tmp_path, *, destructive=False):
    from nmm_fakes import make_inputs

    kwargs = {'checkpoint_store': MemoryCheckpointStore()}
    if destructive:
        kwargs['image_edit'] = RemovedParts()
    item = service(tmp_path, **kwargs)
    if destructive:
        original, mask = structural_fixture()
        inputs = {
            'original': image_input(original),
            'mask': image_input(mask.astype(np.uint8) * 255),
        }
    else:
        inputs = make_inputs()
    project = item.create_project(title='structural guard', **inputs)
    job = await item.run(project['id'], request_id='start', revision=1, confirm_generation=True)
    assert (await wait_job(item.jobs, job))['status'] == 'completed'
    return item, item.detail(project['id'])


def test_candidate_review_requires_ack_and_ack_is_in_idempotency_hash(tmp_path):
    async def exercise():
        item, paused = await paused_project(tmp_path)
        candidate = paused['candidate_ids'][0]
        assert paused['candidate_checks'][candidate]['status'] == 'needs_review'
        arguments = dict(request_id='select', revision=1, action='select', candidate_id=candidate)
        with pytest.raises(ServiceError) as error:
            await item.confirm(paused['id'], **arguments)
        assert error.value.status_code == 422
        job = await item.confirm(paused['id'], **arguments, quality_acknowledged=True)
        assert (await wait_job(item.jobs, job))['status'] == 'completed'
        assert (await item.confirm(paused['id'], **arguments, quality_acknowledged=True))[
            'id'
        ] == job['id']
        with pytest.raises(ServiceError) as error:
            await item.confirm(paused['id'], **arguments, quality_acknowledged=False)
        assert error.value.status_code == 409

    asyncio.run(exercise())


def test_blocked_legacy_checks_recompute_read_only_and_fail_before_billing_or_job(
    tmp_path, monkeypatch
):
    async def exercise():
        item, paused = await paused_project(tmp_path, destructive=True)
        candidate = paused['candidate_ids'][0]
        with Session(item.engine) as db, db.begin():
            asset = db.get(NmmAsset, UUID(candidate))
            assert asset.meta['quality_check']['status'] == 'blocked'
            asset.meta = {'quality_check': {'version': 'obsolete', 'status': 'needs_review'}}
        detail = item.detail(paused['id'])
        assert detail['candidate_checks'][candidate]['status'] == 'blocked'
        with Session(item.engine) as db:
            assert db.get(NmmAsset, UUID(candidate)).meta == {
                'quality_check': {'version': 'obsolete', 'status': 'needs_review'}
            }
            before = db.scalar(select(func.count()).select_from(Job))
        item.image_edit.mode = 'live'
        monkeypatch.setattr(
            item.billing,
            'authorize',
            lambda *a, **kw: pytest.fail('Must not bill blocked candidate'),
        )
        with pytest.raises(ServiceError) as error:
            await item.confirm(
                paused['id'],
                request_id='blocked',
                revision=1,
                action='select',
                candidate_id=candidate,
                quality_acknowledged=True,
            )
        assert error.value.status_code == 409
        with Session(item.engine) as db:
            assert db.scalar(select(func.count()).select_from(Job)) == before
            project = db.get(NmmProject, UUID(paused['id']))
            assert project.selected_candidate_id is None
        assert len(item.vision.calls) == 1

    asyncio.run(exercise())


def test_selected_legacy_resume_cannot_skip_quality_review(tmp_path):
    async def exercise():
        item, paused = await paused_project(tmp_path)
        with Session(item.engine) as db, db.begin():
            project = db.get(NmmProject, UUID(paused['id']))
            project.selected_candidate_id = UUID(paused['candidate_ids'][0])
            project.status = 'stopped'
        with pytest.raises(ServiceError) as error:
            await item.run(paused['id'], request_id='resume', revision=1, confirm_generation=False)
        assert error.value.status_code == 422
        job = await item.run(
            paused['id'],
            request_id='resume',
            revision=1,
            confirm_generation=False,
            quality_acknowledged=True,
        )
        assert (await wait_job(item.jobs, job))['status'] == 'completed'
        assert len(item.image_edit.calls) == 1

    asyncio.run(exercise())


def test_generation_filters_cached_legacy_analysis_before_image_call(tmp_path):
    import json
    from nmm_fakes import OfflineVision, RecordingImageEdit, make_inputs

    class Hallucinated(OfflineVision):
        async def analyze(self, images, prompt, schema):
            result = await super().analyze(images, prompt, schema)
            if json.loads(prompt)['task'] == 'analysis':
                assert len(images) == 3
                assert json.loads(prompt)['mask_scope']['coverage'] == 1
                result.summary = 'invented knees'
                result.reflection_hints = ['invented knees']
            return result

    class Inspecting(RecordingImageEdit):
        async def edit(self, image, mask, references, prompt, n):
            assert 'invented knees' not in prompt
            return await super().edit(image, mask, references, prompt, n)

    async def exercise():
        item = service(
            tmp_path,
            vision=Hallucinated(),
            image_edit=Inspecting(),
            checkpoint_store=MemoryCheckpointStore(),
        )
        project = item.create_project(title='grounding', **make_inputs())
        job = await item.run(project['id'], request_id='start', revision=1, confirm_generation=True)
        assert (await wait_job(item.jobs, job))['status'] == 'completed'

    asyncio.run(exercise())


def test_blocked_selection_is_rejected_on_resume_and_before_cached_stage(tmp_path):
    from app.nmm.operations import WorkflowOperations

    async def exercise():
        item, paused = await paused_project(tmp_path, destructive=True)
        identity = UUID(paused['id'])
        selected = paused['candidate_ids'][0]
        with Session(item.engine) as db, db.begin():
            project = db.get(NmmProject, identity)
            project.selected_candidate_id = UUID(selected)
            project.status = 'stopped'
        with pytest.raises(ServiceError) as error:
            await item.run(
                paused['id'],
                request_id='blocked-resume',
                revision=1,
                confirm_generation=False,
                quality_acknowledged=True,
            )
        assert error.value.status_code == 409
        with Session(item.engine) as db, db.begin():
            project = db.get(NmmProject, identity)
            job_id = db.scalar(select(Job.id).where(Job.kind == 'nmm'))
            project.active_job_id, project.status = job_id, 'rendering'
            project.execution = {
                **project.execution,
                'stages': {'palette:1': {'status': 'completed', 'output': {'palette': []}}},
            }
        operations = WorkflowOperations(item, identity, 1, job_id, paused['workflow_thread_id'])
        with pytest.raises(ServiceError) as error:
            await operations.stage('palette', {'selected_candidate_id': selected, 'attempt': 1})
        assert error.value.status_code == 409
        assert len(item.vision.calls) == 1

    asyncio.run(exercise())


def test_cached_quality_still_checks_asset_integrity(tmp_path):
    async def exercise():
        item, paused = await paused_project(tmp_path)
        candidate = paused['candidate_ids'][0]
        assert paused['candidate_checks'][candidate]['status'] == 'needs_review'
        with Session(item.engine) as db:
            asset = db.get(NmmAsset, UUID(candidate))
            path = item.inputs.storage.root / str(asset.project_id) / asset.storage_name
        path.write_bytes(b'corrupt synthetic candidate')
        assert item.detail(paused['id'])['candidate_checks'][candidate]['status'] == 'blocked'
        with pytest.raises(ServiceError):
            await item.confirm(
                paused['id'],
                request_id='tampered',
                revision=1,
                action='select',
                candidate_id=candidate,
                quality_acknowledged=True,
            )

    asyncio.run(exercise())


def test_wrong_size_provider_candidate_is_not_stretched(tmp_path):
    from nmm_fakes import make_inputs, make_png

    class WrongSize:
        async def edit(self, *args):
            return [make_png(size=(18, 9))]

    async def exercise():
        item = service(tmp_path, image_edit=WrongSize(), checkpoint_store=MemoryCheckpointStore())
        project = item.create_project(title='wrong canvas', **make_inputs())
        job = await item.run(project['id'], request_id='start', revision=1, confirm_generation=True)
        assert (await wait_job(item.jobs, job))['status'] == 'failed'
        assert item.detail(project['id'])['candidate_ids'] == []

    asyncio.run(exercise())


def test_old_unlocated_analysis_is_filtered_again_during_generation(tmp_path):
    import json
    from app.nmm.operations import WorkflowOperations
    from nmm_fakes import RecordingImageEdit
    from test_nmm_structure import analysis, surface

    class Capturing(RecordingImageEdit):
        async def edit(self, image, mask, references, prompt, n):
            self.prompt = json.loads(prompt)
            return await super().edit(image, mask, references, prompt, n)

    async def exercise():
        item, paused = await paused_project(tmp_path)
        item.image_edit = Capturing()
        with Session(item.engine) as db, db.begin():
            project = db.get(NmmProject, UUID(paused['id']))
            job_id = db.scalar(select(Job.id).where(Job.kind == 'nmm'))
            project.active_job_id, project.status = job_id, 'generating'
        operations = WorkflowOperations(item, paused['id'], 1, job_id, paused['workflow_thread_id'])
        legacy = surface()
        del legacy['bbox']
        del legacy['confidence']
        del legacy['reflection_hint']
        output = await operations.stage(
            'generate',
            {
                'attempt': 1,
                'params': paused['params'],
                'analysis': analysis([legacy]),
                'warnings': [],
            },
        )
        assert item.image_edit.prompt['analysis']['surfaces'] == []
        assert 'invented knees' not in json.dumps(item.image_edit.prompt)
        assert any('未用于生成' in warning for warning in output['warnings'])

    asyncio.run(exercise())
