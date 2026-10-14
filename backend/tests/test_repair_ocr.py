import hashlib
from pathlib import Path
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from pg_test_support import test_database_url
from app.models import Attachment, Source, Technique
from app.vision_models import ImageAnalysis
from app.ingestion_models import Document, Chunk
from scripts.import_nmm import OCR_NOTICE, SOURCE_TYPE

@pytest.fixture
def library(tmp_path):
    engine=create_engine(test_database_url(tmp_path/'repair.db'))
    storage=tmp_path/'images';storage.mkdir()
    raw=b'original image';sha=hashlib.sha256(raw).hexdigest()
    (storage/'image.png').write_bytes(raw)
    with Session(engine) as db:
        a=Attachment(filename='original.png',storage_name='image.png',content_type='image/png',sha256=sha,size=len(raw))
        source=Source(title='import',source_type=SOURCE_TYPE,content_hash=sha)
        db.add_all([a,source]);db.flush()
        body=OCR_NOTICE+'旧乱码'
        row=Technique(title='参考',summary=body,content_blocks=[{'type':'image','attachment_id':a.id,'caption':body}],sources=[source],attachments=[a])
        db.add(row);db.flush()
        db.add(ImageAnalysis(attachment_id=a.id,image_hash=sha,model='cached-paid-model',ocr_text='旧乱码',description={'description':'必须保留的视觉推断'}))
        db.commit(); ids=(a.id,row.id)
    return engine,storage,ids


def test_repair_is_dry_by_default_atomic_and_idempotent(library):
    from scripts.repair_ocr import repair_ocr
    engine,storage,(aid,rid)=library
    url=engine.url.render_as_string(hide_password=False)
    result=repair_ocr(url,storage,ocr=lambda p:'完整新中文正文')
    assert result['images'][0]['status']=='planned'
    with Session(engine) as db: assert db.get(ImageAnalysis,aid).ocr_text=='旧乱码'
    result=repair_ocr(url,storage,apply=True,ocr=lambda p:'完整新中文正文')
    assert result['changed_document_ids']
    with Session(engine) as db:
        a=db.get(ImageAnalysis,aid); row=db.get(Technique,rid)
        assert a.ocr_text=='完整新中文正文' and a.description=={'description':'必须保留的视觉推断'}
        assert a.model=='cached-paid-model'
        assert len([b for b in row.content_blocks if b['type']=='image'])==1
        assert any(b.get('text')=='完整新中文正文' for b in row.content_blocks)
        doc=db.scalar(select(Document).where(Document.record_id==rid));version=doc.active_version
        chunks=db.scalars(select(Chunk).where(Chunk.document_id==doc.id,Chunk.version==version)).all()
        matching=[c for c in chunks if '完整新中文正文' in c.content]
        assert len(matching)==1 and matching[0].attachment_id==aid
        assert matching[0].content.startswith('本地 OCR 原文（未经人工核对）：')
    again=repair_ocr(url,storage,apply=True,ocr=lambda p:'完整新中文正文')
    assert again['images'][0]['status']=='unchanged'
    assert again['changed_document_ids']==[]


def test_manual_content_preserved_and_failed_index_rolls_back(library,monkeypatch):
    from scripts.repair_ocr import repair_ocr
    import app.ingestion
    engine,storage,(aid,rid)=library; url=engine.url.render_as_string(hide_password=False)
    with Session(engine) as db:
        row=db.get(Technique,rid);row.summary='人工说明，请保留';db.commit()
    result=repair_ocr(url,storage,apply=True,ocr=lambda p:'新文')
    assert result['images'][0]['manual_preserved']==[rid]
    with Session(engine) as db: assert db.get(Technique,rid).summary=='人工说明，请保留'
    def fail(*a,**k): raise ValueError('index failure')
    monkeypatch.setattr(app.ingestion,'chunk_record',fail)
    result=repair_ocr(url,storage,apply=True,ocr=lambda p:'另一个版本')
    assert result['images'][0]['status']=='failed'
    with Session(engine) as db: assert db.get(ImageAnalysis,aid).ocr_text=='新文'


def test_changed_bytes_or_concurrent_authored_edits_are_never_overwritten(library):
    from scripts.repair_ocr import repair_ocr
    engine,storage,(aid,rid)=library;url=engine.url.render_as_string(hide_password=False)
    def edit(path):
        with Session(engine) as db:
            db.get(Technique,rid).summary='识别期间人工编辑';db.commit()
        return '新文'
    result=repair_ocr(url,storage,apply=True,ocr=edit)
    assert result['images'][0]['status']=='stale'
    with Session(engine) as db: assert db.get(ImageAnalysis,aid).ocr_text=='旧乱码'
    (storage/'image.png').write_bytes(b'changed image')
    result=repair_ocr(url,storage,apply=True,ocr=lambda p:pytest.fail('must not OCR changed image'))
    assert result['images'][0]['status']=='failed'


@pytest.mark.parametrize('change',['source','owner','file'])
def test_withdrawal_or_file_change_during_ocr_rejects_result(library,change):
    from scripts.repair_ocr import repair_ocr
    engine,storage,(aid,rid)=library
    def mutate(path):
        if change=='file': path.write_bytes(b'changed during OCR')
        else:
            with Session(engine) as db:
                row=db.get(Technique,rid)
                if change=='source': row.sources[0].status='withdrawn'
                else: row.status='withdrawn'
                db.commit()
        return 'stale OCR result'
    result=repair_ocr(engine.url.render_as_string(hide_password=False),storage,apply=True,ocr=mutate)
    assert result['images'][0]['status']==('failed' if change=='file' else 'stale')
    with Session(engine) as db: assert db.get(ImageAnalysis,aid).ocr_text=='旧乱码'


def test_report_cannot_overwrite_original_image_before_apply(tmp_path,monkeypatch):
    from scripts import repair_ocr
    storage=tmp_path/'images';storage.mkdir();image=storage/'original.png';image.write_bytes(b'original')
    monkeypatch.setattr(repair_ocr,'repair_ocr',lambda *a,**k:pytest.fail('must reject before any OCR/DB mutation'))
    with pytest.raises(SystemExit):
        repair_ocr.main(['--database-url','not-used','--storage-dir',str(storage),'--report',str(image),'--apply'])
    assert image.read_bytes()==b'original'


def test_needs_review_repair_keeps_vlm_and_reports_raw_without_indexing_it(library):
    from scripts.repair_ocr import repair_ocr
    engine,storage,(aid,rid)=library
    class Review:
        def recognize(self,path): return {'text':'','status':'needs_review','raw_text':'vertical-noise','engine':'test'}
    result=repair_ocr(engine.url.render_as_string(hide_password=False),storage,apply=True,ocr=Review())
    assert result['images'][0]['ocr_status']=='needs_review'
    assert result['images'][0]['recognition']['raw_text']=='vertical-noise'
    with Session(engine) as db:
        assert db.get(ImageAnalysis,aid).description=={'description':'必须保留的视觉推断'}
        assert db.get(ImageAnalysis,aid).ocr_text==''
        assert '待人工核对' in db.get(Technique,rid).summary
        assert all('vertical-noise' not in c.content for c in db.scalars(select(Chunk)))
