"""Catalogue restore must never consume independent NMM work records."""

import pytest
from io import BytesIO
from zipfile import ZipFile
import json
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.job_models import Job
from app.main import create_app
from app.services.backups import BackupService
from app.nmm.models import NmmProject
from app.nmm.repository import NmmRepository
from pg_test_support import test_database_url


@pytest.fixture
def client(tmp_path):
    app = create_app(test_database_url(), tmp_path / 'attachments')
    with TestClient(app) as value:
        yield value


def add_job(client, status):
    with Session(client.app.state.engine) as db:
        job = Job(kind='nmm', request_id='nmm-phase-one', request_hash='a' * 64, status=status)
        db.add(job)
        db.commit()
        return job.id


@pytest.mark.parametrize('version', [1, 6])
@pytest.mark.parametrize('status', ['completed', 'stopped', 'failed'])
def test_restore_preserves_nmm_job(client, status, version):
    if version == 6:
        archive = client.get('/api/v1/backup').content
    else:
        stream = BytesIO()
        with ZipFile(stream, 'w') as content:
            content.writestr(
                'manifest.json',
                json.dumps(
                    {
                        'version': 1,
                        'sources': [],
                        'techniques': [],
                        'products': [],
                        'attachments': [],
                    }
                ),
            )
        archive = stream.getvalue()
    job_id = add_job(client, status)
    response = client.post(
        '/api/v1/backup', files={'file': ('backup.zip', archive, 'application/zip')}
    )
    assert response.status_code == 200, response.text
    with Session(client.app.state.engine) as db:
        job = db.get(Job, job_id)
        assert job is not None
        assert job.status == status
        assert job.request_id == 'nmm-phase-one'


@pytest.mark.parametrize('status', ['queued', 'running'])
def test_restore_rejects_active_nmm_job(client, status):
    archive = client.get('/api/v1/backup').content
    job_id = add_job(client, status)
    response = client.post(
        '/api/v1/backup', files={'file': ('backup.zip', archive, 'application/zip')}
    )
    assert response.status_code == 409
    with Session(client.app.state.engine) as db:
        assert db.get(Job, job_id).status == status


def test_direct_restore_service_also_checks_active_nmm(client, tmp_path):
    archive = client.get('/api/v1/backup').content
    add_job(client, 'running')
    upload = tmp_path / 'restore'
    upload.mkdir()
    (upload / 'backup.zip').write_bytes(archive)
    service = BackupService(
        client.app.state.engine, client.app.state.storage, client.app.state.backup_limit
    )
    with pytest.raises(HTTPException) as failure:
        service.restore_backup_archive(upload)
    assert failure.value.status_code == 409


@pytest.mark.parametrize('status', ['generating', 'planning', 'rendering'])
def test_active_project_blocks_restore_without_job(client, status):
    archive = client.get('/api/v1/backup').content
    with Session(client.app.state.engine) as db:
        project = NmmRepository(db).create_project('active')
        project.status = status
        db.commit()
    response = client.post(
        '/api/v1/backup', files={'file': ('backup.zip', archive, 'application/zip')}
    )
    assert response.status_code == 409


def test_waiting_project_survives_restore_and_is_not_exported(client, tmp_path):
    with Session(client.app.state.engine) as db:
        project = NmmRepository(db).create_project('waiting')
        project.status = 'awaiting_confirmation'
        project.workflow_thread_id = 'nmm-persistent-thread'
        db.commit()
        project_id = project.id
    project_dir = tmp_path / 'nmm' / str(project_id)
    project_dir.mkdir(parents=True)
    stored = project_dir / 'local-only.png'
    stored.write_bytes(b'local work')
    archive = client.get('/api/v1/backup').content
    with ZipFile(BytesIO(archive)) as content:
        manifest = json.loads(content.read('manifest.json'))
        assert not any(key.startswith('nmm') for key in manifest)
        assert not any(name.startswith('nmm/') for name in content.namelist())
    response = client.post(
        '/api/v1/backup', files={'file': ('backup.zip', archive, 'application/zip')}
    )
    assert response.status_code == 200, response.text
    with Session(client.app.state.engine) as db:
        project = db.get(NmmProject, project_id)
        assert project.status == 'awaiting_confirmation'
        assert project.workflow_thread_id == 'nmm-persistent-thread'
    assert stored.read_bytes() == b'local work'
