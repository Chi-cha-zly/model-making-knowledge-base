import asyncio
from fastapi.testclient import TestClient
from test_chat import setup


class JobProvider:
    configured = True
    model = 'chat-test'
    embedding_configured = True
    embedding_model = 'embedding-test'
    settings = {}


def test_job_start_requires_configuration_apply_and_local_origin(tmp_path):
    app = setup(tmp_path, JobProvider())
    with TestClient(app) as client:
        payload = {'kind': 'embedding', 'request_id': 'one', 'apply': True}
        assert (
            client.post(
                '/api/v1/jobs', json=payload, headers={'Origin': 'https://evil.invalid'}
            ).status_code
            == 403
        )
        assert (
            client.post(
                '/api/v1/jobs', json={k: v for k, v in payload.items() if k != 'apply'}
            ).status_code
            == 422
        )
        assert (
            client.post('/api/v1/jobs', json={**payload, 'max_documents': True}).status_code == 422
        )
        assert client.post('/api/v1/jobs', json={**payload, 'kind': 'vision'}).status_code == 503
        response = client.post('/api/v1/jobs', json=payload)
        assert response.status_code == 202
        row = response.json()
        assert client.post('/api/v1/jobs', json=payload).json()['id'] == row['id']
        assert client.post('/api/v1/jobs', json={**payload, 'max_documents': 10}).status_code == 409
        assert client.get('/api/v1/jobs').json()[0]['kind'] == 'embedding'
        assert (
            client.post(
                f'/api/v1/jobs/{row["id"]}/stop', headers={'Origin': 'https://evil.invalid'}
            ).status_code
            == 403
        )


def test_jobs_submission_obeys_restore_lifecycle_lock(tmp_path):
    import threading

    app = setup(tmp_path, JobProvider())
    with TestClient(app) as client:
        client.portal.call(app.state.chat.lock.acquire)
        responses = []
        attempted = threading.Event()

        def submit():
            attempted.set()
            responses.append(
                client.post(
                    '/api/v1/jobs',
                    json={'kind': 'embedding', 'request_id': 'locked', 'apply': True},
                )
            )

        worker = threading.Thread(target=submit)
        worker.start()
        assert attempted.wait(1)
        # Wait for the request to enter the event loop while the restore lock stays held.
        client.portal.call(asyncio.sleep, 0.1)
        assert responses == []
        assert app.state.jobs.history() == []
        client.portal.call(app.state.chat.lock.release)
        worker.join(3)
        assert not worker.is_alive()
        assert responses[0].status_code == 202


def test_vision_job_honors_explicit_local_ocr_engine(tmp_path, monkeypatch):
    import app.vision_jobs
    import app.services.ocr

    class VisionProvider(JobProvider):
        vision_configured = True
        settings = {'OCR_ENGINE': 'tesseract', 'OCR_TESSDATA_DIR': '/local/languages'}

    selected = []
    monkeypatch.setattr(app.services.ocr, 'local_ocr', lambda **kw: selected.append(kw) or object())

    async def describe(*a, **kw):
        return {'documents': [], 'status': 'completed'}

    monkeypatch.setattr(app.vision_jobs, 'describe_pending', describe)
    app = setup(tmp_path, VisionProvider())
    with TestClient(app) as client:
        assert (
            client.post(
                '/api/v1/jobs', json={'kind': 'vision', 'request_id': 'explicit-ocr', 'apply': True}
            ).status_code
            == 202
        )
        client.portal.call(asyncio.sleep, 0.05)
    assert selected == [{'engine': 'tesseract', 'tessdata_dir': '/local/languages'}]
