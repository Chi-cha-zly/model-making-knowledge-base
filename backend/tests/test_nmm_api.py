"""HTTP boundaries exercise real NMM services with synthetic, offline providers."""

import asyncio
import json
import time
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.main import create_app
from app.nmm.models import NmmProject
from nmm_fakes import OfflineVision, RecordingImageEdit, make_inputs, make_png
from pg_test_support import test_database_url

BASE = '/api/v1/nmm/projects'


@pytest.fixture
def client(tmp_path):
    app = create_app(test_database_url(), tmp_path / 'attachments')
    with TestClient(app) as value:
        yield value


@pytest.fixture
def offline(tmp_path):
    vision, image = OfflineVision(), RecordingImageEdit()
    app = create_app(
        test_database_url(),
        tmp_path / 'attachments',
        nmm_vision_provider=vision,
        nmm_image_provider=image,
    )
    with TestClient(app) as client:
        yield client, vision, image


def create(client, *, data=None, files=None):
    inputs = make_inputs()
    return client.post(
        BASE,
        data=data or {'title': 'Silver shield', 'params': '{}'},
        files=files
        or {key: (key + '.png', image.data, image.content_type) for key, image in inputs.items()},
    )


def project(client):
    response = create(client)
    assert response.status_code == 201, response.text
    return response.json()


def wait_project(client, project_id, status):
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        response = client.get(f'{BASE}/{project_id}')
        assert response.status_code == 200, response.text
        result = response.json()
        if result['status'] == status:
            return result
        assert result['status'] != 'failed', result
        time.sleep(0.02)
    pytest.fail(f'Project did not reach {status}: {result}')


def test_default_creation_assets_estimate_and_fail_closed(client, tmp_path, monkeypatch):
    monkeypatch.setenv('NMM_FAKE', 'true')
    item = project(client)
    assert item['revision'] == 1 and item['status'] == 'draft'
    assert item['provider_mode'] == 'unconfigured'
    assert str(tmp_path) not in json.dumps(item)
    assert [row['id'] for row in client.get(BASE).json()] == [item['id']]
    estimate = client.get(f'{BASE}/{item["id"]}/estimate').json()
    assert estimate['provider_mode'] == 'unconfigured'
    assert estimate['generation_count'] == 0
    original = next(asset for asset in item['assets'] if asset['role'] == 'original')
    response = client.get('/api/v1/nmm/assets/' + original['id'])
    assert response.status_code == 200 and response.headers['content-type'] == 'image/png'
    assert response.headers['x-content-type-options'] == 'nosniff'
    response = client.post(
        f'{BASE}/{item["id"]}/run',
        json={
            'request_id': 'closed',
            'revision': 1,
            'confirm_generation': True,
        },
    )
    assert response.status_code == 503
    assert client.delete(f'{BASE}/{item["id"]}').status_code == 204
    assert client.get(f'{BASE}/{item["id"]}').status_code == 404
    assert client.get('/api/v1/nmm/assets/' + original['id']).status_code == 404


@pytest.mark.parametrize(
    'data',
    [
        {'title': '  ', 'params': '{}'},
        {'title': 'x' * 301, 'params': '{}'},
        {'title': 'x', 'params': '{'},
        {'title': 'x', 'params': '{"step_count":"7"}'},
        {'title': 'x', 'params': '{"unknown":true}'},
    ],
)
def test_creation_rejects_invalid_text_without_writing(client, data):
    assert create(client, data=data).status_code == 422
    assert client.get(BASE).json() == []


def test_reference_roles_lengths_limits_and_bad_image(client):
    source = [
        ('original', ('x.png', make_png(), 'image/png')),
        ('mask', ('m.png', make_png((255, 255, 255)), 'image/png')),
    ]
    reference = ('references[]', ('r.png', make_png(), 'image/png'))
    assert create(client, files=source + [reference]).status_code == 422
    response = create(
        client,
        data={'title': 'x', 'roles[]': ['reference_style'] * 4},
        files=source + [reference] * 4,
    )
    assert response.status_code == 422
    response = create(
        client, files={'original': ('bad.png', b'bad', 'image/png'), 'mask': source[1][1]}
    )
    assert response.status_code == 422
    response = create(
        client,
        data={'title': 'references', 'roles[]': ['reference_steps']},
        files=source + [reference],
    )
    assert response.status_code == 201, response.text
    assert len(response.json()['assets']) == 3


