"""Refresh local OCR without cloud calls. Default: recognize and report, no DB writes.

Existing VLM descriptions/models and original files are preserved. Apply uses an
atomic attachment+owners transaction and rejects stale snapshots or changed bytes.
"""
from __future__ import annotations
import argparse
import json
from pathlib import Path
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from app.db import create_db_engine
from app.ingestion import digest, ingest_record
from app.ingestion_models import Chunk, Document, Embedding
from app.models import Attachment, Product, Source, Technique
from app.services.ocr import local_ocr, recognize_local
from app.vision_jobs import _attachment_identity, _read_image
from app.vision_models import ImageAnalysis
from scripts.import_nmm import EMPTY_NOTICE, OCR_NOTICE, SOURCE_TYPE, image_article, validate_report_path, write_report

OLD_NOTICE='本地 OCR 原文（未经人工核对，可能包含错字；视觉描述尚未生成）：\n'
OLD_EMPTY='本地 OCR 未提取到可识别文字；视觉描述尚未生成。仅按文件名元数据检索，请查看原图。'


def _owners(db, aid):
    return [row for model in (Technique,Product) for row in db.scalars(
        select(model).where(model.attachments.any(Attachment.id==aid)).order_by(model.id))]


def _columns(row):
    return {c.name:str(value) if c.name.endswith('_at') else value
            for c in row.__table__.columns for value in [getattr(row,c.name)]}


def _snapshot(db, attachment, owners):
    analysis=db.get(ImageAnalysis,attachment.id)
    return digest({'attachment':_columns(attachment),'analysis':_columns(analysis) if analysis else None,
        'owners':[{'kind':row.__tablename__,'record':_columns(row),
            'sources':[_columns(s) for s in sorted(row.sources,key=lambda s:s.id)],
            'attachments':[_columns(a) for a in sorted(row.attachments,key=lambda a:a.id)],
            'version':[(d.id,d.active_version,d.fingerprint) for d in db.scalars(select(Document).where(
                Document.kind==('technique' if isinstance(row,Technique) else 'product'),Document.record_id==row.id))]}
            for row in owners]})


def _authorized(attachment,owners):
    return attachment.owner_id=='local' and attachment.status=='published' and bool(owners) and all(
        row.owner_id=='local' and row.status=='published' and row.sources and all(
            s.owner_id=='local' and s.status=='published' for s in row.sources) for row in owners)


def _machine_layout(row, attachment, text, status):
    """Old source marker + exact summary/caption partition, or unchanged new layout.

    A changed paragraph/summary in the modern layout cannot be proven machine-owned,
    so it is preserved unless it already equals the newly recognized text.
    """
    if not isinstance(row,Technique) or len(row.attachments)!=1 or not any(
        s.source_type==SOURCE_TYPE and s.content_hash==attachment.sha256 for s in row.sources):
        return None
    new=image_article(attachment.id,text,status=status)
    if (row.summary,row.content_blocks)==new:
        return new
    body=row.summary or ''
    if not (body.startswith(OCR_NOTICE) or body.startswith(OLD_NOTICE) or body in (EMPTY_NOTICE,OLD_EMPTY)):
        return None
    expected=[{'type':'image','attachment_id':attachment.id,'caption':body[i:i+1000]}
              for i in range(0,len(body),1000)]
    return new if row.content_blocks==expected else None


def _changes(db, attachment, owners, text, status):
    layouts=[];manual=[]
    for row in owners:
        if isinstance(row,Technique):
            layout=_machine_layout(row,attachment,text,status)
            if layout is None:
                manual.append(row.id)
            elif (row.summary,row.content_blocks)!=layout:
                layouts.append((row,layout))
    analysis=db.get(ImageAnalysis,attachment.id)
    change_analysis=bool(analysis and analysis.image_hash==attachment.sha256 and analysis.ocr_text!=text)
    return layouts,manual,analysis,change_analysis


