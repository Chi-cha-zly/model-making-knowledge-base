"""Explicit, bounded embedding work. Importing this module never invokes a model."""

from sqlalchemy import and_, or_, select, update
from sqlalchemy.orm import Session

from .errors import ProviderError
from .ingestion import digest
from .ingestion_models import Chunk, Document, DocumentSource, Embedding
from .embedding_contract import embedding_text, validate_vectors, embedding_batch_limit
from .models import Product, Source, Technique
from .repositories.retrieval import visible_chunk

PENDING = ('pending', 'failed')
MAX_CHUNKS_PER_DOCUMENT = 512


def _visible():
    technique = (
        select(Technique.id)
        .where(
            Technique.id == Document.record_id,
            Technique.owner_id == 'local',
            Technique.status == 'published',
        )
        .exists()
    )
    product = (
        select(Product.id)
        .where(
            Product.id == Document.record_id,
            Product.owner_id == 'local',
            Product.status == 'published',
        )
        .exists()
    )
    source = (
        select(DocumentSource.source_id)
        .join(Source, Source.id == DocumentSource.source_id)
        .where(
            DocumentSource.document_id == Document.id,
            DocumentSource.version == Document.active_version,
            Source.owner_id == 'local',
            Source.status == 'published',
        )
        .exists()
    )
    return and_(
        source,
        or_(
            and_(Document.kind == 'technique', technique), and_(Document.kind == 'product', product)
        ),
    )


def _embedding_text(chunk):
    # Title-only records and empty OCR/image blocks still have real title metadata.
    return embedding_text(chunk.content, chunk.payload)


def _snapshot(db, document_id, version):
    return list(
        db.execute(
            select(Embedding, Chunk)
            .join(Chunk)
            .join(Document)
            .where(
                Document.id == document_id,
                Document.active_version == version,
                Chunk.version == version,
                visible_chunk(),
            )
            .order_by(Chunk.ordinal)
            .limit(MAX_CHUNKS_PER_DOCUMENT + 1)
        )
    )


def _publish(engine, document_id, version, targets, vectors, model, *, failed=False):
    """Compare-and-write the entire document; no transaction spans an HTTP request."""
    with Session(engine) as db, db.begin():
        # Lock and recheck the document version before publishing the complete batch.
        matched = db.execute(
            update(Document)
            .where(Document.id == document_id, Document.active_version == version, _visible())
            .values(active_version=version)
        )
        if matched.rowcount != 1:
            return False
        current = _snapshot(db, document_id, version)
        pending = {e.id: (e, c) for e, c in current if e.status in PENDING}
        if set(pending) != {item[0] for item in targets} or any(
            pending[eid][0].content_hash != fingerprint
            or pending[eid][1].content != content
            or _embedding_text(pending[eid][1]) != text
            for eid, text, fingerprint, content in targets
        ):
            return False
        if not failed:
            dimensions = validate_vectors(vectors, len(targets))
            if any(
                e.status == 'ready' and (e.model != model or e.dimensions != dimensions)
                for e, _ in current
            ):
                raise ProviderError('该文档已有不同模型或维度的向量；请使用原配置重试。')
        for index, (eid, text, _, _) in enumerate(targets):
            embedding = pending[eid][0]
            if failed:
                embedding.status = 'failed'
            else:
                embedding.vector, embedding.model = vectors[index], model
                embedding.dimensions, embedding.status = dimensions, 'ready'
                embedding.content_hash = digest(text)
        return True


async def embed_pending(
    engine, provider, *, apply=False, batch_size=32, max_documents=20, progress=None
):
    """Plan by default; --apply callers explicitly authorize the outbound text requests."""
    if type(batch_size) is not int or not 1 <= batch_size <= 64:
        raise ValueError('batch_size must be between 1 and 64')
    if type(max_documents) is not int or not 1 <= max_documents <= 100:
        raise ValueError('max_documents must be between 1 and 100')
    batch_size = min(batch_size, embedding_batch_limit(provider.embedding_model))
    with Session(engine) as db:
        documents = list(
            db.execute(
                select(Document.id, Document.active_version)
                .join(Chunk)
                .join(Embedding)
                .where(
                    Chunk.version == Document.active_version,
                    Embedding.status.in_(PENDING),
                    visible_chunk(),
                )
                .distinct()
                .order_by(Document.id)
                .limit(max_documents)
            )
        )
    result = {
        'status': 'completed' if apply else 'dry_run',
        'dry_run': not apply,
        'configured': provider.embedding_configured,
        'documents': [],
    }
    if not provider.embedding_configured:
        result['status'] = 'unconfigured'
    if progress:
        progress(0, len(documents))
    for completed, (document_id, version) in enumerate(documents, 1):
        with Session(engine) as db:
            snapshot = _snapshot(db, document_id, version)
            targets = [
                (e.id, _embedding_text(c), e.content_hash, c.content)
                for e, c in snapshot
                if e.status in PENDING
            ]
            ready = [(e.model, e.dimensions) for e, _ in snapshot if e.status == 'ready']
        item = {
            'document_id': document_id,
            'version': version,
            'chunks': len(targets),
            'status': 'planned',
        }
        result['documents'].append(item)
        if not targets:
            item['status'] = 'stale'
            if progress:
                progress(completed, len(documents))
            continue
        if not apply or not provider.embedding_configured:
            if progress:
                progress(completed, len(documents))
            continue
        try:
            if len(snapshot) > MAX_CHUNKS_PER_DOCUMENT:
                raise ProviderError('该文档超过 512 个分块的执行上限；请先拆分文档。')
            if any(digest(content) != fingerprint for _, _, fingerprint, content in targets):
                raise ProviderError('分块内容校验失败；请重新入库后重试。')
            if any(model != provider.embedding_model for model, _ in ready):
                raise ProviderError('该文档已有其他模型的向量；请使用原配置重试。')
            vectors = []
            for offset in range(0, len(targets), batch_size):
                # Permission/version may change while another document or batch is encoding.
                batch = targets[offset : offset + batch_size]
                with Session(engine) as db:
                    current = {e.id: (e, c) for e, c in _snapshot(db, document_id, version)}
                    if any(
                        eid not in current
                        or current[eid][0].status not in PENDING
                        or current[eid][0].content_hash != fingerprint
                        or current[eid][1].content != content
                        or _embedding_text(current[eid][1]) != text
                        for eid, text, fingerprint, content in batch
                    ):
                        break
                vectors.extend(await provider.embed([text for _, text, _, _ in batch]))
            else:
                validate_vectors(vectors, len(targets))
                if _publish(
                    engine, document_id, version, targets, vectors, provider.embedding_model
                ):
                    item['status'] = 'ready'
                    if progress:
                        progress(completed, len(documents))
                    continue
            item['status'] = 'stale'
        except Exception as exc:
            item['status'] = 'failed'
            item['error'] = (
                str(exc)
                if isinstance(exc, ProviderError)
                else '向量任务失败；未发布部分结果，请检查配置后重试。'
            )
            _publish(
                engine, document_id, version, targets, [], provider.embedding_model, failed=True
            )
            result['status'] = 'partial_failure'
        if progress:
            progress(completed, len(documents))
    return result
