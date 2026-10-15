"""Index writes must be explicit and stay inside the caller's transaction."""

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app import ingestion
from app.ingestion_models import Chunk, Document, IngestionJob
from app.models import Source, Technique
from app.services.ingestion import IngestionService
from pg_test_support import test_database_url


def test_plain_orm_commit_has_no_index_side_effects():
    engine = create_engine(test_database_url())
    with Session(engine) as db:
        row = Technique(title='普通 ORM 写入', sources=[Source(title='来源')])
        db.add(row)
        db.commit()
        assert db.scalar(select(func.count()).select_from(Document)) == 0
        row.summary = '普通 ORM 更新'
        db.commit()
        assert db.scalar(select(func.count()).select_from(IngestionJob)) == 0


def test_explicit_reindex_flushes_without_committing_and_rollback_is_complete():
    engine = create_engine(test_database_url())
    with Session(engine) as db:
        row = Technique(title='显式入库', sources=[Source(title='来源')])
        db.add(row)
        document = IngestionService(db).reindex(row)
        assert document.active_version == 1
        assert db.scalar(select(func.count()).select_from(Chunk)) > 0
        with Session(engine) as observer:
            assert observer.scalar(select(func.count()).select_from(Technique)) == 0
            assert observer.scalar(select(func.count()).select_from(Document)) == 0
        db.rollback()
    with Session(engine) as db:
        assert db.scalar(select(func.count()).select_from(Source)) == 0
        assert db.scalar(select(func.count()).select_from(Document)) == 0


def test_strict_reindex_failure_rolls_back_catalog_and_keeps_old_active(monkeypatch):
    engine = create_engine(test_database_url())
    with Session(engine) as db:
        row = Technique(title='原始标题', summary='原始正文', sources=[Source(title='来源')])
        db.add(row)
        service = IngestionService(db)
        document = service.reindex(row)
        db.commit()
        old_id, row_id = document.id, row.id

        def fail(_):
            raise ValueError('private failure')

        monkeypatch.setattr(ingestion, 'chunk_record', fail)
        row.summary = '不能发布的正文'
        with pytest.raises(ValueError, match='旧版本仍可检索'):
            service.reindex(row, strict=True)
        db.rollback()
    with Session(engine) as db:
        assert db.get(Technique, row_id).summary == '原始正文'
        assert db.get(Document, old_id).active_version == 1
        assert db.scalar(select(func.count()).select_from(IngestionJob)) == 1


def test_backfill_is_explicit_idempotent_and_does_not_commit():
    engine = create_engine(test_database_url())
    with Session(engine) as db:
        db.add(Technique(title='旧资料', sources=[Source(title='来源')]))
        db.commit()
        service = IngestionService(db)
        assert service.backfill() == 1
        assert service.backfill() == 1
        assert db.scalar(select(func.count()).select_from(IngestionJob)) == 1
        db.rollback()
        assert db.scalar(select(func.count()).select_from(Document)) == 0
        assert service.backfill(missing_only=True) == 1
        db.commit()
        assert service.backfill(missing_only=True) == 0


