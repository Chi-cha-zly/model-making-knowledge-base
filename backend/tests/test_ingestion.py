"""Ingestion must publish complete versions and retain usable evidence on failure."""

from pg_test_support import test_database_url
from sqlalchemy import create_engine, select, func
from sqlalchemy.orm import Session
from app.models import Technique, Source
from app.services.ingestion import IngestionService


def test_ingestion_versions_idempotence_and_pending_embeddings(monkeypatch):
    from app import ingestion
    from app.ingestion_models import Document, Chunk, Embedding, IngestionJob

    engine = create_engine(test_database_url(''))
    with Session(engine) as db:
        row = Technique(title='打磨', summary='砂纸处理', sources=[Source(title='出处')])
        db.add(row)
        IngestionService(db).reindex(row)
        db.commit()
        document = db.scalar(select(Document))
        assert document.active_version == 1
        IngestionService(db).reindex(row)
        db.commit()
        assert db.scalar(select(func.count()).select_from(IngestionJob)) == 1
        assert db.scalar(select(Embedding)).status == 'pending'
        assert db.scalar(select(Embedding)).vector is None
        row.summary = '新的抛光工序'
        IngestionService(db).reindex(row)
        db.commit()
        assert document.active_version == 2
        assert db.scalar(select(func.count()).select_from(Chunk).where(Chunk.version == 1)) > 0
        original = ingestion.chunk_record

        def fail(_):
            raise ValueError('upstream-private-detail')

        monkeypatch.setattr(ingestion, 'chunk_record', fail)
        row.summary = '失败版本'
        IngestionService(db).reindex(row)
        db.commit()
        assert document.active_version == 2
        job = db.scalar(select(IngestionJob).order_by(IngestionJob.id.desc()))
        assert job.status == 'failed'
        assert 'private' not in job.error
        monkeypatch.setattr(ingestion, 'chunk_record', original)
        IngestionService(db).reindex(row)
        db.commit()
        assert document.active_version == 3


def test_markdown_sections_keep_heading_boundaries():
    from app.ingestion import chunk_record

    row = Technique(
        title='教程',
        summary='# NMM\n导读\n\n## 阴影\n紫色薄涂\n\n## 高光\n浅黄色描边',
        content_blocks=[],
        tags=[],
    )
    parts = [part for part, _ in chunk_record(row)]
    assert any('## 阴影' in part and '紫色薄涂' in part and '## 高光' not in part for part in parts)
    assert any(
        '## 高光' in part and '浅黄色描边' in part and '## 阴影' not in part for part in parts
    )
