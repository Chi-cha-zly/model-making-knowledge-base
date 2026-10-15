from pg_test_support import test_database_url
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from app.models import Attachment, Source, Technique
from app.ingestion_models import Document
from app.vision_models import ImageAnalysis
from app.services.ingestion import IngestionService
from app.retrieval import search_documents


def test_image_analysis_is_searchable_separate_evidence_and_cites_matched_images():
    engine = create_engine(test_database_url())
    with Session(engine) as db:
        source = Source(title='图片教程')
        a = Attachment(
            filename='a.png',
            content_type='image/png',
            storage_name='a.png',
            sha256='a' * 64,
            size=1,
        )
        b = Attachment(
            filename='b.png',
            content_type='image/png',
            storage_name='b.png',
            sha256='b' * 64,
            size=1,
        )
        db.add_all([source, a, b])
        db.flush()
        row = Technique(
            title='示例',
            sources=[source],
            attachments=[a, b],
            content_blocks=[
                {'type': 'paragraph', 'text': '普通水贴说明'},
                {'type': 'image', 'attachment_id': a.id, 'caption': '手工图注'},
                {'type': 'image', 'attachment_id': b.id, 'caption': '另一张图'},
            ],
        )
        db.add(row)
        IngestionService(db).reindex(row)
        db.commit()
        initial = db.scalar(select(Document)).active_version
        db.add(
            ImageAnalysis(
                attachment_id=a.id,
                image_hash=a.sha256,
                model='test-vision',
                ocr_text='AK12345',
                description={
                    'description': '钴蓝高光',
                    'materials': ['黄铜'],
                    'techniques': ['釉染'],
                    'colors': ['孔雀绿'],
                    'step_number': 2,
                    'visible_text': '推断文字',
                    'uncertainties': ['材质不确定'],
                },
            )
        )
        db.flush()
        IngestionService(db).reindex(row)
        db.commit()
        document = db.scalar(select(Document))
        assert document.active_version == initial + 1
        hit = search_documents(db, '孔雀绿')[0]
        assert hit['attachment_ids'] == [a.id]
        assert '本地 OCR 原文' in hit['excerpt'] and 'AK12345' in hit['excerpt']
        assert '模型视觉推断' in hit['excerpt'] and '材质不确定' in hit['excerpt']
        assert search_documents(db, '水贴说明')[0]['attachment_ids'] == []
        IngestionService(db).reindex(row)
        db.commit()
        assert document.active_version == initial + 1
        db.get(ImageAnalysis, a.id).image_hash = 'stale'
        db.flush()
        IngestionService(db).reindex(row)
        db.commit()
        assert search_documents(db, '孔雀绿') == []
    engine.dispose()


def test_unanalysed_auxiliary_attachments_receive_image_chunks():
    from app.models import Product
    from app.ingestion_models import Chunk

    engine = create_engine(test_database_url())
    with Session(engine) as db:
        source = Source(title='source')
        attachment = Attachment(
            filename='aux.png',
            content_type='image/png',
            storage_name='aux.png',
            sha256='a' * 64,
            size=1,
        )
        db.add(Product(name='product', sources=[source], attachments=[attachment]))
        IngestionService(db).backfill()
        db.commit()
        chunk = db.scalar(select(Chunk).where(Chunk.attachment_id == attachment.id))
        assert chunk is not None and chunk.kind == 'image'
        assert chunk.payload['attachment_hash'] == attachment.sha256
    engine.dispose()


def test_long_image_evidence_retains_origin_and_current_attachment_authorization():
    engine = create_engine(test_database_url())
    with Session(engine) as db:
        source = Source(title='source')
        attachment = Attachment(
            filename='aux.png',
            content_type='image/png',
            storage_name='aux.png',
            sha256='a' * 64,
            size=1,
        )
        db.add_all([source, attachment])
        db.flush()
        row = Technique(
            title='image', sources=[source], attachments=[attachment], content_blocks=[]
        )
        db.add(row)
        db.flush()
        db.add(
            ImageAnalysis(
                attachment_id=attachment.id,
                image_hash=attachment.sha256,
                model='vision',
                ocr_text='普通文字' * 1800 + '稀有铬绿颜料',
                description={
                    'description': '背景' * 1800 + '孔雀石罩染',
                    'materials': [],
                    'techniques': [],
                    'colors': [],
                    'step_number': None,
                    'visible_text': '',
                    'uncertainties': [],
                },
            )
        )
        IngestionService(db).reindex(row)
        db.commit()
        for query, label in [('稀有铬绿', '本地 OCR 原文'), ('孔雀石罩染', '模型视觉推断')]:
            hit = search_documents(db, query)[0]
            assert label in hit['excerpt']
            assert hit['attachment_ids'] == [attachment.id]
        attachment.status = 'withdrawn'
        db.commit()
        assert search_documents(db, '孔雀石罩染') == []
        attachment.status = 'published'
        attachment.sha256 = 'b' * 64
        db.commit()
        assert search_documents(db, '孔雀石罩染') == []
    engine.dispose()