def test_catalog_restore_and_retry_explicitly_index_each_record_once(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import create_app

    app = create_app(test_database_url(), tmp_path / 'images', runtime_settings={})
    with TestClient(app) as client:
        calls = []
        real = IngestionService.reindex

        def tracked(service, row, **kwargs):
            calls.append(
                (type(row).__name__, row.title if isinstance(row, Technique) else row.name)
            )
            return real(service, row, **kwargs)

        monkeypatch.setattr(IngestionService, 'reindex', tracked)
        sid = client.post('/api/v1/sources', json={'title': 'source'}).json()['id']
        assert calls == []
        technique = client.post(
            '/api/v1/techniques', json={'title': 'article', 'source_ids': [sid]}
        ).json()
        assert len(calls) == 1
        client.patch(
            f'/api/v1/techniques/{technique["id"]}', json={'summary': 'updated'}
        ).raise_for_status()
        assert len(calls) == 2
        product = client.post(
            '/api/v1/products', json={'name': 'paint', 'aliases': ['blue'], 'source_ids': [sid]}
        ).json()
        assert len(calls) == 3
        client.patch(
            f'/api/v1/products/{product["id"]}', json={'aliases': ['cyan']}
        ).raise_for_status()
        assert len(calls) == 4
        client.post(
            '/api/v1/imports/markdown',
            files={'file': ('article.md', b'# imported\nbody', 'text/markdown')},
        ).raise_for_status()
        assert len(calls) == 5
        with Session(app.state.engine) as db:
            did = db.scalar(
                select(Document.id).where(Document.record_key == f'technique:{technique["id"]}')
            )
        retry = client.post(f'/api/v1/ingestion/documents/{did}/retry')
        retry.raise_for_status()
        assert retry.json()['version'] == 3 and len(calls) == 6
        archive = client.get('/api/v1/backup').content
        calls.clear()
        client.post(
            '/api/v1/backup', files={'file': ('backup.zip', archive, 'application/zip')}
        ).raise_for_status()
        assert len(calls) == 3
        with Session(app.state.engine) as db:
            assert db.scalar(select(func.count()).select_from(IngestionJob)) == 3
            assert set(db.scalars(select(Document.active_version))) == {1}
        calls.clear()
        result = client.post('/api/v1/ingestion/backfill')
        result.raise_for_status()
        assert result.json() == {'indexed': 3, 'embeddings': 'pending'}
        assert len(calls) == 3


def test_catalog_unexpected_index_failure_rolls_back_the_write(monkeypatch):
    from app.repositories.catalog import CatalogRepository
    from app.services.catalog import CatalogService
    from app.schemas import TechniquePatch

    engine = create_engine(test_database_url())
    with Session(engine) as db:
        row = Technique(title='article', summary='kept', sources=[Source(title='source')])
        db.add(row)
        IngestionService(db).reindex(row)
        db.commit()
        record_id = row.id

        def fail(service, record, **kwargs):
            service.session.flush()
            raise RuntimeError('index storage failed')

        monkeypatch.setattr(IngestionService, 'reindex', fail)
        with pytest.raises(RuntimeError, match='index storage failed'):
            CatalogService(CatalogRepository(db)).patch_technique(
                record_id, TechniquePatch(summary='lost')
            )
        assert db.get(Technique, record_id).summary == 'kept'
        assert db.scalar(select(Document)).active_version == 1


def test_legacy_copy_explicitly_indexes_and_rolls_back_strict_failure(tmp_path, monkeypatch):
    import sqlite3
    from scripts.migrate_legacy_sqlite import copy_legacy_database
    from app.ingestion_models import Embedding

    source = tmp_path / 'legacy.db'
    with sqlite3.connect(source) as db:
        db.execute(
            'CREATE TABLE techniques(id INTEGER PRIMARY KEY, title TEXT, steps JSON, tags JSON, owner_id TEXT, visibility TEXT, status TEXT)'
        )
        db.execute(
            "INSERT INTO techniques VALUES(1,'legacy','[\"preserved text\"]','[]','local','private','published')"
        )
    original = source.read_bytes()
    images = tmp_path / 'original-images'
    images.mkdir()
    target = test_database_url('successful copy')
    result = copy_legacy_database(source, images, target, tmp_path / 'new-images', apply=True)
    assert result['indexed_documents'] == 1
    with Session(create_engine(target)) as db:
        assert db.scalar(select(Document)).active_version == 1
        assert db.scalar(select(Embedding)).status == 'pending'
    failed_target = test_database_url('failed copy')

    def fail(_):
        raise ValueError('parser failure')

    monkeypatch.setattr(ingestion, 'chunk_record', fail)
    with pytest.raises(ValueError, match='旧版本仍可检索'):
        copy_legacy_database(source, images, failed_target, tmp_path / 'failed-images', apply=True)
    with Session(create_engine(failed_target)) as db:
        assert db.scalar(select(func.count()).select_from(Technique)) == 0
        assert db.scalar(select(func.count()).select_from(Document)) == 0
    assert source.read_bytes() == original
    assert not (tmp_path / 'failed-images').exists()