def repair_ocr(database_url,storage_dir,*,apply=False,ocr=None,attachment_ids=None):
    engine=create_db_engine(database_url)
    storage=Path(storage_dir).resolve(strict=True)
    recognizer=ocr or local_ocr()
    report={'dry_run':not apply,'ocr_engine':type(recognizer).__name__,'images':[],
            'changed_document_ids':[],'active_pending_embeddings':0,'active_pending_document_ids':[]}
    try:
        with Session(engine) as db:
            statement=select(Attachment.id).order_by(Attachment.id)
            if attachment_ids is not None: statement=statement.where(Attachment.id.in_(attachment_ids))
            ids=list(db.scalars(statement))
        changed=set()
        for aid in ids:
            item={'attachment_id':aid,'status':'planned','manual_preserved':[]}
            report['images'].append(item)
            try:
                with Session(engine) as db:
                    attachment=db.get(Attachment,aid); owners=_owners(db,aid)
                    if not _authorized(attachment,owners):
                        item['status']='skipped_unauthorized';continue
                    identity=_attachment_identity(attachment)
                    snapshot=_snapshot(db,attachment,owners)
                path,_=_read_image(storage,identity)
                result=recognize_local(recognizer,path)
                text=result['text']
                item['ocr_status']=result['status']
                item['recognition']={k:result[k] for k in ('engine','revision','regions','observations','raw_text','language_pass') if k in result}
                if not isinstance(text,str) or len(text)>99000:
                    raise ValueError('OCR output exceeds supported article limit')
                text=text.strip(); item['ocr_text']=text;item['ocr_characters']=len(text)
                # Even dry runs reject OCR computed from bytes that changed mid-read.
                _read_image(storage,identity)
                with Session(engine) as db, db.begin():
                    attachment=db.scalar(select(Attachment).where(Attachment.id==aid).with_for_update())
                    owners=_owners(db,aid)
                    source_ids=sorted({s.id for row in owners for s in row.sources})
                    list(db.scalars(select(Source).where(Source.id.in_(source_ids)).order_by(Source.id).with_for_update()))
                    for row in owners:
                        db.refresh(row,with_for_update=True)
                    list(db.scalars(select(Document).where(
                        Document.record_key.in_([('technique' if isinstance(r,Technique) else 'product')+':'+str(r.id) for r in owners])
                    ).order_by(Document.id).with_for_update()))
                    db.scalar(select(ImageAnalysis).where(ImageAnalysis.attachment_id==aid).with_for_update())
                    # Re-read under locks, including relationship/source state.
                    db.expire_all();attachment=db.get(Attachment,aid);owners=_owners(db,aid)
                    if not _authorized(attachment,owners) or _snapshot(db,attachment,owners)!=snapshot:
                        item['status']='stale';continue
                    _read_image(storage,identity)
                    layouts,manual,analysis,change_analysis=_changes(db,attachment,owners,text,result['status'])
                    item['manual_preserved']=manual
                    item['changed_technique_ids']=[row.id for row,_ in layouts]
                    item['analysis_ocr_changed']=change_analysis
                    if not layouts and not change_analysis:
                        item['status']='unchanged';continue
                    if not apply:
                        continue
                    for row,(summary,blocks) in layouts:
                        row.summary,row.content_blocks=summary,blocks
                    if change_analysis: analysis.ocr_text=text
                    db.flush()
                    current=[]
                    for row in owners:
                        document=ingest_record(db,row,strict=True)
                        current.append(document.id)
                    # All versions and analysis commit together. The old vectors
                    # remain tied to their old versions; new ones remain pending.
                    _read_image(storage,identity)
                changed.update(current)
                item['status']='updated'
                item['changed_document_ids']=current
            except Exception as exc:
                item.update(status='failed',error_type=type(exc).__name__,
                    error='OCR repair failed; this image and its owner records were not changed.')
        with Session(engine) as db:
            pending=select(Embedding).join(Chunk,Chunk.id==Embedding.chunk_id).join(Document,Document.id==Chunk.document_id).where(
                Chunk.version==Document.active_version,Embedding.status=='pending')
            report['active_pending_embeddings']=db.scalar(select(func.count()).select_from(pending.subquery()))
            report['active_pending_document_ids']=list(db.scalars(select(Document.id).join(Chunk,Chunk.document_id==Document.id).join(
                Embedding,Embedding.chunk_id==Chunk.id).where(Chunk.version==Document.active_version,Embedding.status=='pending').distinct().order_by(Document.id)))
        report['changed_document_ids']=sorted(changed)
        report['counts']={status:sum(i['status']==status for i in report['images']) for status in ('planned','updated','unchanged','stale','failed','skipped_unauthorized')}
        return report
    finally:
        engine.dispose()


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database-url',required=True)
    parser.add_argument('--storage-dir',required=True,type=Path)
    parser.add_argument('--report',required=True,type=Path)
    mode=parser.add_mutually_exclusive_group();mode.add_argument('--apply',action='store_true');mode.add_argument('--dry-run',action='store_true')
    parser.add_argument('--attachment-id',action='append',type=int)
    parser.add_argument('--ocr-engine',choices=['vision','tesseract'],default='vision')
    parser.add_argument('--tessdata-dir',type=Path)
    args=parser.parse_args(argv)
    try:
        report_path=validate_report_path(args.report,args.storage_dir)
    except ValueError as exc:
        parser.error(str(exc))
    report=repair_ocr(args.database_url,args.storage_dir,apply=args.apply,attachment_ids=args.attachment_id,
                      ocr=local_ocr(engine=args.ocr_engine,tessdata_dir=args.tessdata_dir))
    write_report(report_path,report)
    print(json.dumps(report['counts'],ensure_ascii=False))
    return 1 if report['counts']['failed'] or report['counts']['stale'] else 0

if __name__=='__main__':
    raise SystemExit(main())