@pytest.mark.parametrize(
    'headers', [{'origin': 'https://other.example'}, {'sec-fetch-site': 'cross-site'}]
)
def test_cross_site_mutations_are_rejected_before_parsing(client, headers):
    for path in [
        BASE,
        f'{BASE}/{uuid4()}/run',
        f'{BASE}/{uuid4()}/confirm',
        f'{BASE}/{uuid4()}/stop',
    ]:
        assert client.post(path, content=b'invalid', headers=headers).status_code == 403
    assert client.delete(f'{BASE}/{uuid4()}', headers=headers).status_code == 403


@pytest.mark.parametrize(
    'patch',
    [
        {'revision': True},
        {'revision': '1'},
        {'revision': 0},
        {'confirm_generation': 'true'},
        {'request_id': ''},
        {'request_id': ' '},
        {'request_id': 'x' * 129},
        {'unexpected': 1},
    ],
)
def test_run_strict_json_validation(client, patch):
    data = {'request_id': 'x', 'revision': 1, 'confirm_generation': True} | patch
    assert client.post(f'{BASE}/{uuid4()}/run', json=data).status_code == 422


def test_invalid_uuid_and_unknown_asset(client):
    assert client.get(f'{BASE}/not-uuid').status_code == 422
    assert client.get(f'{BASE}/{uuid4()}').status_code == 404
    assert client.get('/api/v1/nmm/assets/' + str(uuid4())).status_code == 404


def test_upload_and_json_limits(client, monkeypatch):
    from app.api import nmm

    monkeypatch.setattr(nmm, 'MAX_IMAGE_BYTES', 64)
    assert create(client).status_code == 413
    assert client.get(BASE).json() == []
    assert (
        client.post(f'{BASE}/{uuid4()}/run', content=b' ' * (nmm.MAX_JSON_BYTES + 1)).status_code
        == 413
    )


def test_real_workflow_pause_sse_select_steps_and_files(offline):
    client, vision, image = offline
    item = project(client)
    assert vision.calls == [] and image.calls == []
    data = {'request_id': 'start', 'revision': 1, 'confirm_generation': True}
    first = client.post(f'{BASE}/{item["id"]}/run', json=data)
    assert first.status_code == 200, first.text
    assert client.post(f'{BASE}/{item["id"]}/run', json=data).json()['id'] == first.json()['id']
    paused = wait_project(client, item['id'], 'awaiting_confirmation')
    events = client.get(f'{BASE}/{item["id"]}/events')
    assert events.status_code == 200
    assert 'event: snapshot' in events.text and 'event: progress' in events.text
    assert 'awaiting_confirmation' in events.text
    candidate = next(asset for asset in paused['assets'] if asset['role'] == 'candidate')
    selected = client.post(
        f'{BASE}/{item["id"]}/confirm',
        json={
            'request_id': 'select',
            'revision': 1,
            'action': 'select',
            'candidate_id': candidate['id'],
        },
    )
    assert selected.status_code == 200, selected.text
    done = wait_project(client, item['id'], 'done')
    assert len(done['steps']) == 7 and done['plan']
    for asset in done['assets']:
        assert client.get('/api/v1/nmm/assets/' + asset['id']).status_code == 200
    assert image.calls == [0]
    assert client.delete(f'{BASE}/{item["id"]}').status_code == 204


