"""Transactional text ingestion. Embeddings stay pending until a real encoder exists.

Commit hooks cover ordinary ORM writes, including imports and restore. Bulk SQL
writers must call backfill explicitly. Search never performs ingestion.
"""
import hashlib
import json
import re
from sqlalchemy import delete, event, select
from sqlalchemy.orm import Session, object_session
from .models import Technique, Product, ProductAlias
from .ingestion_models import Document, Chunk, Embedding, IngestionJob, Posting, DocumentSource

CHUNK_SIZE = 1600


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def record_payload(row):
    from .retrieval import readable_excerpt
    if isinstance(row, Technique):
        return {'key': f'technique:{row.id}', 'type': 'technique', 'id': row.id,
                'title': row.title, 'sku': '', 'aliases': [],
                'fields': {'category': row.category or '', 'tags': readable_excerpt(row.tags)}}
    return {'key': f'product:{row.id}', 'type': 'product', 'id': row.id,
            'title': ' '.join(filter(None, (row.brand, row.name, row.sku))),
            'sku': row.sku or '', 'aliases': [a.alias for a in row.aliases],
            'fields': {'brand': row.brand or '', 'name': row.name, 'category': row.kind or ''}}



def image_evidence(row):
    """Only current, authorized attachment interpretations may enter evidence."""
    from .vision_models import ImageAnalysis
    db = object_session(row)
    if db is None:
        return {}
    result = {}
    for attachment in row.attachments:
        if attachment.owner_id != 'local' or attachment.status != 'published':
            continue
        analysis = db.get(ImageAnalysis, attachment.id)
        if analysis is not None and analysis.image_hash == attachment.sha256:
            result[attachment.id] = {'ocr_text': analysis.ocr_text, 'description': analysis.description,
                                     'model': analysis.model, 'image_hash': analysis.image_hash}
    return result


IMAGE_ORIGINS = ('本地 OCR 原文（未经人工核对）：', '模型视觉推断（可能有误，须核对原图）：', '资料图注（原始资料）：')


def image_evidence_sections(evidence):
    from .retrieval import readable_excerpt
    description = evidence['description']
    inferred = readable_excerpt({'描述': description.get('description'),
        '材料': description.get('materials'), '技法': description.get('techniques'),
        '颜色': description.get('colors'), '步骤序号': description.get('step_number'),
        '模型辨识文字': description.get('visible_text'), '不确定项': description.get('uncertainties')})
    parts = []
    if evidence['ocr_text']:
        parts.append(IMAGE_ORIGINS[0] + '\n' + evidence['ocr_text'])
    parts.append(IMAGE_ORIGINS[1] + '\n' + inferred)
    return parts



def current_import_notice(value):
    old_ocr = '本地 OCR 原文（未经人工核对，可能包含错字；视觉描述尚未生成）：\n'
    old_empty = '本地 OCR 未提取到可识别文字；视觉描述尚未生成。仅按文件名元数据检索，请查看原图。'
    if value and value.startswith(old_ocr):
        return '本地 OCR 原文（未经人工核对，可能包含错字）：\n' + value[len(old_ocr):]
    if value == old_empty:
        return '本地 OCR 未提取到可识别文字。请查看原图及单独标注的模型视觉推断。'
    return value

def attachment_hashes(row):
    return {a.id: a.sha256 for a in row.attachments if a.owner_id == 'local' and a.status == 'published'}


def chunk_record(row):
    """Bound chunks while repeating provenance on every image evidence segment."""
    from .retrieval import readable_excerpt
    sections = []
    evidence = image_evidence(row)
    attachments = attachment_hashes(row)
    seen_images = set()
    body_ocr = {}
    if isinstance(row, Technique):
        summary = current_import_notice(row.summary) if evidence else row.summary
        if summary and summary.startswith('本地 OCR 原文（未经人工核对，可能包含错字'):
            body = summary.split('：\n', 1)[-1]
            aid = next(iter(attachments)) if len(attachments) == 1 else None
            sections.append((IMAGE_ORIGINS[0] + '\n' + body, aid))
            summary = None
        sections.append((readable_excerpt({'摘要': summary, '前提条件': row.prerequisites,
                         '风险': row.risks, '难度': row.difficulty, '标签': row.tags}), None))
        heading = ''
        for block in row.content_blocks or []:
            if block['type'] == 'heading':
                heading = block['text']
                sections.append((heading, None))
            elif block['type'] == 'image':
                aid = block['attachment_id']
                if aid not in attachments:
                    continue
                seen_images.add(aid)
                caption = current_import_notice(block['caption']) if aid in evidence else block['caption']
                caption = '\n'.join(filter(None,[heading,caption]))
                sections.append((IMAGE_ORIGINS[2] + '\n' + caption, aid))
            else:
                if heading == IMAGE_ORIGINS[0] and len(attachments) == 1:
                    aid = next(iter(attachments))
                    sections.append((IMAGE_ORIGINS[0] + '\n' + block['text'], aid))
                    body_ocr.setdefault(aid, []).append(block['text'])
                else:
                    sections.append(('\n'.join(filter(None,[heading,block['text']])), None))
    else:
        sections.append((readable_excerpt({'属性': row.properties, '类别': row.kind,
                         '别名': [a.alias for a in row.aliases]}), None))
    for aid in attachments:
        if aid not in seen_images:
            sections.append(('', aid))
        if aid in evidence:
            image = evidence[aid]
            if ''.join(body_ocr.get(aid, [])) == image['ocr_text']:
                image = {**image, 'ocr_text': ''}
            sections.extend((part, aid) for part in image_evidence_sections(image))
    chunks = []
    for text, attachment_id in sections:
        origin = next((label for label in IMAGE_ORIGINS if text.startswith(label)), '')
        body = text[len(origin):].lstrip('\n') if origin else text
        sections_by_heading = re.split(r'(?m)(?=^#{1,6}\s)', body) if attachment_id is None else [body]
        for section in sections_by_heading:
            for start in range(0, max(1,len(section)), CHUNK_SIZE - 100):
                part = section[start:start + CHUNK_SIZE]
                if part.strip() or attachment_id is not None:
                    chunks.append(((origin + '\n' if origin else '') + part, attachment_id))
    return chunks or [('', None)]


