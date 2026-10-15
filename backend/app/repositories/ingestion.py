"""Derived index persistence. No method commits the caller's transaction."""

from sqlalchemy import delete, select
from ..ingestion_models import Chunk, Document, DocumentSource, Embedding, IngestionJob, Posting
from ..models import Product, Technique


class IngestionRepository:
    def __init__(self, session):
        self.session = session

    def recent_jobs(self):
        return self.session.scalars(
            select(IngestionJob).order_by(IngestionJob.id.desc()).limit(100)
        ).all()

    def document(self, document_id):
        return self.session.get(Document, document_id)

    def record(self, document):
        return self.session.get(
            Technique if document.kind == 'technique' else Product, document.record_id
        )

    def records(self, *, missing_only=False):
        for model, kind in ((Technique, 'technique'), (Product, 'product')):
            statement = select(model).order_by(model.id)
            if missing_only:
                statement = statement.where(
                    ~select(Document.id)
                    .where(Document.kind == kind, Document.record_id == model.id)
                    .exists()
                )
            yield from self.session.scalars(statement.execution_options(yield_per=100))

    def lock_document(self, payload):
        document = self.session.scalar(
            select(Document).where(Document.record_key == payload['key']).with_for_update()
        )
        if document is None:
            document = Document(
                record_key=payload['key'], kind=payload['type'], record_id=payload['id']
            )
            self.session.add(document)
            self.session.flush()
        return document

    def start_job(self, document, version):
        job = IngestionJob(document_id=document.id, version=version)
        self.session.add(job)
        self.session.flush()
        return job

    def publish(self, document, job, fingerprint, payload, prepared):
        version = job.version
        self.session.add_all(
            DocumentSource(document_id=document.id, version=version, source_id=source_id)
            for source_id in payload['source_ids']
        )
        for ordinal, part in enumerate(prepared):
            chunk = Chunk(
                document_id=document.id,
                version=version,
                ordinal=ordinal,
                kind='image' if part['attachment_id'] else 'text',
                content=part['content'],
                attachment_id=part['attachment_id'],
                payload=part['payload'],
            )
            self.session.add(chunk)
            self.session.flush()
            self.session.add(
                Embedding(chunk_id=chunk.id, content_hash=part['content_hash'], status='pending')
            )
            self.session.add_all(
                Posting(chunk_id=chunk.id, term=term) for term in sorted(part['terms'])
            )
        document.active_version, document.fingerprint, document.status = (
            version,
            fingerprint,
            'ready',
        )
        job.status, job.progress = 'completed', 100
        self.session.flush()

    def clear(self):
        for model in (Posting, Embedding, Chunk, DocumentSource, IngestionJob, Document):
            self.session.execute(delete(model))