def test_stop_cancels_provider_and_restore_guard(offline):
    client, _, image = offline
    image.block = True
    item = project(client)
    backup = client.get('/api/v1/backup').content
    response = client.post(
        f'{BASE}/{item["id"]}/run',
        json={
            'request_id': 'blocked',
            'revision': 1,
            'confirm_generation': True,
        },
    )
    assert response.status_code == 200, response.text
    assert (
        client.post(
            '/api/v1/backup', files={'file': ('b.zip', backup, 'application/zip')}
        ).status_code
        == 409
    )
    assert client.post(f'{BASE}/{item["id"]}/stop').json()['status'] == 'stopped'
    assert (
        client.post(
            '/api/v1/backup', files={'file': ('b.zip', backup, 'application/zip')}
        ).status_code
        == 200
    )
    assert client.get(f'{BASE}/{item["id"]}').json()['status'] == 'stopped'


def test_startup_recovers_running_and_preserves_waiting(tmp_path):
    url = test_database_url()
    first = create_app(url, tmp_path / 'attachments')
    with TestClient(first) as client:
        running, waiting = project(client), project(client)
        with Session(first.state.engine) as db:
            db.get(NmmProject, UUID(running['id'])).status = 'generating'
            db.get(NmmProject, UUID(waiting['id'])).status = 'awaiting_confirmation'
            db.commit()
    second = create_app(url, tmp_path / 'attachments')
    with TestClient(second) as client:
        assert client.get(f'{BASE}/{running["id"]}').json()['status'] == 'stopped'
        assert client.get(f'{BASE}/{waiting["id"]}').json()['status'] == 'awaiting_confirmation'


def test_sse_disconnect_returns_without_polling(client):
    from app.api.nmm import project_events

    item = project(client)

    class Disconnected:
        app = client.app

        async def is_disconnected(self):
            return True

    async def exercise():
        response = await project_events(UUID(item['id']), Disconnected())
        assert [chunk async for chunk in response.body_iterator] == []

    asyncio.run(exercise())


def test_sse_heartbeats_and_sanitizes_midstream_failure(client, monkeypatch):
    from app.api.nmm import project_events

    item = project(client)
    project_id = UUID(item['id'])
    with Session(client.app.state.engine) as db:
        db.get(NmmProject, project_id).status = 'generating'
        db.commit()

    class Connection:
        app = client.app
        polls = 0

        async def is_disconnected(self):
            self.polls += 1
            return self.polls > 2

    async def heartbeat():
        response = await project_events(project_id, Connection())
        chunks = [chunk async for chunk in response.body_iterator]
        assert any(chunk.startswith(': keep-alive') for chunk in chunks)

    asyncio.run(heartbeat())
    original = client.app.state.nmm.detail
    calls = 0

    def unavailable(value):
        nonlocal calls
        calls += 1
        if calls > 1:
            raise RuntimeError('/private/secret/provider-token')
        return original(value)

    monkeypatch.setattr(client.app.state.nmm, 'detail', unavailable)

    async def failure():
        response = await project_events(project_id, Connection())
        text = ''.join([chunk async for chunk in response.body_iterator])
        assert 'event: error' in text and 'secret' not in text and 'private' not in text

    asyncio.run(failure())


def test_restore_waits_for_nmm_write_lock(client):
    import httpx

    archive = client.get('/api/v1/backup').content

    async def exercise():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=client.app), base_url='http://testserver'
        ) as http:
            async with client.app.state.nmm.lock:
                restore = asyncio.create_task(
                    http.post(
                        '/api/v1/backup',
                        files={
                            'file': ('backup.zip', archive, 'application/zip'),
                        },
                    )
                )
                await asyncio.sleep(0.05)
                assert not restore.done(), 'restore bypassed the NMM write lock'
            assert (await asyncio.wait_for(restore, 5)).status_code == 200

    client.portal.call(exercise)


def test_chunked_multipart_limit_does_not_trust_content_length(client, monkeypatch):
    from app.api import nmm

    monkeypatch.setattr(nmm, 'MAX_IMAGE_BYTES', 1)
    headers = {'content-type': 'multipart/form-data; boundary=x', 'content-length': '1'}
    response = client.post(
        BASE,
        headers=headers,
        content=iter(
            [
                b'--x\r\nContent-Disposition: form-data; name="original"; filename="x.png"\r\n\r\n'
                + b'x' * 40000,
                b'x' * 40000,
            ]
        ),
    )
    assert response.status_code == 413
    assert client.get(BASE).json() == []