def index_terms(text):
    from .retrieval import _normalize, _tokens
    normalized = _normalize(text)
    tokens = set(_tokens(normalized))
    tokens.update(re.findall(r'[\u4e00-\u9fff]', normalized))
    output = {f't:{t}' for t in tokens if len(t) <= 190}
    for token in tokens:
        if re.fullmatch(r'[a-z]{4,40}', token):
            output.add('f:' + token)
            output.update('f:' + token[:i] + token[i + 1:] for i in range(len(token)))
    return output


def ingest_record(db, row, *, force=False, strict=False):
    """Build a whole version, then atomically activate it; caller owns commit."""
    db.flush()
    payload = record_payload(row)
    payload['source_ids'] = sorted(source.id for source in row.sources)
    raw = {c.name: getattr(row, c.name) for c in row.__table__.columns}
    fingerprint = digest({'record': raw, 'payload': payload, 'images': image_evidence(row), 'attachments': attachment_hashes(row)})
    document = db.scalar(select(Document).where(Document.record_key == payload['key']).with_for_update())
    if document is None:
        document = Document(record_key=payload['key'], kind=payload['type'], record_id=row.id)
        db.add(document)
        db.flush()
    if not force and document.fingerprint == fingerprint:
        return document
    version = document.active_version + 1
    job = IngestionJob(document_id=document.id, version=version)
    db.add(job)
    db.flush()
    # Parsing can fail without replacing any old chunks or active version.
    try:
        parts = chunk_record(row)
        prepared = []
        for content, attachment_id in parts:
            searchable = ' '.join([payload['title'], content, payload['sku'],
                                   ' '.join(payload['aliases']), ' '.join(payload['fields'].values())])
            terms = index_terms(searchable)
            compact_sku = re.sub(r'[\s_-]+', '', payload['sku'].casefold())
            if compact_sku:
                terms.add('s:' + compact_sku)
                if len(re.findall(r'\d', compact_sku)) >= 5:
                    terms.update('s:' + compact_sku[:i] + compact_sku[i + 1] + compact_sku[i] + compact_sku[i + 2:]
                                 for i in range(len(compact_sku) - 1))
            prepared.append((content, attachment_id, terms))
    except Exception:
        job.status, job.error = 'failed', '资料解析或索引生成失败；旧版本仍可检索，请重试。'
        document.status = 'failed'
        if strict:
            raise ValueError(job.error) from None
        return document
    db.add_all(DocumentSource(document_id=document.id, version=version, source_id=source_id)
               for source_id in payload['source_ids'])
    for ordinal, (content, attachment_id, terms) in enumerate(prepared):
        chunk = Chunk(document_id=document.id, version=version, ordinal=ordinal,
                      kind='image' if attachment_id else 'text', content=content,
                      attachment_id=attachment_id, payload={**payload, **({'attachment_hash': attachment_hashes(row)[attachment_id]} if attachment_id else {})})
        db.add(chunk)
        db.flush()
        db.add(Embedding(chunk_id=chunk.id, content_hash=digest(content), status='pending'))
        db.add_all(Posting(chunk_id=chunk.id, term=term) for term in sorted(terms))
    document.active_version, document.fingerprint, document.status = version, fingerprint, 'ready'
    job.status, job.progress = 'completed', 100
    db.flush()
    return document


def clear_index(db):
    for model in (Posting, Embedding, Chunk, DocumentSource, IngestionJob, Document):
        db.execute(delete(model))


def backfill(db, *, missing_only=False, strict=False):
    count = 0
    for model, kind in ((Technique, 'technique'), (Product, 'product')):
        statement = select(model).order_by(model.id)
        if missing_only:
            statement = statement.where(~select(Document.id).where(
                Document.kind == kind, Document.record_id == model.id).exists())
        for row in db.scalars(statement.execution_options(yield_per=100)):
            ingest_record(db, row, strict=strict)
            count += 1
    return count


@event.listens_for(Session, 'before_flush')
def remember_changed_records(db, context, instances):
    if db.info.get('ingestion_active'):
        return
    pending = db.info.setdefault('ingestion_pending', set())
    for row in db.new | db.dirty:
        if isinstance(row, (Technique, Product)):
            pending.add(row)
        elif isinstance(row, ProductAlias) and row.product is not None:
            pending.add(row.product)


@event.listens_for(Session, 'before_commit')
def index_changed_records(db):
    if db.info.get('ingestion_active'):
        return
    db.info['ingestion_active'] = True
    try:
        # before_flush must still collect records from this final flush.
        db.info['ingestion_active'] = False
        db.flush()
        db.info['ingestion_active'] = True
        for row in db.info.pop('ingestion_pending', set()):
            if row in db and row not in db.deleted:
                ingest_record(db, row)
    finally:
        db.info['ingestion_active'] = False


@event.listens_for(Session, 'after_rollback')
def discard_pending(db):
    db.info.pop('ingestion_pending', None)
