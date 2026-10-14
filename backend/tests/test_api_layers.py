"""API dependency boundaries must not retain a closed-over database or storage."""

from pg_test_support import test_database_url
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.main import create_app
from app.models import Source


def test_catalog_routes_resolve_database_from_app_state(tmp_path):
    app = create_app(test_database_url(f'{tmp_path / "original.db"}'), tmp_path / 'uploads')
    replacement = create_engine(test_database_url(f'{tmp_path / "replacement.db"}'))
    with Session(replacement) as db:
        db.add(Source(title='Replacement database', source_type='manual'))
        db.commit()
    with TestClient(app) as client:
        app.state.engine = replacement
        response = client.get('/api/v1/sources')
        assert response.status_code == 200
        assert [source['title'] for source in response.json()] == ['Replacement database']
        created = client.post('/api/v1/sources', json={'title': 'Written through dependency'})
        assert created.status_code == 201
    with Session(replacement) as db:
        assert db.get(Source, created.json()['id']).title == 'Written through dependency'
    replacement.dispose()


def test_attachment_routes_resolve_storage_from_app_state(tmp_path):
    original = tmp_path / 'original'
    replacement = tmp_path / 'replacement'
    replacement.mkdir()
    app = create_app(test_database_url(f'{tmp_path / "test.db"}'), original)
    raw = b'\x89PNG\r\n\x1a\n' + b'fixture'
    with TestClient(app) as client:
        app.state.storage = replacement
        response = client.post(
            '/api/v1/attachments', files={'file': ('photo.png', raw, 'image/png')}
        )
        assert response.status_code == 201
        assert len(list(replacement.glob('*.png'))) == 1
        assert list(original.glob('*.png')) == []
        assert client.get(response.json()['url']).content == raw


def test_application_lifespan_releases_database_pool(tmp_path):
    app = create_app(test_database_url(str(tmp_path)), tmp_path / 'uploads')
    with TestClient(app) as client:
        assert client.get('/api/v1/sources').status_code == 200
        assert app.state.engine.pool.checkedin() > 0
    assert app.state.engine.pool.checkedin() == 0
