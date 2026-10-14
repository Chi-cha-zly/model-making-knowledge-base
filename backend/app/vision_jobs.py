"""Explicit image interpretation with short, atomic compare-and-publish transactions."""
import asyncio
import hashlib
import inspect
from pathlib import Path

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from .embedding_jobs import _visible
from .ingestion import ingest_record
from .ingestion_models import Chunk, Document, DocumentSource
from .models import Attachment, Product, Source, Technique
from .services.ocr import local_ocr, recognize_local
from .vision_models import ImageAnalysis, ImageDescription

MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_LINKED_RECORDS = 1000


def _snapshot(db, attachment_id):
    """Version and active source identity for every visible owner of this image."""
    rows = db.execute(select(Document.id, Document.active_version, Document.kind,
        Document.record_id, DocumentSource.source_id).join(Chunk, Chunk.document_id == Document.id)
        .join(DocumentSource, (DocumentSource.document_id == Document.id) &
              (DocumentSource.version == Document.active_version))
        .join(Source, Source.id == DocumentSource.source_id)
        .where(Chunk.attachment_id == attachment_id, Chunk.version == Document.active_version,
               Source.owner_id == 'local', Source.status == 'published', _visible())
        .distinct().order_by(Document.id, DocumentSource.source_id).limit(MAX_LINKED_RECORDS + 1)).all()
    return [tuple(row) for row in rows]


def _plan(engine, model, limit):
    with Session(engine) as db:
        ids = db.scalars(select(Attachment.id).join(Chunk, Chunk.attachment_id == Attachment.id)
            .join(Document, Document.id == Chunk.document_id)
            .outerjoin(ImageAnalysis, ImageAnalysis.attachment_id == Attachment.id)
            .where(Chunk.version == Document.active_version, _visible(),
                   Attachment.owner_id == 'local', Attachment.status == 'published',
                   or_(ImageAnalysis.attachment_id.is_(None), ImageAnalysis.image_hash != Attachment.sha256,
                       ImageAnalysis.model != model))
            .distinct().order_by(Attachment.id).limit(limit)).all()
    return ids


def _read_image(storage, attachment):
    name, image_hash, size, content_type = attachment
    path = storage / name
    if Path(name).name != name or path.is_symlink() or path.resolve().parent != storage:
        raise ValueError('Unsafe image path')
    with path.open('rb') as file:
        raw = file.read(MAX_IMAGE_BYTES + 1)
    if not 0 < len(raw) <= MAX_IMAGE_BYTES or len(raw) != size or hashlib.sha256(raw).hexdigest() != image_hash:
        raise ValueError('Image changed or exceeds size limit')
    if content_type not in ('image/png', 'image/jpeg', 'image/gif', 'image/webp'):
        raise ValueError('Unsupported image type')
    return path, raw


def _attachment_identity(row):
    return row.storage_name, row.sha256, row.size, row.content_type


def _still_current(engine, attachment_id, identity, snapshot):
    with Session(engine) as db:
        row = db.get(Attachment, attachment_id)
        return bool(row is not None and row.owner_id == 'local' and row.status == 'published'
                    and _attachment_identity(row) == identity and _snapshot(db, attachment_id) == snapshot)


def _publish(engine, storage, attachment_id, identity, snapshot, model, ocr_text, description):
    with Session(engine) as db, db.begin():
        attachment = db.scalar(select(Attachment).where(Attachment.id == attachment_id).with_for_update())
        if (attachment is None or attachment.owner_id != 'local' or attachment.status != 'published'
                or _attachment_identity(attachment) != identity):
            return 'stale'
        # Lock provenance and versions before the final comparison. A withdrawal or
        # concurrent reindex must finish before this check or wait for publication.
        source_ids = sorted({row[4] for row in snapshot})
        document_ids = sorted({row[0] for row in snapshot})
        list(db.scalars(select(Source).where(Source.id.in_(source_ids)).order_by(Source.id).with_for_update()))
        list(db.scalars(select(Document).where(Document.id.in_(document_ids)).order_by(Document.id).with_for_update()))
        if _snapshot(db, attachment_id) != snapshot:
            return 'stale'
        try:
            _read_image(storage, identity)
        except (OSError, ValueError):
            return 'stale'
        analysis = db.get(ImageAnalysis, attachment_id)
        if analysis is not None and analysis.image_hash == identity[1] and analysis.model == model:
            return 'unchanged'
        if analysis is None:
            analysis = ImageAnalysis(attachment_id=attachment_id)
            db.add(analysis)
        analysis.image_hash, analysis.model = identity[1], model
        analysis.ocr_text, analysis.description = ocr_text, description
        db.flush()
        for _, _, kind, record_id in dict.fromkeys(row[:4] for row in snapshot):
            row = db.get(Technique if kind == 'technique' else Product, record_id)
            if row is None or row.owner_id != 'local' or row.status != 'published':
                raise ValueError('Owner changed during publication')
            # Analysis and all derived versions commit together; strict failures
            # roll back the new analysis and every already-built owner version.
            ingest_record(db, row, strict=True)
        return 'ready'


async def describe_pending(engine, provider, storage, *, apply=False, max_documents=20, ocr=None, progress=None):
    """Plan by default; each bounded result is one image shared by zero or more documents."""
    if type(max_documents) is not int or not 1 <= max_documents <= 100:
        raise ValueError('max_documents must be between 1 and 100')
    storage = Path(storage).resolve()
    model = getattr(provider, 'vision_model', None)
    configured = bool(getattr(provider, 'vision_configured', False) and model)
    targets = _plan(engine, model, max_documents)
    result = {'status': ('completed' if apply else 'dry_run') if configured else 'unconfigured',
              'dry_run': not apply, 'configured': configured, 'documents': []}
    recognizer = ocr or local_ocr()
    for index, attachment_id in enumerate(targets):
        with Session(engine) as db:
            attachment = db.get(Attachment, attachment_id)
            snapshot = _snapshot(db, attachment_id)
            identity = _attachment_identity(attachment) if attachment is not None else None
        item = {'attachment_id': attachment_id, 'document_ids': sorted({row[0] for row in snapshot}),
                'status': 'planned'}
        result['documents'].append(item)
        if not identity or not snapshot:
            item['status'] = 'stale'
        elif apply and configured:
            try:
                if len(snapshot) > MAX_LINKED_RECORDS:
                    raise ValueError('Too many linked records')
                path, image = _read_image(storage, identity)
                # OCR is a local subprocess. Neither it nor the network request
                # holds a SQLAlchemy transaction or blocks the asyncio loop.
                recognized = await asyncio.to_thread(recognize_local, recognizer, path)
                ocr_text = recognized['text']
                item['ocr_status'] = recognized['status']
                if not isinstance(ocr_text, str) or len(ocr_text) > 100000:
                    raise ValueError('Invalid OCR output')
                ocr_text = ocr_text.strip()
                if not _still_current(engine, attachment_id, identity, snapshot):
                    item['status'] = 'stale'
                else:
                    _read_image(storage, identity)
                    description = ImageDescription.model_validate(await provider.describe_image(image, identity[3])).model_dump()
                    await asyncio.sleep(0)
                    if getattr(provider, 'vision_model', None) != model:
                        item['status'] = 'stale'
                    else:
                        item['status'] = _publish(engine, storage, attachment_id, identity, snapshot,
                                                  model, ocr_text, description)
            except Exception:
                item.update(status='failed', error='图片任务失败；未发布部分结果，已有资料和索引保持不变。')
                result['status'] = 'partial_failure'
        if progress is not None:
            pending = progress(index + 1, len(targets))
            if inspect.isawaitable(pending):
                await pending
    return result
