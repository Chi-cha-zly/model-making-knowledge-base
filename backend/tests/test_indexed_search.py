from app.services.ingestion import IngestionService
from pg_test_support import test_database_url
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session
from app.main import create_app
from app import retrieval


def test_indexed_search_long_tail_update_withdraw_restore_and_no_full_scan(tmp_path, monkeypatch):
    app = create_app(
        test_database_url(f'{tmp_path / "test.db"}'), tmp_path / 'files', runtime_settings={}
    )
    with TestClient(app) as client:
        sid = client.post('/api/v1/sources', json={'title': '真实出处'}).json()['id']
        row = client.post(
            '/api/v1/techniques',
            json={
                'title': '长篇',
                'summary': '普通步骤。' * 1300 + '\n\n稀有靛蓝罩染',
                'source_ids': [sid],
            },
        ).json()

        def forbidden(*args, **kwargs):
            raise AssertionError('online retrieval loaded the entire corpus')

        monkeypatch.setattr(retrieval, 'load_documents', forbidden)
        monkeypatch.setattr('app.main.load_documents', forbidden, raising=False)
        hits = client.get('/api/v1/search', params={'q': '靛蓝罩染'}).json()
        assert hits['techniques'][0]['id'] == row['id']
        backup = client.get('/api/v1/backup').content
        client.patch(f'/api/v1/techniques/{row["id"]}', json={'summary': '铜绿腐蚀'})
        assert client.get('/api/v1/search', params={'q': '靛蓝罩染'}).json()['ranking'] == []
        assert client.get('/api/v1/search', params={'q': '铜绿腐蚀'}).json()['ranking']
        client.post(
            '/api/v1/backup', files={'file': ('backup.zip', backup, 'application/zip')}
        ).raise_for_status()
        assert client.get('/api/v1/search', params={'q': '靛蓝罩染'}).json()['ranking']
        assert client.get('/api/v1/search', params={'q': '铜绿腐蚀'}).json()['ranking'] == []
        client.delete(f'/api/v1/sources/{sid}')
        assert client.get('/api/v1/search', params={'q': '靛蓝罩染'}).json()['ranking'] == []


def test_image_chunk_keeps_attachment_reference(tmp_path):
    from app.ingestion_models import Chunk

    app = create_app(
        test_database_url(f'{tmp_path / "test.db"}'), tmp_path / 'files', runtime_settings={}
    )
    with TestClient(app) as client:
        sid = client.post('/api/v1/sources', json={'title': '图例'}).json()['id']
        aid = client.post(
            '/api/v1/attachments', files={'file': ('a.png', b'\x89PNG\r\n\x1a\nxx', 'image/png')}
        ).json()['id']
        client.post(
            '/api/v1/techniques',
            json={
                'title': '图像',
                'source_ids': [sid],
                'content_blocks': [
                    {'type': 'image', 'attachment_id': aid, 'caption': 'OCR原文：边缘高光'}
                ],
            },
        ).raise_for_status()
        assert client.get('/api/v1/search', params={'q': '边缘高光'}).json()['ranking']
        with Session(app.state.engine) as db:
            chunk = db.scalar(select(Chunk).where(Chunk.attachment_id == aid))
            assert chunk is not None and '边缘高光' in chunk.content


def test_startup_indexes_legacy_rows_once(tmp_path):
    from sqlalchemy import create_engine
    from app.models import Source, Technique
    from app.ingestion_models import IngestionJob

    url = test_database_url(f'{tmp_path / "legacy.db"}')
    engine = create_engine(url)
    # Simulate rows written by the pre-ingestion release without ORM hooks.
    with engine.begin() as connection:
        connection.execute(Source.__table__.insert().values(id=1, title='旧出处'))
        connection.execute(Technique.__table__.insert().values(id=1, title='历史铜绿教程'))
        from app.models import technique_sources

        connection.execute(technique_sources.insert().values(technique_id=1, source_id=1))
    app = create_app(url, tmp_path / 'files', runtime_settings={})
    with TestClient(app) as client:
        assert client.get('/api/v1/search', params={'q': '铜绿'}).json()['ranking'] == [
            'technique:1'
        ]
    with TestClient(app):
        with Session(engine) as db:
            assert len(list(db.scalars(select(IngestionJob)))) == 1


