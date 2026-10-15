"""Evaluation persistence and corpus identity; callers own transactions."""

from copy import deepcopy
from sqlalchemy import select, delete, func, Text
from ..evaluation_demo import demo_fixture
from ..evaluation_models import EvaluationDataset, EvaluationRun
from ..evaluation_metrics import fingerprint
from ..ingestion_models import Chunk, Document, Embedding
from ..services.errors import ServiceError
from .retrieval import visible_chunk, sources_for


def dataset(db, id):
    if id == 'demo-v1':
        return demo_fixture()[0]
    row = db.get(EvaluationDataset, id)
    if row is None:
        raise ServiceError(404, 'Dataset not found')
    return deepcopy(row.payload)


def summary(value):
    return {key: item for key, item in value.items() if key != 'cases'}


class EvaluationRepository:
    def __init__(self, db):
        self.db = db

    def datasets(self):
        return [
            summary(demo_fixture()[0]),
            *[
                summary(row.payload)
                for row in self.db.scalars(select(EvaluationDataset).order_by(EvaluationDataset.id))
            ],
        ]

    def save_dataset(self, payload, updating=False):
        if updating:
            dataset(self.db, payload['id'])
            self.db.get(EvaluationDataset, payload['id']).payload = payload
        else:
            self.db.add(EvaluationDataset(id=payload['id'], payload=payload))

    def delete_dataset(self, id):
        dataset(self.db, id)
        self.db.execute(delete(EvaluationDataset).where(EvaluationDataset.id == id))

    def save_report(self, payload):
        row = EvaluationRun(payload=payload)
        self.db.add(row)
        self.db.flush()
        row.payload = {'id': row.id, **payload}
        return deepcopy(row.payload)

    def reports(self):
        fields = {
            'id',
            'dataset_id',
            'dataset_name',
            'created_at',
            'k',
            'corpus',
            'metrics',
            'report_version',
            'strategies',
            'degraded_cases',
        }
        return [
            {key: value for key, value in row.payload.items() if key in fields}
            for row in self.db.scalars(
                select(EvaluationRun).order_by(EvaluationRun.id.desc()).limit(50)
            )
        ]

    def report(self, id):
        row = self.db.get(EvaluationRun, id)
        if row is None:
            raise ServiceError(404, 'Evaluation run not found')
        return deepcopy(row.payload)

    def corpus_identity(self):
        rows = self.db.execute(
            select(
                Chunk.id,
                Chunk.document_id,
                Chunk.version,
                Chunk.content,
                Chunk.payload,
                Document.record_key,
                Embedding.status,
                Embedding.model,
                Embedding.dimensions,
                Embedding.content_hash,
                func.md5(Embedding.vector.cast(Text)),
            )
            .select_from(Chunk)
            .join(Document)
            .outerjoin(Embedding)
            .where(visible_chunk())
            .order_by(Chunk.id)
        ).all()
        identities = {(row.document_id, row.version) for row in rows}
        sources = sources_for(self.db, identities)
        serial = [
            {'chunk': list(row), 'sources': sources.get((row.document_id, row.version), [])}
            for row in rows
        ]
        return fingerprint(serial), {row.record_key for row in rows}
