"""Result archives contain only current rendered teaching outputs, never project backups."""

from io import BytesIO
import json
from uuid import UUID, uuid4
from zipfile import ZipFile

from fastapi.testclient import TestClient
import pytest
from sqlalchemy.orm import Session

from app.main import create_app
from app.nmm.models import NmmProject
from nmm_fakes import OfflineVision, RecordingImageEdit
from pg_test_support import test_database_url
from test_nmm_api import create, wait_project

BASE = '/api/v1/nmm/projects'


@pytest.fixture
def client(tmp_path):
    app = create_app(
        test_database_url(),
        tmp_path / 'attachments',
        nmm_vision_provider=OfflineVision(),
        nmm_image_provider=RecordingImageEdit(),
    )
    with TestClient(app) as value:
        yield value


def completed(client):
    response = create(
        client,
        data={'title': '../../unsafe-title', 'params': json.dumps({'notes': 'private-input-note'})},
    )
    assert response.status_code == 201
    item = response.json()
    result = client.post(
        f'{BASE}/{item["id"]}/run',
        json={'request_id': 'run', 'revision': 1, 'confirm_generation': True},
    )
    assert result.status_code == 200
    item = wait_project(client, item['id'], 'awaiting_confirmation')
    result = client.post(
        f'{BASE}/{item["id"]}/confirm',
        json={
            'request_id': 'select',
            'revision': 1,
            'action': 'select',
            'quality_acknowledged': True,
            'candidate_id': item['candidate_ids'][0],
        },
    )
    assert result.status_code == 200
    return wait_project(client, item['id'], 'done')


def test_empty_and_missing_result_exports_are_explicit(client):
    item = create(client).json()
    assert client.get(f'{BASE}/{item["id"]}/export').status_code == 409
    assert client.get(f'{BASE}/{uuid4()}/export').status_code == 404


def test_result_zip_fixed_names_teaching_content_and_private_exclusion(client):
    item = completed(client)
    with Session(client.app.state.engine) as db, db.begin():
        row = db.get(NmmProject, UUID(item['id']))
        row.meta = {**row.meta, 'private_marker': 'credential-and-cache-marker'}
        row.execution = {**row.execution, 'private_marker': 'execution-cache-marker'}
    response = client.get(f'{BASE}/{item["id"]}/export')
    assert response.status_code == 200, response.text
    assert response.headers['content-type'] == 'application/zip'
    assert response.headers['content-disposition'] == 'attachment; filename="nmm-results.zip"'
    with ZipFile(BytesIO(response.content)) as archive:
        names = set(archive.namelist())
        expected = {'result.json', 'final.png', 'contact-sheet.png'}
        expected |= {f'step-{index:02d}.png' for index in range(1, len(item['steps']) + 1)}
        expected |= {f'diff-{index:02d}.png' for index in range(1, len(item['steps']) + 1)}
        assert names == expected and all('/' not in name and '..' not in name for name in names)
        output = archive.read('result.json')
        assert (
            b'credential-and-cache-marker' not in output and b'execution-cache-marker' not in output
        )
        assert (
            b'private-input-note' not in output
            and b'storage_name' not in output
            and b'input_snapshots' not in output
        )
        data = json.loads(output)
        assert data['kind'] == 'nmm_results' and data['revision'] == 1
        assert len(data['steps']) == len(item['steps'])
        assert data['steps'][0]['description']['technique']
        final_raw = client.get('/api/v1/nmm/assets/' + item['steps'][-1]['image_asset_id']).content
        assert archive.read('final.png') == final_raw
    assert all(
        value['role']
        not in (
            'original',
            'mask',
            'reference_style',
            'reference_palette',
            'reference_steps',
            'candidate',
        )
        for value in data.get('assets', [])
    )


