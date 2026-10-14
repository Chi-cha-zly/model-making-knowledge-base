"""Transactional text ingestion. Embeddings stay pending until a real encoder exists.

Commit hooks cover ordinary ORM writes, including imports and restore. Bulk SQL
writers must call backfill explicitly. Search never performs ingestion.
"""
import hashlib
import json
import re
from sqlalchemy import delete, event, select
from sqlalchemy.orm import Session
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


def chunk_record(row):
    """Split headings/paragraphs and retain image provenance, including long tails."""
    from .retrieval import readable_excerpt
    sections = []
    if isinstance(row, Technique):
        sections.append((readable_excerpt({'摘要': row.summary, '前提条件': row.prerequisites,
                         '风险': row.risks, '难度': row.difficulty, '标签': row.tags}), None))
        if row.content_blocks:
            heading = ''
            for block in row.content_blocks:
                if block['type'] == 'heading':
                    heading = block['text']
                    sections.append((heading, None))
                elif block['type'] == 'image':
                    sections.append(('\n'.join(filter(None, [heading, block['caption']])), block['attachment_id']))
                else:
                    sections.append(('\n'.join(filter(None, [heading, block['text']])), None))
        else:
            sections.append((readable_excerpt({'步骤': row.steps}), None))
    else:
        sections.append((readable_excerpt({'属性': row.properties, '类别': row.kind,
                         '别名': [a.alias for a in row.aliases]}), None))
    chunks = []
    for text, attachment_id in sections:
        # Preserve a reference even when an image has no extracted text yet.
        sections_by_heading = re.split(r'(?m)(?=^#{1,6}\s)', text) if attachment_id is None else [text]
        for section in sections_by_heading:
            for start in range(0, max(1, len(section)), CHUNK_SIZE - 100):
                part = section[start:start + CHUNK_SIZE]
                if part.strip() or attachment_id is not None:
                    chunks.append((part, attachment_id))
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
    fingerprint = digest({'record': raw, 'payload': payload})
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
                      attachment_id=attachment_id, payload=payload)
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