def test_confirm_rejects_cross_project_candidate_and_stale_revision(offline):
    client, _, image = offline
    first, second = project(client), project(client)
    assert (
        client.post(
            f'{BASE}/{first["id"]}/run',
            json={
                'request_id': 'candidate',
                'revision': 1,
                'confirm_generation': True,
            },
        ).status_code
        == 200
    )
    paused = wait_project(client, first['id'], 'awaiting_confirmation')
    candidate = next(asset for asset in paused['assets'] if asset['role'] == 'candidate')
    data = {
        'request_id': 'select',
        'revision': 1,
        'action': 'select',
        'candidate_id': candidate['id'],
    }
    assert client.post(f'{BASE}/{second["id"]}/confirm', json=data).status_code in (409, 422)
    assert (
        client.post(f'{BASE}/{first["id"]}/confirm', json=data | {'revision': 2}).status_code == 409
    )
    assert (
        client.post(f'{BASE}/{first["id"]}/confirm', json=data | {'revision': True}).status_code
        == 422
    )
    assert image.calls == [0]


def test_explicit_checkpoint_injection_and_shutdown_cancellation(tmp_path):
    from nmm_fakes import MemoryCheckpointStore

    checkpoint = MemoryCheckpointStore()
    image = RecordingImageEdit(block=True)
    app = create_app(
        test_database_url(),
        tmp_path / 'attachments',
        nmm_vision_provider=OfflineVision(),
        nmm_image_provider=image,
        nmm_checkpoint_store=checkpoint,
    )
    assert app.state.nmm.checkpoint_store is checkpoint
    with TestClient(app) as client:
        item = project(client)
        response = client.post(
            f'{BASE}/{item["id"]}/run',
            json={
                'request_id': 'shutdown',
                'revision': 1,
                'confirm_generation': True,
            },
        )
        assert response.status_code == 200, response.text

        async def started():
            await asyncio.wait_for(image.started.wait(), 5)

        client.portal.call(started)
    assert app.state.jobs.tasks == {}
    assert app.state.nmm.detail(item['id'])['status'] == 'stopped'
    app.state.engine.dispose()


def test_environment_cannot_enable_fake_provider(tmp_path, monkeypatch):
    monkeypatch.setenv('NMM_FAKE', 'true')
    monkeypatch.setenv('NMM_PROVIDER_MODE', 'offline_test')
    app = create_app(test_database_url(), tmp_path / 'attachments')
    with TestClient(app) as client:
        assert project(client)['provider_mode'] == 'unconfigured'


def test_openapi_documents_multipart_and_workflow_request_fields(client):
    paths = client.get('/openapi.json').json()['paths']
    create_schema = paths[BASE]['post']['requestBody']['content']['multipart/form-data']['schema']
    assert set(create_schema['required']) == {'title', 'original', 'mask'}
    assert create_schema['properties']['original']['format'] == 'binary'
    assert create_schema['properties']['references']['items']['format'] == 'binary'
    assert create_schema['properties']['roles']['items']['enum'] == [
        'reference_style',
        'reference_palette',
        'reference_steps',
    ]
    run_schema = paths[BASE + '/{project_id}/run']['post']['requestBody']['content'][
        'application/json'
    ]['schema']
    assert set(run_schema['required']) == {'request_id', 'revision', 'confirm_generation'}
    assert run_schema['properties']['confirm_generation']['type'] == 'boolean'
    confirm_schema = paths[BASE + '/{project_id}/confirm']['post']['requestBody']['content'][
        'application/json'
    ]['schema']
    assert confirm_schema['properties']['action']['enum'] == ['select', 'regenerate']
    assert 'candidate_id' in confirm_schema['properties']
    assert 'step_count' in json.dumps(confirm_schema['properties']['params'])
    assert '$ref' not in json.dumps(confirm_schema) and '$defs' not in confirm_schema


