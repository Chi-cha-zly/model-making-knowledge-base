"""Database access for catalog records; callers own transaction decisions."""

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from ..models import Attachment, Product, Source, Technique
from ..retrieval import search_documents


class CatalogRepository:
    def __init__(self, session):
        self.session = session

    def get(self, model, record_id):
        return self.session.get(model, record_id)

    def list(self, model):
        statement = select(model).where(model.owner_id == 'local').order_by(model.id)
        if model in (Technique, Product):
            statement = statement.options(
                selectinload(model.sources), selectinload(model.attachments)
            )
        if model is Product:
            statement = statement.options(selectinload(Product.aliases))
        return self.session.scalars(statement).all()

    def sources(self, ids):
        return self.session.scalars(
            select(Source).where(
                Source.id.in_(ids), Source.owner_id == 'local', Source.status != 'withdrawn'
            )
        ).all()

    def attachments(self, ids):
        return self.session.scalars(
            select(Attachment).where(
                Attachment.id.in_(ids),
                Attachment.owner_id == 'local',
                Attachment.status == 'published',
            )
        ).all()

    def search(self, query):
        hits = search_documents(self.session, query, k=20)
        return [
            (hit, self.get(Technique if hit['type'] == 'technique' else Product, hit['id']))
            for hit in hits
        ]

    def add(self, record):
        self.session.add(record)

    def commit(self):
        self.session.commit()

    def rollback(self):
        self.session.rollback()
