"""Ingestion job and source-record lookups."""

from sqlalchemy import select
from ..ingestion_models import Document, IngestionJob
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