def test_proposed_estimate_and_authorization_are_documented_and_strict(client):
    item = project(client)
    path = f'{BASE}/{item["id"]}/estimate'
    response = client.post(path, json={'params': {'metal': 'gold'}})
    assert response.status_code == 200, response.text
    assert client.get(f'{BASE}/{item["id"]}').json()['params']['metal'] == 'silver'
    assert client.post(path, json={'params': {'step_count': '7'}}).status_code == 422
    assert client.post(path, json={'unknown': 1}).status_code == 422
    assert client.post(path, json={}, headers={'sec-fetch-site': 'cross-site'}).status_code == 403
    paths = client.get('/openapi.json').json()['paths']
    for action in ('run', 'confirm'):
        schema = paths[BASE + '/{project_id}/' + action]['post']['requestBody']['content'][
            'application/json'
        ]['schema']
        assert 'cost_authorization' in schema['properties']
        assert 'max_image_calls' in json.dumps(schema['properties']['cost_authorization'])
        assert '$ref' not in json.dumps(schema)


def test_live_api_refuses_unapproved_and_malformed_authorization(tmp_path):
    import httpx
    from app.nmm.provider_config import build_nmm_providers

    allowed, requests = [False], []

    def forbidden(request):
        assert allowed[0], 'Unapproved API invocation reached a provider'
        requests.append(request)
        return httpx.Response(
            429,
            json={
                'error': {
                    'code': 'rate_limit_exceeded',
                    'message': 'synthetic-secret-provider-error',
                }
            },
        )

    vision, image = build_nmm_providers(
        {
            'VISION_BASE_URL': 'https://dashscope.aliyuncs.com/compatible-mode/v1',
            'VISION_MODEL': 'qwen3-vl-plus',
            'VISION_API_KEY': 'synthetic-vision',
            'IMAGE_BASE_URL': 'https://api.openai.com/v1',
            'IMAGE_MODEL': 'gpt-image-2.5-sunburst',
            'IMAGE_API_KEY': 'synthetic-image',
        },
        transport=httpx.MockTransport(forbidden),
    )
    app = create_app(
        test_database_url(),
        tmp_path / 'attachments',
        nmm_vision_provider=vision,
        nmm_image_provider=image,
    )
    with TestClient(app) as client:
        item = project(client)
        data = {'request_id': 'no-approval', 'revision': 1, 'confirm_generation': True}
        response = client.post(f'{BASE}/{item["id"]}/run', json=data)
        assert response.status_code == 409
        quote = client.get(f'{BASE}/{item["id"]}/estimate').json()
        assert quote['requires_authorization'] is True and quote['estimate_id']
        approval = {
            'estimate_id': quote['estimate_id'],
            'max_image_calls': 1,
            'max_vision_calls': 1,
            'accept_unknown_cost': True,
        }
        for patch in (
            {'max_image_calls': True},
            {'max_vision_calls': '1'},
            {'accept_unknown_cost': 'true'},
            {'unexpected': 1},
        ):
            response = client.post(
                f'{BASE}/{item["id"]}/run',
                json=data
                | {
                    'cost_authorization': approval | patch,
                },
            )
            assert response.status_code == 422
        response = client.post(
            f'{BASE}/{item["id"]}/run',
            json=data
            | {
                'cost_authorization': approval | {'accept_unknown_cost': False},
            },
        )
        assert response.status_code == 422
        assert client.get(f'{BASE}/{item["id"]}').json()['meta']['usage']['calls'] == []
        assert requests == []
        allowed[0] = True
        approved = data | {'request_id': 'approved', 'cost_authorization': approval}
        first = client.post(f'{BASE}/{item["id"]}/run', json=approved)
        assert first.status_code == 200, first.text
        failed = wait_project(client, item['id'], 'failed')
        assert failed['meta']['usage']['reserved_vision_calls'] == 1
        assert failed['meta']['usage']['reserved_image_calls'] == 0
        assert 'synthetic-secret-provider-error' not in json.dumps(failed)
        assert (
            client.post(f'{BASE}/{item["id"]}/run', json=approved).json()['id']
            == first.json()['id']
        )
        assert len(requests) == 1