def test_candidates_rank_before_limit_and_chat_excerpt_contains_tail(tmp_path):
    from app.models import Technique, Source
    from app.chat import evidence

    app = create_app(
        test_database_url(f'{tmp_path / "bounded.db"}'), tmp_path / 'files', runtime_settings={}
    )
    with Session(app.state.engine) as db:
        source = Source(title='当前出处', url='https://example.org/current')
        db.add_all(
            [Technique(title=f'背景笔记{i}', summary='蓝色', sources=[source]) for i in range(205)]
        )
        target = Technique(
            title='罕见金属示例',
            summary='准备工序。' * 1600 + '\n钴蓝反光金属罩染',
            sources=[source],
        )
        db.add(target)
        IngestionService(db).backfill()
        db.commit()
        hits = retrieval.search_documents(db, '钴蓝反光金属罩染', k=6)
        assert hits[0]['id'] == target.id
        assert '钴蓝反光金属罩染' in hits[0]['excerpt']
        assert '钴蓝反光金属罩染' in evidence(db, '钴蓝反光金属罩染')[0]['excerpt']
        source.title = '更新出处'
        source.url = 'https://example.org/new'
        db.commit()
        assert (
            retrieval.search_documents(db, '钴蓝反光金属罩染')[0]['sources'][0]['title']
            == '更新出处'
        )


def test_postgres_ingestion_versions_tail_and_restore(tmp_path, monkeypatch):
    """Destructive restore coverage; PG_TEST_DATABASE_URL must be disposable."""
    import uuid
    from app import ingestion
    from app.ingestion_models import Document, IngestionJob, Embedding

    url = test_database_url()
    app = create_app(url, tmp_path / 'pg-files', runtime_settings={})
    with TestClient(app) as client:
        sid = client.post(
            '/api/v1/sources', json={'title': 'PG真实出处' + uuid.uuid4().hex}
        ).json()['id']
        record = client.post(
            '/api/v1/techniques',
            json={
                'title': 'PG长文',
                'source_ids': [sid],
                'summary': '背景内容。' * 1200 + '罕见孔雀石罩染',
            },
        ).json()
        rid = record['id']
        assert (
            client.get('/api/v1/search', params={'q': '孔雀石罩染'}).json()['techniques'][0]['id']
            == rid
        )
        archive = client.get('/api/v1/backup').content
        client.patch(f'/api/v1/techniques/{rid}', json={'summary': '新铬黄渐层'})
        assert client.get('/api/v1/search', params={'q': '孔雀石罩染'}).json()['ranking'] == []
        original = ingestion.chunk_record

        def failed_parse(_):
            raise ValueError('private upstream data')

        monkeypatch.setattr(ingestion, 'chunk_record', failed_parse)
        client.patch(
            f'/api/v1/techniques/{rid}', json={'summary': '失败的青色版本'}
        ).raise_for_status()
        assert (
            client.get('/api/v1/search', params={'q': '铬黄渐层'}).json()['techniques'][0]['id']
            == rid
        )
        with Session(app.state.engine) as db:
            document = db.scalar(select(Document).where(Document.record_key == f'technique:{rid}'))
            assert document.active_version == 2
            assert (
                db.scalar(
                    select(IngestionJob)
                    .where(IngestionJob.document_id == document.id)
                    .order_by(IngestionJob.id.desc())
                ).status
                == 'failed'
            )
            assert db.scalar(select(Embedding)).vector is None
        monkeypatch.setattr(ingestion, 'chunk_record', original)
        client.post(
            '/api/v1/backup', files={'file': ('backup.zip', archive, 'application/zip')}
        ).raise_for_status()
        assert (
            client.get('/api/v1/search', params={'q': '孔雀石罩染'}).json()['techniques'][0]['id']
            == rid
        )
        assert client.get('/api/v1/search', params={'q': '铬黄渐层'}).json()['ranking'] == []
        client.delete(f'/api/v1/sources/{sid}')
        assert client.get('/api/v1/search', params={'q': '孔雀石罩染'}).json()['ranking'] == []