def test_current_revision_and_active_state_reject_exports(client):
    item = completed(client)
    with Session(client.app.state.engine) as db, db.begin():
        row = db.get(NmmProject, UUID(item['id']))
        row.status = 'rendering'
    assert client.get(f'{BASE}/{item["id"]}/export').status_code == 409
    with Session(client.app.state.engine) as db, db.begin():
        row = db.get(NmmProject, UUID(item['id']))
        row.status, row.revision, row.selected_candidate_id = 'draft', 2, None
    assert client.get(f'{BASE}/{item["id"]}/export').status_code == 409
    assert client.get(BASE).json()[0]['thumbnail_asset_id'] is None


def test_list_thumbnail_and_explicit_single_image_download(client):
    item = create(client).json()
    original = next(asset for asset in item['assets'] if asset['role'] == 'original')
    assert client.get(BASE).json()[0]['thumbnail_asset_id'] == original['id']
    assert item['thumbnail_asset_id'] == original['id']
    response = client.get('/api/v1/nmm/assets/' + original['id'] + '?download=1')
    assert response.status_code == 200
    assert (
        response.headers['content-disposition']
        == f'attachment; filename="nmm-asset-{original["id"]}.png"'
    )
    assert response.headers['cache-control'] == 'no-store'
    assert 'content-disposition' not in client.get('/api/v1/nmm/assets/' + original['id']).headers


def test_export_rejects_missing_or_mismatched_output_files(client):
    item = completed(client)
    asset = next(
        asset for asset in item['assets'] if asset['id'] == item['steps'][0]['image_asset_id']
    )
    with Session(client.app.state.engine) as db:
        from app.nmm.models import NmmAsset

        row = db.get(NmmAsset, UUID(asset['id']))
        client.app.state.nmm.inputs.storage.delete(row.project_id, row.storage_name)
    assert client.get(f'{BASE}/{item["id"]}/export').status_code == 404


def test_result_export_limits_and_closes_temporary_file_before_error(client, monkeypatch):
    import app.nmm.export as module

    item = completed(client)
    files = []
    factory = module.SpooledTemporaryFile

    def capture(*args, **kwargs):
        value = factory(*args, **kwargs)
        files.append(value)
        return value

    monkeypatch.setattr(module, 'SpooledTemporaryFile', capture)
    monkeypatch.setattr(module, 'MAX_TOTAL_BYTES', 16)
    response = client.get(f'{BASE}/{item["id"]}/export')
    assert response.status_code == 413
    assert files and all(value.closed for value in files)


def test_cross_project_asset_reference_is_rejected_instead_of_exported(client):
    from app.nmm.models import NmmStep

    first, other = completed(client), completed(client)
    with Session(client.app.state.engine) as db, db.begin():
        step = db.get(NmmStep, UUID(first['steps'][0]['id']))
        step.image_asset_id = UUID(other['steps'][0]['image_asset_id'])
    assert client.get(f'{BASE}/{first["id"]}/export').status_code == 409


def test_completed_thumbnail_prefers_current_final_output(client):
    item = completed(client)
    chosen = client.get(BASE).json()[0]['thumbnail_asset_id']
    final_ids = {asset['id'] for asset in item['assets'] if asset['role'] == 'final'}
    assert chosen in final_ids and item['thumbnail_asset_id'] == chosen


@pytest.mark.parametrize('fail_after', [0, 1])
def test_export_disconnect_closes_archive_before_or_during_body(monkeypatch, fail_after):
    import asyncio
    from types import SimpleNamespace
    from app.api import nmm
    from app.nmm.export import ResultArchive

    archive = ResultArchive(BytesIO(b'x' * 100_000), 100_000)
    monkeypatch.setattr(nmm, 'build_results_archive', lambda *args: archive)
    response = nmm.export_project(
        uuid4(), SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(nmm=None)))
    )
    sent = 0

    async def send(message):
        nonlocal sent
        if sent == fail_after:
            raise OSError('disconnected')
        sent += 1

    async def receive():
        return {'type': 'http.disconnect'}

    async def exercise():
        with pytest.raises(Exception):
            await response({'type': 'http', 'asgi': {'spec_version': '2.4'}}, receive, send)

    asyncio.run(exercise())
    assert archive.file.closed
