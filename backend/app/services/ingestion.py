"""Application operations for rebuilding and retrying the derived index."""

from ..ingestion import backfill, ingest_record
from .errors import ServiceError


class IngestionService:
    def __init__(self, repository):
        self.repository = repository

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

    def backfill(self):
        count = backfill(self.repository.session)
        self.repository.session.commit()
        return {'indexed': count, 'embeddings': 'pending'}

    def retry(self, document_id):
        document = self.repository.document(document_id)
        if document is None:
            raise ServiceError(404, 'Not found')
        row = self.repository.record(document)
        if row is None or row.owner_id != 'local':
            raise ServiceError(404, 'Not found')
        document = ingest_record(self.repository.session, row, force=True)
        self.repository.session.commit()
        return {
            'document_id': document.id,
            'version': document.active_version,
            'status': document.status,
        }
