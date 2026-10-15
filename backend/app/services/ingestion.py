"""Explicit index operations; ordinary ORM writes have no ingestion side effects."""

import re
from .. import ingestion
from ..models import Product, Technique
from ..repositories.ingestion import IngestionRepository
from .errors import ServiceError


class IngestionService:
    def __init__(self, session):
        self.session = session
        self.repository = IngestionRepository(session)

    def reindex(self, row, *, force=False, strict=False):
        """Flush one whole derived version; the caller owns commit or rollback."""
        if not isinstance(row, (Technique, Product)):
            raise TypeError('Only techniques and products can be indexed')
        self.session.flush()
        payload = ingestion.record_payload(row)
        payload['source_ids'] = sorted(source.id for source in row.sources)
        attachments = ingestion.attachment_hashes(row)
        fingerprint = ingestion.digest(
            {
                'record': {c.name: getattr(row, c.name) for c in row.__table__.columns},
                'payload': payload,
                'images': ingestion.image_evidence(row),
                'attachments': attachments,
            }
        )
        document = self.repository.lock_document(payload)
        if not force and document.fingerprint == fingerprint:
            return document
        job = self.repository.start_job(document, document.active_version + 1)
        try:
            prepared = []
            for content, attachment_id in ingestion.chunk_record(row):
                searchable = ' '.join(
                    [
                        payload['title'],
                        content,
                        payload['sku'],
                        ' '.join(payload['aliases']),
                        ' '.join(payload['fields'].values()),
                    ]
                )
                terms = ingestion.index_terms(searchable)
                compact_sku = re.sub(r'[\s_-]+', '', payload['sku'].casefold())
                if compact_sku:
                    terms.add('s:' + compact_sku)
                    if len(re.findall(r'\d', compact_sku)) >= 5:
                        terms.update(
                            's:'
                            + compact_sku[:i]
                            + compact_sku[i + 1]
                            + compact_sku[i]
                            + compact_sku[i + 2 :]
                            for i in range(len(compact_sku) - 1)
                        )
                prepared.append(
                    {
                        'content': content,
                        'attachment_id': attachment_id,
                        'terms': terms,
                        'content_hash': ingestion.digest(content),
                        'payload': {
                            **payload,
                            **(
                                {'attachment_hash': attachments[attachment_id]}
                                if attachment_id
                                else {}
                            ),
                        },
                    }
                )
        except Exception:
            job.status, job.error = 'failed', '资料解析或索引生成失败；旧版本仍可检索，请重试。'
            document.status = 'failed'
            if strict:
                raise ValueError(job.error) from None
            self.session.flush()
            return document
        self.repository.publish(document, job, fingerprint, payload, prepared)
        return document

    def backfill(self, *, missing_only=False, strict=False):
        """Explicitly index current records without committing any writes."""
        self.session.flush()
        count = 0
        for row in self.repository.records(missing_only=missing_only):
            self.reindex(row, strict=strict)
            count += 1
        return count

    def clear(self):
        self.repository.clear()

    def jobs(self):
        return [
            {
                'id': row.id,
                'document_id': row.document_id,
                'version': row.version,
                'status': row.status,
                'progress': row.progress,
                'error': row.error,
            }
            for row in self.repository.recent_jobs()
        ]

    def rebuild_catalog(self):
        """HTTP application operation with an explicit transaction boundary."""
        try:
            count = self.backfill()
            self.session.commit()
        except Exception:
            self.session.rollback()
            raise
        return {'indexed': count, 'embeddings': 'pending'}

    def retry_document(self, document_id):
        document = self.repository.document(document_id)
        if document is None:
            raise ServiceError(404, 'Not found')
        row = self.repository.record(document)
        if row is None or row.owner_id != 'local':
            raise ServiceError(404, 'Not found')
        try:
            document = self.reindex(row, force=True)
            self.session.commit()
        except Exception:
            self.session.rollback()
            raise
        return {
            'document_id': document.id,
            'version': document.active_version,
            'status': document.status,
        }