def test_restore_parser_failure_rolls_back_old_active_index(tmp_path, monkeypatch):
    from app import ingestion

    app = create_app(
        test_database_url(f'{tmp_path / "restore-failure.db"}'),
        tmp_path / 'files',
        runtime_settings={},
    )
    with TestClient(app) as client:
        sid = client.post('/api/v1/sources', json={'title': '原始出处'}).json()['id']
        client.post(
            '/api/v1/techniques', json={'title': '教程', 'summary': '靛蓝罩染', 'source_ids': [sid]}
        )
        archive = client.get('/api/v1/backup').content

        def fail(_):
            raise ValueError('parser failure')

        with monkeypatch.context() as patch:
            patch.setattr(ingestion, 'chunk_record', fail)
            response = client.post(
                '/api/v1/backup', files={'file': ('backup.zip', archive, 'application/zip')}
            )
        assert response.status_code == 400
        assert client.get('/api/v1/search', params={'q': '靛蓝罩染'}).json()['ranking']


def test_failed_version_never_relabels_old_text_with_new_source(tmp_path, monkeypatch):
    from app.models import Technique, Source
    from app import ingestion
    from app.ingestion_models import Document

    app = create_app(
        test_database_url(f'{tmp_path / "source-version.db"}'),
        tmp_path / 'files',
        runtime_settings={},
    )
    with Session(app.state.engine) as db:
        old = Source(title='原始出处')
        row = Technique(title='教程', summary='旧来源特有靛蓝罩染', sources=[old])
        db.add(row)
        IngestionService(db).reindex(row)
        db.commit()
        old_id = old.id
        row.sources = [Source(title='无关新出处')]
        row.summary = '新来源只有铬黄'

        def fail(_):
            raise ValueError('parser failure')

        with monkeypatch.context() as patch:
            patch.setattr(ingestion, 'chunk_record', fail)
            IngestionService(db).reindex(row)
            db.commit()
        hits = retrieval.search_documents(db, '靛蓝罩染')
        assert hits[0]['source_ids'] == [old_id]
        assert hits[0]['sources'][0]['title'] == '原始出处'
        old.status = 'withdrawn'
        db.commit()
        assert retrieval.search_documents(db, '靛蓝罩染') == []
        IngestionService(db).reindex(row)
        db.commit()
        document = db.scalar(select(Document))
        version = document.active_version
        row.sources = [Source(title='更正出处')]
        IngestionService(db).reindex(row)
        db.commit()
        assert document.active_version == version + 1
        assert retrieval.search_documents(db, '铬黄')[0]['sources'][0]['title'] == '更正出处'


def test_long_excerpt_covers_specific_term_from_separate_chunk(tmp_path):
    from app.models import Technique, Source
    from app.chat import evidence

    app = create_app(
        test_database_url(f'{tmp_path / "excerpt.db"}'), tmp_path / 'files', runtime_settings={}
    )
    with Session(app.state.engine) as db:
        db.add(
            Technique(
                title='sanding',
                summary='sanding preparation ' * 300,
                content_blocks=[{'type': 'paragraph', 'text': 'sanding: use grit 3000'}],
                sources=[Source(title='manual')],
            )
        )
        IngestionService(db).backfill()
        db.commit()
        assert '3000' in retrieval.search_documents(db, 'sanding preparation 3000')[0]['excerpt']
        assert '3000' in evidence(db, 'sanding preparation 3000')[0]['excerpt']


def test_long_excerpt_does_not_count_terms_outside_emitted_window(tmp_path):
    from app.models import Technique, Source
    from app.chat import evidence

    app = create_app(
        test_database_url(f'{tmp_path / "window.db"}'), tmp_path / 'files', runtime_settings={}
    )
    with Session(app.state.engine) as db:
        db.add(
            Technique(
                title='sanding',
                summary='sanding preparation ' + 'x ' * 600 + 'use grit 3000 ' + 'y ' * 1200,
                sources=[Source(title='manual')],
            )
        )
        IngestionService(db).backfill()
        db.commit()
        assert '3000' in retrieval.search_documents(db, 'sanding preparation 3000')[0]['excerpt']
        assert '3000' in evidence(db, 'sanding preparation 3000')[0]['excerpt']
